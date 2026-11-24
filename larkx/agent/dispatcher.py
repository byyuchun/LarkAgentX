import asyncio
import os
import re
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from loguru import logger

from .. import accounts, media, memory

INSTRUCTIONS = '以上是飞书会话里新到的消息。你是这个数字人,代表本人回复。如果对方也是机器人且只是互相客套寒暄,直接结束不要再接话。'
# 讲清楚上下文的结构,不管用户有没有自定义系统提示词都附上
CONTEXT_GUIDE = ('<history> 是这个会话自你上次处理以来的消息,按时间先后。needs_reply="true" 的是这次要你处理的'
                 '(可能是连着发的好几条,合在一起回一次,不要逐条回;如果这条同时是 role="me",说明是本人主动在调用或指示你,'
                 '直接按他的要求处理并回复);其余 role="me" 是本人亲手发的上下文(你代表的就是他:'
                 '别重复他已经说过的,别和他矛盾,他已经回答了就不用再回);<quoted> 是被回复/引用的原消息;'
                 '<attachments> 是已自动下载到本地的图片附件,可直接读取路径查看截图内容。')
TOPIC_FOLLOWUP_GUIDE = ('这个话题是由本人此前主动唤起你介入的,当前是话题内其他成员发来的后续消息:'
                        '如果对方是在提问题、追问细节、补充报错/现象或需要你继续排查,请直接结合话题上下文和工作区代码回复;'
                        '如果只是感谢、确认(如「好的」「收到」「谢谢」)、表情包、与技术排查无关的闲聊,'
                        '或本人(role="me")已经回答过,直接结束不要回复。')
READ_ONLY_GUIDE = ('【只读保护】当前为他人追问触发的只读模式: 严禁修改、创建、删除、格式化任何本地代码或仓库文件,'
                   '禁止执行 git 写操作、go mod tidy、改库或提工单等变更命令;仅限只读查阅代码/文档/日志并通过 lark send 回复。')
DRAFT_MODE_GUIDE = ('【私密草稿模式】这是本人在「{chat_name}」(会话 {chat_id}) 中发出的私密指令: '
                    '请结合上面的原会话上下文、附件截图和工作区代码完成排查,'
                    '但绝对不要往原会话({chat_id})发任何消息! '
                    '请把排查结论和可直接复制发送的建议回复,发到本人与自己的私聊会话({self_chat_id})里(开头简明标注来源会话名)。')
OUTPUT_RULES = '不需要回复就直接结束,不要解释、不要复述消息。'
# 发送选项,写给 agent 看。撤回、转发、@ 只在明确要求时用
SEND_HELP = ('lark send 的其他用法: -m 按 markdown 发(代码块、列表、加粗、链接);--mention 用户id:名字 真正 @ 某人(可重复,'
             '正文里写到的「@名字」会变成 @);--image 路径 / --file 路径 发图片或文件。lark recall 消息id 撤回自己发的消息;'
             'lark forward 消息id... --to 会话id [--merge] 转发。lark task add --in 30m / --at "2026-01-02 09:00" / --daily 09:00 "要做的事" '
             '安排定时任务(到点后在当前会话里交给你处理),lark task 查看,lark task rm 编号 删除。')
# 太久以前的消息(重启后排队里剩下的、补收回来的)只入库不回复,免得回复几个小时前的事
STALE_SECONDS = 600
REVIEW_KEY = 'memory-review'
BATCH_SECONDS = 3           # 收到要处理的消息后再等这么久,连着发的几条合成一轮
CONTEXT_LIMIT = 40          # 一轮最多带多少条历史消息
FIRST_CONTEXT_LIMIT = 15    # 这个会话还没处理过(或刚重启)时往前带多少条
FIRST_CONTEXT_SECONDS = 86400   # ……而且只带最近 24 小时的,几天前的旧话题不带
CONTEXT_CHARS = 1500        # 每条历史消息最多带多少字
MAX_TURN_IMAGES = 6         # 单轮最多自动下载多少张图片附件给 agent 看图
TYPE_NAMES = {1: '私聊', 2: '群聊', 3: '话题群'}
_LEAD_IMAGE_RE = re.compile(r'^(?:\[图片[^\]]*\]\s*)+')


def strip_rich_prefix(content: str) -> str:
    """去掉富文本解码时加的「[富文本]」头,让带截图/多行列表的消息也能正常匹配前缀。"""
    c = str(content or '').strip()
    if c.startswith('[富文本]'):
        c = c[len('[富文本]'):].lstrip()
    return c


def match_prefix(content: str, prefix: str) -> bool:
    """判断消息是否以 prefix 开头(兼容富文本前缀和开头贴图后写前缀的情况)。"""
    if not prefix:
        return True
    c = strip_rich_prefix(content)
    if c.startswith(prefix):
        return True
    return _LEAD_IMAGE_RE.sub('', c).lstrip().startswith(prefix)


def _attr(v) -> str:
    return quoteattr(str(v if v is not None else ''))


