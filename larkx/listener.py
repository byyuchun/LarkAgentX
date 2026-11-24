"""一个账号的常驻监听: WS 收消息 -> 入库 -> (可选)交给本地 coding agent。

`lark listen` 和 web 里的「启动账号」共用这一份。
"""
import asyncio
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger

from . import accounts, memory, tasks
from .agent import AgentDispatcher, get_backend
from .agent.dispatcher import is_stale
from .auth import AuthExpired, LarkAuth
from .client import LarkClient
from .locks import FileLock
from .storage import Storage


class ListenerError(Exception):
    pass


class AccountLock(FileLock):
    """同一账号同一时间只允许一个进程监听(lark listen 和 lark web 之间也互斥),
    否则两条连接会把每条消息各回复一遍。"""

    def __init__(self, account_id: str):
        super().__init__(accounts.home_of(account_id) / 'listener.lock')


class UserLock(FileLock):
    """同一个飞书用户在本机只允许监听一处。AccountLock 按数据目录区分,换一个 LARKX_HOME
    (另一份数据目录、演示服务)就拦不住;这把锁放在系统临时目录,按区域 + user_id 区分。"""

    def __init__(self, region: str, user_id: str):
        super().__init__(Path(tempfile.gettempdir()) / 'larkx-locks' / f'{region}-{user_id}.lock')


