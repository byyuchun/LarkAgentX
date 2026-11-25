"""web API 的请求体。字段在这里严格校验,路由里只管业务。"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

IdList = Field(default_factory=list, max_length=200)
Region = Literal['feishu', 'lark']


class AccountCreate(BaseModel):
    name: str = Field('', max_length=40)
    region: Region = 'feishu'


class AddAccount(BaseModel):
    region: Region = 'feishu'


class LoginStart(BaseModel):
    region: Optional[Region] = None


class CookieLogin(BaseModel):
    cookie: str = Field(..., min_length=10, max_length=20000)
    region: Optional[Region] = None


class TriggersIn(BaseModel):
    prefix: str = Field('', max_length=64)
    draft_prefix: str = Field('/me', max_length=64)
    draft_auto_recall: bool = True
    chat_types: List[Literal['p2p', 'group', 'topic_group']] = IdList
    include_chats: List[str] = IdList
    exclude_chats: List[str] = IdList
    include_senders: List[str] = IdList
    exclude_senders: List[str] = IdList
    keywords: List[str] = IdList
    group_at_only: bool = False
    dm_enabled: bool = True              # 别人私聊我时是否处理
    self_chat_enabled: bool = False      # 「和自己的会话」里亲手发的是否处理
    trigger_by: Literal['others', 'owner', 'all'] = 'others'   # 触发对象: 仅他人 | 仅自己(需前缀) | 自己和他人
    topic_followup: bool = True          # 本人带前缀唤起话题后,话题内他人的后续提问自动跟进
    followup_read_only: bool = True      # 话题内他人追问触发时只读保护本地代码


class ChatConfigIn(BaseModel):
    """「只处理这些会话」里一个会话(私聊或群聊)单独的设置。记忆不在这里,走 /memory?chat= 接口。"""
    chat_id: str = Field(..., min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    workdir: str = Field('', max_length=500)
    senders: List[str] = Field(default_factory=list, max_length=200)   # 群聊里只回复这些人,空 = 所有人
    notes: str = Field('', max_length=2000)                            # 会话说明: 每次换新内容时附给 agent


class WatchRuleIn(BaseModel):
    """静默监控规则: 监听指定群的特定关键词/发送者,命中后由 agent 整理并仅私发到「和自己的会话」,绝不往原群发言。"""
    chat_id: str = Field(..., min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    name: str = Field('', max_length=120)
    keywords: List[str] = Field(default_factory=list, max_length=50)
    senders: List[str] = Field(default_factory=list, max_length=50)
    prompt: str = Field('', max_length=4000)
    enabled: bool = True


class SettingsIn(BaseModel):
    remark: str = Field('', max_length=40)                # 备注名,可留空(留空显示飞书里的姓名)
    autostart: bool = False
    agent_backend: Literal['claude', 'codex', 'trae', 'doubao', 'pi', 'opencode', 'cursor', 'agy', 'none']
    # 模型名会作为命令行参数传给 agent CLI,不允许以 - 开头,免得被当成别的开关
    agent_model: str = Field('', max_length=100, pattern=r'^([A-Za-z0-9][A-Za-z0-9_.:/-]*)?$')
    agent_effort: Literal['', 'low', 'medium', 'high', 'xhigh', 'max'] = ''
    workdir: str = Field('', max_length=500)
    extra_workdirs: List[str] = Field(default_factory=list, max_length=50)
    agent_timeout: int = Field(7200, ge=0, le=86400)        # 秒;0 = 不限
    system_prompt: str = Field('', max_length=8000)
    context_scope: Literal['anchor', 'chat', 'global'] = 'anchor'
    mark_read: bool = True
    reply_in_topic: bool = True                           # 话题里的消息: 回到话题 / 发到外面的主会话
    reply_mode: Literal['direct', 'quote', 'thread'] = 'direct'   # 主会话的消息: 直接发 / 引用 / 开话题
    reply_thread_on_quote: bool = True                    # 开话题回复时,引用了别人消息则挂在被引用消息下开话题
    reply_signature: str = Field('', max_length=100)      # 加在 agent 发的消息末尾
    ack_reaction: bool = False       # 交给 agent 的消息先点个 👌
    deny_reply: str = Field('', max_length=500)           # 不在「只处理这些发送者」里的人触发时回的话
    memory_enabled: bool = False
    memory_review_at: str = Field('00:00', pattern=r'^(([01]\d|2[0-3]):[0-5]\d)?$')   # 每日回顾时间,空 = 不回顾
    triggers: TriggersIn = Field(default_factory=TriggersIn)
    chat_configs: List[ChatConfigIn] = Field(default_factory=list, max_length=200)
    watch_rules: List[WatchRuleIn] = Field(default_factory=list, max_length=50)


class SendIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000)
    root_id: Optional[str] = Field(None, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


class HistoryIn(BaseModel):
    count: int = Field(50, ge=1, le=200)


class ChatAgentIn(BaseModel):
    enabled: bool


class ResetIn(BaseModel):
    anchor: str = Field('', max_length=64)


class MemoryIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class LoginIn(BaseModel):
    token: str = Field(..., max_length=500)