def _clip(text: str, n: int = CONTEXT_CHARS) -> str:
    text = str(text or '')
    return text if len(text) <= n else text[:n] + f'…(后面还有 {len(text) - n} 字)'


def batchable(msg: dict) -> bool:
    """普通聊天消息可以合并;定时任务、每日回顾各自单独一轮。"""
    return msg.get('_task') is None and msg.get('_review') is None


def chat_config(cfg: dict, chat_id) -> dict:
    """「只处理这些会话」里给这个会话(私聊或群聊)单独做的设置,没有返回 {}。"""
    rows = cfg.get('chat_configs')
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get('chat_id')) == str(chat_id):
            return row
    return {}


def workdir_for(cfg: dict, chat_id) -> str:
    """这个会话单独设置的工作目录,没设返回空(用全局工作目录)。"""
    return (chat_config(cfg, chat_id).get('workdir') or '').strip()


def workspaces_for(cfg: dict, chat_id) -> tuple:
    """返回 (主工作目录, 额外工作区目录列表),展开 ~ 并去重。"""
    raw = []
    for p in (workdir_for(cfg, chat_id), (cfg.get('workdir') or '').strip(), *(cfg.get('extra_workdirs') or [])):
        s = str(p or '').strip()
        if not s:
            continue
        expanded = os.path.expanduser(s)
        if expanded not in raw:
            raw.append(expanded)
    return (raw[0], raw[1:]) if raw else ('', [])