class Listener:
    # idle -> starting -> connecting -> running <-> reconnecting
    # 终态: stopped(被取消) / login_required(凭证失效) / error
    ACTIVE = ('starting', 'connecting', 'running', 'reconnecting')
    CLOSED_WAIT = 2     # 服务端正常关闭 WS 后多久重连
    ERROR_WAIT = 10     # 异常断开后多久重连
    SELF_ECHO_WAIT = 3  # 和自己的会话: 等 lark send 把自己发的消息落库,再判断回显是不是用户亲手发的

    def __init__(self, account_id: str=None, agent: bool=True, cfg: dict=None):
        self.account_id = accounts.normalize(account_id)
        self.agent = agent
        self.cfg = cfg if cfg is not None else accounts.get_settings(self.account_id)
        self.log = logger.bind(account=self.account_id)
        self.state = 'idle'
        self.error = ''
        self.client = None
        self.dispatcher = None
        self.storage = None
        self.task = None
        self.locks = []
        self._background = None      # 定时任务、每日回顾、补收(跑在 client.loop 上)
        self._catchup_from = None    # 需要补收时,从这个时间点之后有动静的会话开始核对

    @property
    def active(self) -> bool:
        return self.state in self.ACTIVE

    def stats(self) -> dict:
        return self.dispatcher.stats() if self.dispatcher else {'busy': 0, 'queued': 0}

    def _set(self, state: str, error: str=''):
        self.state, self.error = state, error

    def apply_settings(self, new: dict):
        """运行中热更新设置。dispatcher / backend / on_message 共用同一个 cfg 字典,
        只做 update(不 clear),别的线程读到的永远是完整配置;换后端则替换 dispatcher.backend。"""
        old_backend = self.cfg.get('agent_backend')
        self.cfg.update(new)
        if self.dispatcher and new.get('agent_backend') != old_backend:
            self.dispatcher.backend = get_backend(storage=self.storage, cfg=self.cfg, account_id=self.account_id)
        self.log.info('设置已热更新' + (f',agent 后端 {old_backend} -> {new.get("agent_backend")}' if new.get('agent_backend') != old_backend else ''))

    async def run(self, auth: LarkAuth=None):
        """一直跑,直到被取消(stopped)或凭证失效(抛 AuthExpired)。"""
        self._set('starting')
        try:
            await self._run(auth)
        except asyncio.CancelledError:
            self._set('stopped')
            raise
        except AuthExpired as e:
            # 网络抖动导致校验失败不等于凭证失效,别让用户白白重新扫码
            transient = bool(auth and auth.transient_error)
            self._set('error' if transient else 'login_required', str(e))
            self.log.error(str(e))
            raise
        except Exception as e:
            self._set('error', str(e))
            self.log.error(f'监听异常退出: {e}')
            raise
        finally:
            await self._cleanup()

    async def _cleanup(self):
        if self._background and not self._background.done():
            self._background.cancel()
        if self.dispatcher and self.client and self.client._loop_started:
            try:
                fut = asyncio.run_coroutine_threadsafe(self.dispatcher.shutdown(), self.client.loop)
                await asyncio.wait_for(asyncio.wrap_future(fut), timeout=5)
            except Exception as e:
                self.log.debug(f'关闭 agent 队列失败: {e}')
        if self.client:
            self.client.close()
        if self.storage:
            self.storage.close()
        for lock in self.locks:
            lock.release()
        self.locks = []

    def review_now(self) -> str:
        """网页上点「立即回顾」(在线程里调,会等后台给出结果)。返回 'started' / 'busy' / 'empty';
        账号没在运行或没接 agent 时返回 ''。"""
        if not (self.active and self.dispatcher and self.client and self.client._loop_started):
            return ''
        return asyncio.run_coroutine_threadsafe(self._run_review(), self.client.loop).result(timeout=30)

    async def _run(self, auth: LarkAuth=None):
        lock = AccountLock(self.account_id)
        if not lock.acquire():
            raise ListenerError('该账号已在其他进程中运行（lark listen 或 lark web），同时运行会重复回复。请先停止另一处。')
        self.locks.append(lock)
        if auth is None:
            auth = LarkAuth(self.account_id)
        self.client = client = await asyncio.to_thread(LarkClient, auth)
        self.storage = st = Storage(account_id=self.account_id)
        if self.agent and (not auth.user_id):
            await asyncio.to_thread(auth._fetch_profile)
            auth.save()
        if self.agent and (not auth.user_id):
            raise ListenerError('无法确定当前账号 user_id,--agent 存在自回环风险,已拒绝启动。请先 lark auth check 确认凭证有效。')
        if auth.user_id:
            region = getattr(getattr(auth, 'region', None), 'name', '') or 'feishu'
            user_lock = UserLock(region, str(auth.user_id))
            if not user_lock.acquire():
                raise ListenerError('这个飞书账号已在本机其他地方运行（另一个数据目录或演示服务），同时运行会重复回复。请先停止另一处。')
            self.locks.append(user_lock)
        cfg = self.cfg
        log = self.log
        name_cache = {}

        async def _save_fetched(m: dict, chat_id: str, chat_name: str):
            m['chat_id'] = str(m.get('chat_id') or chat_id)
            uid = str(m.get('from_id') or '')
            sname = ''
            if uid:
                skey = (uid, m['chat_id'])
                if skey not in name_cache:
                    name_cache[skey] = await asyncio.to_thread(client.get_user_name, uid, m['chat_id'])
                sname = name_cache.get(skey) or ''
            st.save_message(m, sender_name=sname, chat_name=chat_name,
                            direction='out' if uid == str(auth.user_id) else 'in')

        async def prepare_context(msgs: list):
            """交给 agent 前补齐可能缺失的上下文: 话题根消息、被引用消息,以及尚未同步的最近聊天记录(均只读,不标已读)。"""
            if not msgs:
                return
            last = msgs[-1]
            chat_id = str(last.get('chat_id') or '')
            if not chat_id:
                return
            chat_name = name_cache.get(chat_id) or st.get_chat_name(chat_id) or ''
            want_ids = []
            for m in msgs:
                mid_self = str(m.get('msg_id') or '')
                for k in ('root_id', 'anchor', 'parent_id'):
                    mid = str(m.get(k) or '')
                    if mid and mid != mid_self and mid not in want_ids and not st.get_message(mid, chat_id):
                        want_ids.append(mid)
            if want_ids:
                try:
                    for fm in await asyncio.to_thread(client.messages_by_ids, want_ids):
                        await _save_fetched(fm, chat_id, chat_name)
                except Exception as e:
                    log.debug(f'补拉根消息/引用消息失败(忽略): {e}')
            # 若该会话在此消息之前本地没有任何记录(比如刚启动监听就在旧群/旧话题里 /ai),只读补拉最近 30 条
            until = max(int(m.get('create_time') or 0) for m in msgs) or None
            trigger_ids = {str(m.get('msg_id')) for m in msgs if m.get('msg_id')}
            pos = int(last.get('position') or 0)
            if pos > 1:
                chat_existing = st.context_messages(chat_id, None, None, until, len(trigger_ids) + 1)
                if not any(str(r.msg_id) not in trigger_ids for r in chat_existing):
                    try:
                        start = max(1, pos - 30)
                        for pm in await asyncio.to_thread(client.pull_history, chat_id, list(range(start, pos))):
                            await _save_fetched(pm, chat_id, chat_name)
                    except Exception as e:
                        log.debug(f'补拉最近历史失败(忽略): {e}')

        if self.agent:
            backend = get_backend(storage=st, cfg=cfg, account_id=self.account_id)

            async def reply_fn(chat_id, text, root_id=None, quote_id=None):
                msg_id = await asyncio.to_thread(client.send_msg, text, chat_id, root_id, quote_id)
                # 回执也要落库,否则在「和自己的会话」里它的回显会被当成用户发的,又触发一轮 agent
                if msg_id:
                    st.save_sent(msg_id, chat_id, text, auth.user_id, root_id=root_id)
                return msg_id
            self.dispatcher = AgentDispatcher(backend, reply_fn, cfg=cfg, account_id=self.account_id, storage=st)
            self.dispatcher.denied = lambda msg: self._deny(msg, reply_fn)
            self.dispatcher.accepted = self._ack
            self.dispatcher.handled = self._handled
            self.dispatcher.prepare_context = prepare_context

        async def on_message(msg, catchup=False):
            """返回这条是不是第一次入库。catchup: 补收回来的,发送时间太久的只入库不交给 agent。"""
            chat_id = msg['chat_id']
            chat_type = msg.get('chat_type', 0)
            is_own = str(msg['from_id']) == str(auth.user_id)
            sender_key = (msg['from_id'], chat_id)
            if sender_key not in name_cache:
                name_cache[sender_key] = await asyncio.to_thread(client.get_user_name, msg['from_id'], chat_id)
            sender_name = name_cache.get(sender_key) or ''
            if chat_id not in name_cache:
                if chat_type in (2, 3):
                    name_cache[chat_id] = await asyncio.to_thread(client.get_group_name, chat_id)
                    if name_cache[chat_id]:
                        st.update_chat_name(chat_id, name_cache[chat_id], chat_type)
                elif chat_type == 1 and not is_own and sender_name:
                    name_cache[chat_id] = sender_name
                    st.update_chat_name(chat_id, sender_name, chat_type)
            chat_name = name_cache.get(chat_id) or ''
            if not chat_name and chat_type == 1:
                chat_name = st.get_chat_name(chat_id) or ''
                if chat_name:
                    name_cache[chat_id] = chat_name
            stale = catchup and is_stale(msg)
            typed_in_self_chat = False
            typed_self_trigger = False
            if is_own and self.dispatcher and not stale:
                t = cfg.get('triggers') or {}
                prefix = t.get('prefix') or ''
                content_stripped = (msg.get('content') or '').strip()
                want_self_chat = bool(chat_type == 1 and t.get('self_chat_enabled') and await asyncio.to_thread(client.is_self_chat, chat_id))
                want_self_trigger = bool((not want_self_chat) and (t.get('trigger_by') or 'others') in ('owner', 'all') and prefix and content_stripped.startswith(prefix))
                if want_self_chat or want_self_trigger:
                    # agent 的回复(lark send)和网页发的消息都按服务端 id 先落库;等落完,库里没有的才是用户亲手发的。
                    # 这一步挡的是死循环: agent 在这个会话里回复,回显回来不能再交给 agent
                    if not st.message_exists(msg['msg_id'], chat_id):
                        await asyncio.sleep(self.SELF_ECHO_WAIT)
                    if not st.message_exists(msg['msg_id'], chat_id):
                        typed_in_self_chat = want_self_chat
                        typed_self_trigger = want_self_trigger
            saved = st.save_message(msg, sender_name=sender_name, chat_name=chat_name,
                                    direction='out' if is_own else 'in')
            if saved:
                where = chat_name or chat_id
                content = msg['content'] or ''
                shown = content[:80] + ('…' if len(content) > 80 else '')
                line = f"[{where}] {sender_name or msg['from_id']}: {shown}"
                if is_own and sys.stderr.isatty():
                    line = f'\x1b[36m{line}\x1b[0m'
                log.info(line)
                # 补收回来的旧消息只入库,也不交给 agent。已读不在这里标: 只有 agent 回复过的消息才标(见 _handled)
                if self.dispatcher and not stale:
                    await self.dispatcher.submit(msg, chat_name=chat_name, sender_name=sender_name, my_user_id=auth.user_id,
                                                 self_chat=typed_in_self_chat, self_trigger=typed_self_trigger)
            return saved

        self._on_message, self._auth = on_message, auth
        # 上次停止前最后一条消息之后的都要补收一遍(最多往前 1 天);库里还没有消息的新账号不补
        last = st.latest_message_time()
        self._catchup_from = max(last, time.time() - 86400) if last else None
        client._ensure_loop()
        self._background = asyncio.run_coroutine_threadsafe(self._background_loop(), client.loop)
        down_at = None

        def connected():
            self._set('running')
            if down_at is not None and self._catchup_from is None:
                self._catchup_from = down_at

        while True:
            self._set('connecting' if self.state != 'reconnecting' else 'reconnecting', self.error)
            reason = ''
            try:
                await client.connect_websocket(on_message, on_connected=connected)
                reason, wait = 'WS 被服务端关闭', self.CLOSED_WAIT
            except AuthExpired:
                raise
            except Exception as e:
                reason, wait = f'WS 断开: {e}', self.ERROR_WAIT
            down_at = down_at if self.state == 'reconnecting' else time.time()
            self._set('reconnecting', reason)
            log.warning(f'{reason},{wait}s 后重连')
            await asyncio.sleep(wait)

    # ---- 后台:补收、定时任务、每日回顾(都跑在 client.loop 上,和 dispatcher 同一个循环) ----
    TICK = 10
    CATCHUP_EVERY = 900        # 兜底:每 15 分钟核对一次,补上长连接漏推的
    CATCHUP_MAX = 500          # 每轮最多补拉多少条
    CATCHUP_PER_CHAT = 200     # 每个会话最多补最近这么多条
    CATCHUP_SLACK = 300        # 本机和服务端的时钟可能差一点,定起点时往前多留 5 分钟

    async def _background_loop(self):
        if self.dispatcher:
            try:
                await self.dispatcher.restore()
            except Exception as e:
                self.log.warning(f'恢复上次没处理完的消息失败: {e}')
        last_check = time.time()
        while True:
            await asyncio.sleep(self.TICK)
            try:
                if self._catchup_from is not None and self.state == 'running':
                    since, self._catchup_from = self._catchup_from, None
                    await self._catch_up(since)
                    last_check = time.time()
                elif time.time() - last_check >= self.CATCHUP_EVERY and self.state == 'running':
                    await self._catch_up(last_check - 60)
                    last_check = time.time()
                if self.dispatcher:
                    await self._run_due_tasks()
                    await self._maybe_review()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.warning(f'后台任务出错: {e}')

    async def _catch_up(self, since: float):
        """核对 since 之后有更新的会话,把 WS 没推到的消息拉回来入库(太久以前的只入库不回复)。
        每个会话从 since 之前本地最后一条往后数,只拉本地缺的 position。"""
        st, client = self.storage, self.client
        chats = await asyncio.to_thread(client.feed_chats, since - 60)
        if not chats:
            return
        latest = await asyncio.to_thread(client.last_positions, chats)
        todo = []
        for chat_id, server_pos in latest.items():
            base, have = st.catchup_range(chat_id, since - self.CATCHUP_SLACK)
            # 本地没有更早记录的会话(断线期间新出现的)只补最近几条
            start = max(base + 1 if base else server_pos - 19, server_pos - self.CATCHUP_PER_CHAT + 1, 1)
            missing = [p for p in range(start, server_pos + 1) if p not in have]
            if missing:
                todo.append((chat_id, missing))
        budget, got = self.CATCHUP_MAX, 0
        for chat_id, missing in sorted(todo, key=lambda t: len(t[1])):
            missing = missing[-budget:]
            for i in range(0, len(missing), 50):
                msgs = await asyncio.to_thread(client.pull_history, chat_id, missing[i:i + 50])
                for m in msgs:
                    if m.get('from_id') and await self._on_message(m, catchup=True):
                        got += 1
            budget -= len(missing)
            if budget <= 0:
                break
        if got:
            self.log.info(f'补收到 {got} 条长连接没推送的消息')

    async def _run_due_tasks(self):
        now = datetime.now()
        for task in self.storage.task_list():
            verdict = tasks.due(task, now)
            if verdict == 'expire':
                self.storage.task_remove(task.id)
                self.log.info(f'定时任务 #{task.id} 已过期超过 1 小时,不再执行: {task.content[:40]}')
            elif verdict == 'run':
                if task.kind == 'daily':
                    self.storage.task_reschedule(task.id, int(tasks.next_daily(task.daily_at, now).timestamp()))
                else:
                    self.storage.task_remove(task.id)
                self.log.info(f'定时任务 #{task.id} 到点: {task.content[:40]}')
                await self.dispatcher.enqueue_task(task)

    async def _maybe_review(self):
        """每日回顾:到了设定的时间、一小时内、今天还没做过。账号那会儿没在运行就跳过,不补。"""
        at = self.cfg.get('memory_review_at') or ''
        if not (self.cfg.get('memory_enabled') and at):
            return
        try:
            due_at = datetime.combine(date.today(), datetime.strptime(at, '%H:%M').time())
        except ValueError:
            return
        now = datetime.now()
        state = accounts.read_meta(self.account_id).get('memory_review_last') or ''
        if due_at <= now < due_at + timedelta(hours=1) and state != date.today().isoformat():
            accounts.update_meta(self.account_id, memory_review_last=date.today().isoformat())
            await self._run_review()

    async def _run_review(self) -> str:
        # 有独立记忆的会话不进共用记忆的回顾
        own = (self.cfg.get('triggers') or {}).get('include_chats') or []
        transcript = memory.review_transcript(self.storage, int(time.time()) - 86400, self._auth.user_id, exclude_chats=own)
        if not transcript:
            self.log.info('每日回顾: 最近 24 小时没有聊天,跳过')
            return 'empty'
        if not await self.dispatcher.enqueue_review(transcript):
            self.log.info('每日回顾: 上一次还没结束,这次跳过')
            return 'busy'
        self.log.info('开始每日回顾,整理长期记忆')
        return 'started'

    async def _deny(self, msg: dict, reply_fn):
        """名单外的人触发了 agent:回一句设置好的话。上一条刚回过同样的话就不再回,免得和对方的自动回复来回刷。"""
        text = (self.cfg.get('deny_reply') or '').strip()
        chat_id = str(msg['chat_id'])
        if not text or self.storage.last_out_content(chat_id) == text:
            return
        anchor = msg.get('anchor') or ''
        await reply_fn(chat_id, text, anchor or None, None if anchor else msg.get('msg_id'))

    async def _handled(self, msg: dict):
        """「处理后标记已读」: agent 在这个会话里回复过才把对方这条标成已读;
        没触发 agent、agent 决定不回的消息保持未读,手机上照常有未读提醒。"""
        if not (self.cfg.get('mark_read') and msg.get('position') and msg.get('msg_id')):
            return
        if self._auth and str(msg.get('from_id')) == str(self._auth.user_id):
            return
        chat_id = str(msg.get('chat_id'))
        try:
            if await asyncio.to_thread(self.storage.has_outgoing_since, chat_id, int(msg.get('create_time') or 0)):
                await asyncio.to_thread(self.client.mark_read, chat_id, msg['position'], [msg['msg_id']])
        except Exception as e:
            self.log.debug(f'标记已读失败: {e}')

    def _ack(self, msg: dict):
        """收到要处理的消息先点个 👌。注意:点在别人的消息上,那条消息会变成已读。"""
        if not (self.cfg.get('ack_reaction') and msg.get('msg_id')):
            return

        async def go():
            try:
                await asyncio.to_thread(self.client.add_reaction, msg['msg_id'])
            except Exception as e:
                self.log.debug(f'点 👌 失败: {e}')
        asyncio.ensure_future(go())