def _ts(msg: dict) -> str:
    t = msg.get('create_time')
    if not t:
        return ''
    try:
        return datetime.fromtimestamp(int(t)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(t)


def build_xml_message(m: dict) -> str:
    lines = ['<message>']
    lines.append(f'  <chat id={quoteattr(str(m.get("chat_id")))} name={quoteattr(str(m.get("_chat_name") or ""))} type={quoteattr(str(m.get("chat_type_name") or ""))} anchor={quoteattr(str(m.get("anchor") or ""))}/>')
    lines.append(f'  <sender id={quoteattr(str(m.get("from_id")))} name={quoteattr(str(m.get("_sender_name") or ""))}/>')
    if m.get('msg_id'):
        lines.append(f'  <msg_id>{escape(str(m["msg_id"]))}</msg_id>')
    if m.get('at_me'):
        lines.append('  <at_me>true</at_me>')
    lines.append(f'  <time>{escape(_ts(m))}</time>')
    lines.append(f'  <content>{escape(str(m.get("content") or ""))}</content>')
    lines.append('</message>')
    return '\n'.join(lines)


def is_stale(msg: dict, now: float = None) -> bool:
    try:
        return (now or time.time()) - int(msg.get('create_time') or 0) > STALE_SECONDS
    except (TypeError, ValueError):
        return False


class SessionWorker:
    def __init__(self, dispatcher, session_key: str):
        self.dispatcher = dispatcher
        self.session_key = session_key
        self.queue = asyncio.Queue()
        self.busy = False
        self.current = None
        self._interrupted = False
        self.task = asyncio.create_task(self._run())

    def _drain(self) -> tuple:
        """取出队列里现有的全部条目,分成能合并的聊天消息和其余(任务/回顾,放回队尾)。"""
        chat, other = [], []
        while True:
            try:
                item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            (chat if batchable(item[1]) else other).append(item)
        for item in other:
            self.queue.put_nowait(item)
        return chat, bool(other)

    async def _run(self):
        while True:
            batch = [await self.queue.get()]
            self.busy, self._interrupted = True, False
            if batchable(batch[0][1]):
                # 等对方说完: 安静 BATCH_SECONDS 秒才开始;agent 处理期间新来的消息也会在下一轮一起交过去
                while True:
                    await asyncio.sleep(self.dispatcher.batch_seconds)
                    more, _ = self._drain()
                    if not more:
                        break
                    batch += more
            msgs = [m for _, m in batch]
            self.current = asyncio.ensure_future(self.dispatcher.handle_batch(self.session_key, msgs))
            try:
                await self.current
            except asyncio.CancelledError:
                if not self._interrupted:
                    raise          # 整个 worker 被取消(停账号),排队记录留着,下次启动接着处理
                self.dispatcher.log.info(f'已打断 [{self.session_key}] 正在处理的消息')
            except Exception as e:
                self.dispatcher.log.error(f'处理消息异常 [{self.session_key}]: {e}')
            self.busy, self.current = False, None
            for pending_id, _ in batch:
                self.dispatcher.forget(pending_id)

    def interrupt(self) -> bool:
        """打断正在跑的这条(agent 子进程会被杀掉)。"""
        if self.current and not self.current.done():
            self._interrupted = True
            self.current.cancel()
            return True
        return False


class AgentDispatcher:
    def __init__(self, backend, reply_fn, cfg: dict=None, account_id: str=None, storage=None):
        self.account_id = accounts.normalize(account_id)
        self.cfg = cfg if cfg is not None else accounts.get_settings(self.account_id)
        self.log = logger.bind(account=self.account_id, kind='agent')
        self.backend = backend
        self.reply_fn = reply_fn
        self.storage = storage
        self.workers = {}
        self.memory_seen = {}      # session_key -> 上次附上的记忆/会话说明/工作区
        self.context_mark = {}     # session_key -> 上次交给 agent 的最后一条消息的时间(下一轮从这之后带历史)
        self._last_main_thread = {}  # chat_id -> 最近由主会话开出话题的 session_key(方便 /stop 打断)
        self.batch_seconds = BATCH_SECONDS
        self.denied = None         # 名单外的人触发时的回调(由 Listener 设置)
        self.accepted = None       # 消息交给 agent 时的回调(点 👌)
        self.handled = None        # agent 处理完一条聊天消息后的回调(回复过就标已读)
        self.prepare_context = None  # 交给 agent 前补拉缺失根消息/历史记录的回调(由 Listener 设置)
        self.download_image = None   # 自动下载消息图片到本地的回调 (chat_id, msg_id, key) -> path
        self.recall_msg = None       # 自动撤回私密草稿指令的回调 (msg_id) -> None
        self.self_chat_id = ''       # 「和自己的会话」chat_id(私密草稿模式发到这里)

    def _quote_thread_root(self, msg: dict) -> str:
        """群主会话开话题回复且引用了别人消息时,优先挂在被引用消息下开话题(系统消息除外)。"""
        if not self.cfg.get('reply_thread_on_quote', True):
            return ''
        pid = str(msg.get('parent_id') or '')
        if not pid or pid == str(msg.get('msg_id') or ''):
            return ''
        if self.storage:
            try:
                q = self.storage.get_message(pid, str(msg.get('chat_id') or ''))
                if q and q.msg_type == 6:
                    return ''
            except Exception:
                pass
        return pid

    def _effective_anchor(self, msg: dict) -> str:
        """这条消息对应的最终话题 anchor: 已在话题里用原 anchor;
        群聊主会话且设置了「开话题回复」时,引用了别人消息则挂在对方消息下开话题,否则挂在自己消息下。"""
        anchor = msg.get('anchor') or ''
        if not anchor and self.cfg.get('reply_mode') == 'thread' and msg.get('chat_type') != 1 and msg.get('msg_id'):
            return self._quote_thread_root(msg) or str(msg['msg_id'])
        return anchor

    def session_key_of(self, msg: dict) -> str:
        if msg.get('_draft'):
            return f"draft:{msg.get('chat_id')}:{msg.get('anchor') or ''}"
        scope = self.cfg['context_scope']
        if scope == 'global':
            return 'global'
        if scope == 'chat':
            return str(msg.get('chat_id'))
        return f"{msg.get('chat_id')}:{self._effective_anchor(msg)}"

    def _topic_followup_ok(self, chat_id: str, anchor: str) -> bool:
        t = self.cfg.get('triggers') or {}
        if not anchor or not self.storage:
            return False
        if (t.get('trigger_by') or 'others') not in ('owner', 'all') or not t.get('topic_followup', True):
            return False
        try:
            return self.storage.topic_is_active(chat_id, anchor)
        except Exception:
            return False

    def should_handle(self, msg: dict, my_user_id: str, self_chat: bool=False, self_trigger: bool=False) -> bool:
        return self.check(msg, my_user_id, self_chat, self_trigger=self_trigger) == 'ok'

    def check(self, msg: dict, my_user_id: str, self_chat: bool=False, self_trigger: bool=False) -> str:
        """'ok' 交给 agent;'denied' 本该触发但发送者不在「只处理这些发送者」名单里;其他情况返回 'skip'。
        self_chat: 用户在「和自己的会话」里亲手发的,这是他自己开的入口,不看会话类型和名单,只看前缀/关键词/排除。
        self_trigger: 用户在与他人会话里亲手发送的带前缀消息(已过回显防环检查),按会话白名单/黑名单/类型/关键词过滤,
        不受 @我 和「只回复这些人」限制。"""
        is_owner = str(msg.get('from_id')) == str(my_user_id)
        t = self.cfg.get('triggers') or {}
        trigger_by = t.get('trigger_by') or 'others'
        prefix = t.get('prefix') or ''
        content = (msg.get('content') or '').strip()
        chat_id = str(msg.get('chat_id'))
        sender_id = str(msg.get('from_id'))
        anchor = msg.get('anchor') or ''
        type_name = {1: 'p2p', 2: 'group', 3: 'topic_group'}.get(msg.get('chat_type'), '')
        exclude = [str(x) for x in t.get('exclude_chats') or []]
        if exclude and chat_id in exclude:
            return 'skip'
        blocked = [str(x) for x in t.get('exclude_senders') or []]
        if blocked and sender_id in blocked:
            return 'skip'
        # 本人此前用前缀激活过的话题: 话题内其他成员的后续消息直接放行,由 agent 判断是否为问题
        if (not is_owner) and anchor and self._topic_followup_ok(chat_id, anchor):
            return 'ok'
        if (not is_owner) and trigger_by == 'owner':
            return 'skip'
        owner_prefix_ok = bool(is_owner and self_trigger and trigger_by in ('owner', 'all') and prefix and match_prefix(content, prefix))
        if is_owner and not self_chat and not owner_prefix_ok:
            return 'skip'
        if prefix and (not match_prefix(content, prefix)):
            return 'skip'
        if type_name == 'p2p' and not self_chat and t.get('dm_enabled', True) is False:
            return 'skip'
        chat_types = t.get('chat_types') or []
        if chat_types and type_name not in chat_types and not self_chat:
            return 'skip'
        include = [str(x) for x in t.get('include_chats') or []]
        if include and chat_id not in include and not self_chat:
            return 'skip'
        keywords = t.get('keywords') or []
        if keywords and (not any((kw in content for kw in keywords))):
            return 'skip'
        if t.get('group_at_only') and type_name in ('group', 'topic_group') and (not msg.get('at_me')) and not owner_prefix_ok:
            return 'skip'
        # 「只回复这些人」是会话自己的设置(群聊里用);旧的全局 include_senders 只在会话没单独设置时兜底
        senders = [str(x) for x in chat_config(self.cfg, chat_id).get('senders') or t.get('include_senders') or []]
        if senders and sender_id not in senders and not self_chat and not owner_prefix_ok:
            return 'denied'
        return 'ok'

    async def submit(self, msg: dict, chat_name: str='', sender_name: str='', my_user_id: str='',
                     self_chat: bool=False, self_trigger: bool=False, self_draft: bool=False):
        is_owner = str(msg.get('from_id')) == str(my_user_id)
        t = self.cfg.get('triggers') or {}
        if is_owner and self_draft:
            draft_prefix = (t.get('draft_prefix') or '').strip()
            chat_id = str(msg.get('chat_id'))
            exclude = [str(x) for x in t.get('exclude_chats') or []]
            blocked = [str(x) for x in t.get('exclude_senders') or []]
            if not draft_prefix or not match_prefix(msg.get('content') or '', draft_prefix):
                return
            if (exclude and chat_id in exclude) or (blocked and str(msg.get('from_id')) in blocked):
                return
            if not self.self_chat_id:
                self.log.warning('未拿到「和自己的会话」id,跳过私密草稿模式以免误发到原会话')
                return
            draft_msg = {**msg, '_is_own': True, '_draft': True}
            if t.get('draft_auto_recall', True) and self.recall_msg and msg.get('msg_id'):
                asyncio.ensure_future(self.recall_msg(str(msg['msg_id'])))
            await self._enqueue(draft_msg, chat_name, sender_name)
            return
        cmd, remainder = self._parse_command(msg.get('content') or '')
        if cmd and is_owner and (remainder == '' or self_chat or (self_trigger and self.check(msg, my_user_id, self_chat, self_trigger=True) == 'ok')):
            # 你本人在任何会话里发的 /stop、/clear、/end 都管用;带后续内容的写法在「和自己的会话」或自己前缀触发时认
            await self._run_command(cmd, remainder, msg, chat_name, sender_name)
            return
        verdict = self.check(msg, my_user_id, self_chat, self_trigger=self_trigger)
        if verdict == 'denied':
            if self.denied and self.cfg.get('deny_reply'):
                await self.denied(msg)
            return
        if verdict != 'ok':
            return
        if cmd and not is_owner:
            return             # 别人发的 /stop、/clear、/end 不当命令,也不交给 agent
        chat_id = str(msg.get('chat_id'))
        anchor = msg.get('anchor') or ''
        eff_anchor = self._effective_anchor(msg)
        msg = dict(msg)
        if is_owner:
            msg['_is_own'] = True
            # 本人用前缀唤起话题(开新话题或在已有话题里调用) -> 激活该话题,后续允许跟进他人提问
            if self_trigger and eff_anchor and t.get('topic_followup', True) and self.storage:
                self.storage.topic_activate(chat_id, eff_anchor)
        elif anchor and self._topic_followup_ok(chat_id, anchor):
            msg['_topic_followup'] = True
        if not anchor and eff_anchor:
            self._last_main_thread[chat_id] = self.session_key_of(msg)
        if self.accepted:
            self.accepted(msg)
        await self._enqueue(msg, chat_name, sender_name)

    async def _run_command(self, cmd, remainder, msg, chat_name, sender_name):
        chat_id = str(msg['chat_id'])
        root_id = (msg.get('anchor') or '') or None
        if cmd == 'end':
            deactivated = bool(self.storage and root_id and self.storage.topic_deactivate(chat_id, root_id))
            await self.reply_fn(chat_id, '✅ 已结束该话题的自动跟进' if deactivated else 'ℹ️ 当前话题未开启自动跟进', root_id)
            return
        # 裸命令(/stop、/clear)在主会话里默认操作主会话 key;如果主会话刚开出话题正在运行,/stop 也能打断它
        key = self.session_key_of(msg) if remainder else (
            'global' if self.cfg['context_scope'] == 'global' else (
                chat_id if self.cfg['context_scope'] == 'chat' else f'{chat_id}:{root_id or ""}'
            )
        )
        n = self._drain_queue(key)
        if cmd == 'clear':
            self.backend.reset_session(key)
            self.memory_seen.pop(key, None)
            self.context_mark.pop(key, None)
        stopped = self._interrupt(key)
        if cmd == 'stop' and not stopped and not root_id:
            last_key = self._last_main_thread.get(chat_id)
            if last_key and last_key != key:
                n += self._drain_queue(last_key)
                stopped = self._interrupt(last_key)
        if remainder:
            await self._enqueue({**msg, 'content': remainder, '_is_own': True}, chat_name, sender_name)
            return
        if cmd == 'clear':
            await self.reply_fn(chat_id, '✅ 已开启新会话', root_id)
        else:
            parts = (['已打断正在处理的消息'] if stopped else []) + ([f'清空了 {n} 条排队消息'] if n else [])
            await self.reply_fn(chat_id, '⏹ ' + ('，'.join(parts) if parts else '没有正在处理的消息'), root_id)

    async def _enqueue(self, msg: dict, chat_name: str='', sender_name: str='', key: str=None):
        msg = dict(msg)
        msg['_chat_name'] = chat_name or msg.get('_chat_name', '')
        msg['_sender_name'] = sender_name or msg.get('_sender_name', '')
        key = key or self.session_key_of(msg)
        pending_id = self.storage.pending_add(key, msg) if self.storage else None
        if key not in self.workers:
            self.workers[key] = SessionWorker(self, key)
        await self.workers[key].queue.put((pending_id, msg))

    async def enqueue_task(self, task):
        """定时任务到点:当成一条消息交给创建它的那个会话。"""
        msg = {'_task': task.content, 'chat_id': task.chat_id, 'anchor': task.anchor, 'scope': 'topic' if task.anchor else 'chat',
               'create_time': int(time.time())}
        await self._enqueue(msg, key=self.session_key_of(msg))

    async def enqueue_review(self, transcript: str) -> bool:
        """每日回顾:单独的会话,不和任何聊天混在一起,每次都从新会话开始,不会一天天越堆越长。
        上一次还在排队或进行中时不再重复,返回 False。"""
        w = self.workers.get(REVIEW_KEY)
        if w and (w.busy or not w.queue.empty()):
            return False
        self.backend.reset_session(REVIEW_KEY)
        await self._enqueue({'_review': transcript, 'chat_id': '', 'create_time': int(time.time())}, key=REVIEW_KEY)
        return True

    async def restore(self):
        """重启后接着处理上次没处理完的消息;太久以前的只记一笔日志,不回复。"""
        if not self.storage:
            return
        stale = 0
        for pending_id, key, msg, created in self.storage.pending_all():
            self.storage.pending_done(pending_id)
            if time.time() - (created or 0) > STALE_SECONDS:
                stale += 1
                continue
            await self._enqueue(msg, key=key)
        if stale:
            self.log.info(f'上次停止时还有 {stale} 条消息没处理,已超过 {STALE_SECONDS // 60} 分钟,不再回复')

    def forget(self, pending_id):
        if self.storage and pending_id:
            try:
                self.storage.pending_done(pending_id)
            except Exception as e:
                self.log.debug(f'删除已处理的排队记录失败: {e}')

    def reply_flags(self, msg: dict) -> str:
        """agent 回复这条消息时 lark send 要带的参数。
        话题里的消息: 默认回到话题(--root),可改成发到外面的主会话。
        主会话的消息: 默认直接发,可改成引用原消息(--quote)或开话题回复(--root)。"""
        anchor = msg.get('anchor') or ''
        if anchor:
            return f' --root {anchor}' if self.cfg.get('reply_in_topic', True) else ''
        mode = self.cfg.get('reply_mode') or 'direct'
        msg_id = str(msg.get('msg_id') or '')
        if not msg_id or mode == 'direct':
            return ''
        # 私聊没有话题,开话题回复退回引用
        if mode == 'thread' and msg.get('chat_type') != 1:
            root = self._quote_thread_root(msg) or msg_id
            return f' --root {root}'
        return f' --quote {msg_id}'

    def stats(self) -> dict:
        workers = list(self.workers.values())
        return {'busy': sum(1 for w in workers if w.busy), 'queued': sum(w.queue.qsize() for w in workers)}

    def clear_session(self, session_key: str) -> int:
        """清排队 + 打断正在跑的 + 换新 agent 会话(和 /clear 一样)。返回清掉的排队条数。"""
        n = self._drain_queue(session_key)
        self._interrupt(session_key)
        self.backend.reset_session(session_key)
        self.memory_seen.pop(session_key, None)
        self.context_mark.pop(session_key, None)
        return n

    def _parse_command(self, content: str):
        t = self.cfg.get('triggers') or {}
        prefix = t.get('prefix') or ''
        c = strip_rich_prefix(content)
        if prefix and c.startswith(prefix):
            c = c[len(prefix):].lstrip()
        for cmd in ('/clear', '/stop', '/end'):
            if c == cmd:
                return cmd[1:], ''
            if c.startswith(cmd) and len(c) > len(cmd) and (c[len(cmd)] in (' ', '\t', '\n')):
                return cmd[1:], c[len(cmd):].strip()
        return None, None

    def _drain_queue(self, key) -> int:
        w = self.workers.get(key)
        if not w:
            return 0
        n = 0
        while True:
            try:
                pending_id, _ = w.queue.get_nowait()
            except asyncio.QueueEmpty:
                return n
            self.forget(pending_id)
            n += 1

    def _interrupt(self, key) -> bool:
        w = self.workers.get(key)
        return bool(w and w.interrupt())

    async def shutdown(self):
        """停账号时取消所有排队 worker(会顺带杀掉正在跑的 agent 子进程)。排队记录留着,下次启动接着处理。"""
        workers, self.workers = list(self.workers.values()), {}
        for w in workers:
            w.task.cancel()
        await asyncio.gather(*(w.task for w in workers), return_exceptions=True)

    def _account_flag(self) -> str:
        return '' if self.account_id == accounts.DEFAULT_ID else f' --account {self.account_id}'

    def _memory_prefix(self, session_key: str, chat_id: str = '', preview: bool = False) -> str:
        """工作区 + 会话说明 + 长期记忆。这个会话第一次处理、或者内容变了,才附上最新的,免得会话历史越堆越长。
        列在「只处理这些会话」里的会话附它自己的独立记忆,其余附共用记忆。"""
        parts = []
        primary_wd, extra_wds = workspaces_for(self.cfg, chat_id)
        if primary_wd or extra_wds:
            ws_lines = ([f'- {primary_wd} (主工作目录)'] if primary_wd else []) + [f'- {d}' for d in extra_wds]
            parts.append('<workspaces>\n' + '\n'.join(escape(line) for line in ws_lines) + '\n</workspaces>\n'
                         '以上是本人为你配置的代码/工作区目录,查代码或回答技术问题时可直接查阅这些目录。')
        notes = (chat_config(self.cfg, chat_id).get('notes') or '').strip() if chat_id else ''
        if notes:
            parts.append(f'<conversation_notes>\n{escape(notes)}\n</conversation_notes>\n'
                         '以上是本人给这个会话写的说明(这个会话是做什么的、要注意什么),回复时遵守。')
        if self.cfg.get('memory_enabled'):
            parts.append(memory.prompt_block(self.account_id, memory.scope_for(self.cfg, chat_id)))
        block = '\n\n'.join(parts)
        if not block or (self.memory_seen.get(session_key) == block and not preview):
            return ''
        if not preview:
            self.memory_seen[session_key] = block
        return block + '\n\n'

    def _context_rows(self, session_key: str, msgs: list):
        """读取这一轮上下文对应的历史记录和被引用消息。返回 (rows, quoted_rows, since, until)。"""
        last = msgs[-1]
        chat_id = str(last['chat_id'])
        anchor = last.get('anchor') or ''
        until = max(int(m.get('create_time') or 0) for m in msgs) or None
        since = self.context_mark.get(session_key)
        # 话题内部本身有边界,首次带完整话题历史(不受 24 小时截断);主会话流只带最近 24 小时
        if anchor:
            floor = since
            limit = CONTEXT_LIMIT
        else:
            floor = since or ((until or int(time.time())) - FIRST_CONTEXT_SECONDS)
            limit = CONTEXT_LIMIT if since else FIRST_CONTEXT_LIMIT
        rows = []
        if self.storage:
            try:
                rows = self.storage.context_messages(chat_id, anchor if self.cfg.get('context_scope') == 'anchor' else None,
                                                     floor, until, limit)
            except Exception as e:
                self.log.debug(f'读取上下文消息失败(只带这一轮的消息): {e}')
        # 普通群里话题根消息入库时 anchor="" (话题群里根消息 anchor=msg_id);
        # 首次进入这个话题时把根消息补到最前面,让 agent 能看到话题最初的问题
        if anchor and not since and self.storage:
            row_ids = {str(r.msg_id) for r in rows}
            root_id = str(last.get('root_id') or anchor)
            for rid in dict.fromkeys(x for x in (root_id, anchor) if x and x not in row_ids):
                try:
                    root_row = self.storage.get_message(rid, chat_id)
                except Exception:
                    root_row = None
                if root_row:
                    rows = [root_row, *rows]
                    break
        seen_ids = {str(r.msg_id) for r in rows} | {str(m.get('msg_id')) for m in msgs if m.get('msg_id')}
        quoted_rows = []
        for m in msgs:
            pid = str(m.get('parent_id') or '')
            if pid and pid not in seen_ids and self.storage:
                try:
                    q = self.storage.get_message(pid, chat_id)
                except Exception:
                    q = None
                if q:
                    seen_ids.add(pid)
                    quoted_rows.append(q)
        return rows, quoted_rows, since, until

    async def _collect_attachments(self, session_key: str, msgs: list) -> list:
        """自动下载本轮上下文中最近的图片(含截图与富文本内嵌图),返回 [(msg_id, key, local_path), ...]。"""
        if not self.download_image or not msgs:
            return []
        last = msgs[-1]
        chat_id = str(last.get('chat_id') or '')
        if not chat_id:
            return []
        rows, quoted_rows, _, _ = self._context_rows(session_key, msgs)
        # 优先下载触发消息、被引用消息、最新历史里的图片(最多 MAX_TURN_IMAGES 张)
        candidates = []
        seen_pairs = set()

        def add_keys(mid, content):
            mid = str(mid or '')
            if not mid:
                return
            for key in media.extract_image_keys(content):
                pair = (mid, key)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    candidates.append(pair)

        for m in reversed(msgs):
            add_keys(m.get('msg_id'), m.get('content'))
        for q in reversed(quoted_rows):
            add_keys(q.msg_id, q.content)
        for r in reversed(rows):
            add_keys(r.msg_id, r.content)
        candidates = candidates[:MAX_TURN_IMAGES]
        if not candidates:
            return []
        paths = await asyncio.gather(*(self.download_image(chat_id, mid, key) for mid, key in candidates), return_exceptions=True)
        out = []
        for (mid, key), res in zip(candidates, paths):
            if isinstance(res, str) and res:
                out.append((mid, key, res))
        return out

    def _conversation_xml(self, session_key: str, msgs: list, preview: bool = False, attachments=()) -> str:
        """这一轮要处理的消息,连同这个会话(话题)自上一轮以来的其他消息、被引用的原消息、已下载图片,一起组成上下文。"""
        last = msgs[-1]
        chat_id = str(last['chat_id'])
        anchor = last.get('anchor') or ''
        triggers = {str(m.get('msg_id')): m for m in msgs if m.get('msg_id')}
        rows, quoted_rows, since, until = self._context_rows(session_key, msgs)
        agent_ids = set()
        session_started = bool(self.backend and self.backend.session_started(session_key))
        if rows and self.storage and session_started:
            try:
                agent_ids = self.storage.agent_sent_ids([r.msg_id for r in rows])
            except Exception:
                agent_ids = set()
        items, seen = [], set()
        for r in rows:
            mid = str(r.msg_id)
            if mid in agent_ids and mid not in triggers:
                continue                 # agent 自己发的,它的会话里本来就有
            seen.add(mid)
            m = triggers.get(mid)
            # 私密草稿指令如果已自动撤回,DB 里 content 会变成「[已撤回]」,优先保留内存中的触发原文
            text = (m.get('content') if m and m.get('content') else r.content)
            items.append(self._message_xml(
                mid, r.create_time, r.sender_name or (m or {}).get('_sender_name', ''), r.sender_id,
                'me' if r.direction == 'out' else 'other', text, needs_reply=mid in triggers,
                at_me=bool(m and m.get('at_me')), reply_to=r.parent_id or ''))
        for mid, m in triggers.items():  # 库里还没有的(理论上不会)也要带上
            if mid not in seen:
                items.append(self._message_xml(mid, m.get('create_time'), m.get('_sender_name', ''), m.get('from_id'),
                                               'me' if m.get('_is_own') else 'other', m.get('content'), needs_reply=True,
                                               at_me=bool(m.get('at_me')), reply_to=m.get('parent_id') or ''))
                seen.add(mid)
        quoted = [f'<quoted id={_attr(q.msg_id)} from={_attr(q.sender_name)} time={_attr(_ts({"create_time": q.create_time}))}>'
                  f'{escape(_clip(q.content))}</quoted>' for q in quoted_rows]
        att_block = []
        if attachments:
            att_block = ['<attachments>',
                         *(f'  <image msg_id={_attr(mid)} key={_attr(key)} path={_attr(path)}/>' for mid, key, path in attachments),
                         '</attachments>']
        if not preview:
            self.context_mark[session_key] = until or int(time.time())
        head = (f'<conversation id={_attr(chat_id)} name={_attr(last.get("_chat_name") or "")} '
                f'type={_attr(TYPE_NAMES.get(last.get("chat_type"), last.get("chat_type_name") or ""))} topic={_attr(anchor)}>')
        note = '' if since else '\n<!-- 这是这个会话最近的消息,其中有些你之前可能已经看过 -->'
        return '\n'.join([head + note, '<history>', *items, '</history>', *quoted, *att_block, '</conversation>'])

    @staticmethod
    def _message_xml(msg_id, create_time, sender, sender_id, role, content, needs_reply=False, at_me=False, reply_to='') -> str:
        attrs = f'id={_attr(msg_id)} time={_attr(_ts({"create_time": create_time}))} from={_attr(sender)} from_id={_attr(sender_id)} role={_attr(role)}'
        if needs_reply:
            attrs += ' needs_reply="true"'
        if at_me:
            attrs += ' at_me="true"'
        if reply_to:
            attrs += f' reply_to={_attr(reply_to)}'
        return f'  <message {attrs}>{escape(_clip(content))}</message>'

    def build_prompt(self, session_key: str, msgs, preview: bool = False, attachments=()) -> str:
        msgs = msgs if isinstance(msgs, list) else [msgs]
        msg = msgs[-1]
        acct = self._account_flag()
        if msg.get('_review') is not None:
            return (memory.prompt_block(self.account_id) + '\n\n' + memory.REVIEW_PROMPT.format(acct=acct) + '\n\n<chats>\n'
                    + msg['_review'] + '\n</chats>')
        chat_id = str(msg['chat_id'])
        is_draft = any(m.get('_draft') for m in msgs) and bool(self.self_chat_id)
        is_followup_only = any(m.get('_topic_followup') for m in msgs) and not any(m.get('_is_own') for m in msgs)
        is_read_only = is_followup_only and bool((self.cfg.get('triggers') or {}).get('followup_read_only', True))
        if msg.get('_task') is not None:
            body = f'<task>\n  <chat id={quoteattr(chat_id)} anchor={quoteattr(str(msg.get("anchor") or ""))}/>\n  <content>{escape(msg["_task"])}</content>\n</task>'
            lead = '以上是你之前安排的定时任务,现在到点了,请执行。'
        else:
            body = self._conversation_xml(session_key, msgs, preview, attachments=attachments) + '\n' + CONTEXT_GUIDE
            lead = self.cfg.get('system_prompt') or INSTRUCTIONS
            if is_draft:
                src_name = msg.get('_chat_name') or chat_id
                lead = lead + '\n' + DRAFT_MODE_GUIDE.format(chat_name=src_name, chat_id=chat_id, self_chat_id=self.self_chat_id)
            elif is_followup_only:
                lead = lead + '\n' + TOPIC_FOLLOWUP_GUIDE
            if is_read_only:
                lead = lead + '\n' + READ_ONLY_GUIDE
        if is_draft:
            reply_cmd = f'lark send {self.self_chat_id} "<回复内容>"' + acct
        else:
            reply_cmd = f'lark send {chat_id} "<回复内容>"' + self.reply_flags(msg) + acct
        rules = f'要回复就执行: {reply_cmd}\n' + OUTPUT_RULES + '\n' + SEND_HELP
        return self._memory_prefix(session_key, chat_id, preview) + body + '\n\n' + lead + '\n' + rules

    async def handle_message(self, session_key: str, msg: dict):
        await self.handle_batch(session_key, [msg])

    async def handle_batch(self, session_key: str, msgs: list):
        """一轮: 这批消息(连着发的合在一起)交给 agent。回复参数、工作目录按最后一条算。"""
        msg = msgs[-1]
        if len(msgs) > 1:
            self.log.info(f'[{session_key}] 连着来的 {len(msgs)} 条消息合成一轮处理')
        if self.prepare_context and batchable(msg):
            try:
                await self.prepare_context(msgs)
            except Exception as e:
                self.log.debug(f'补拉上下文失败(忽略): {e}')
        attachments = await self._collect_attachments(session_key, msgs) if batchable(msg) else []
        prompt = self.build_prompt(session_key, msgs, attachments=attachments)
        is_draft = any(m.get('_draft') for m in msgs)
        is_followup_only = any(m.get('_topic_followup') for m in msgs) and not any(m.get('_is_own') for m in msgs)
        is_read_only = is_followup_only and bool((self.cfg.get('triggers') or {}).get('followup_read_only', True))
        try:
            kwargs = {}
            wd, extra = workspaces_for(self.cfg, msg.get('chat_id') or '')
            extra = list(extra)
            if attachments:
                att_dir = str(Path(attachments[0][2]).parent)
                if att_dir != wd and att_dir not in extra:
                    extra.append(att_dir)
            if wd:
                kwargs['workdir'] = wd
            if extra:
                kwargs['extra_dirs'] = extra
            if is_read_only:
                kwargs['read_only'] = True
            target_chat = self.self_chat_id if (is_draft and self.self_chat_id) else str(msg.get('chat_id') or '')
            target_anchor = '' if is_draft else self._effective_anchor(msg)
            out = await self.backend.run(session_key, prompt, chat_id=target_chat, anchor=target_anchor, **kwargs)
        finally:
            # 第一轮没跑成功时会话还没建立(下次会换一个新会话),记忆下次要重新附上
            if not self.backend.session_started(session_key):
                self.memory_seen.pop(session_key, None)
                self.context_mark.pop(session_key, None)
        if out:
            self.log.debug(f'[{session_key}] agent 输出: {out[:200]}')
        if self.handled and batchable(msg) and not is_draft:
            for m in msgs:
                await self.handled(m)
