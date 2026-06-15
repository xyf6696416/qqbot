"""
插件系统常量定义
"""

from enum import Enum, auto


class EventType(str, Enum):
    """事件类型枚举"""
    # 生命周期事件
    BOT_START = "bot_start"
    BOT_SHUTDOWN = "bot_shutdown"

    # 消息事件
    MESSAGE = "message"                   # 收到群消息（原始 JSON）
    MESSAGE_PARSED = "message_parsed"     # 消息已解析（text/img/at 就绪）

    # 路由事件
    INTENT_RESOLVED = "intent_resolved"   # 意图路由完成
    BEFORE_HANDLER = "before_handler"     # handler 即将执行
    AFTER_HANDLER = "after_handler"       # handler 已执行
    REPLY = "reply"                       # 即将发送回复

    # 插件生命周期
    PLUGIN_LOADED = "plugin_loaded"
    PLUGIN_UNLOADED = "plugin_unloaded"
    PLUGIN_ERROR = "plugin_error"


# 默认优先级
PRIORITY_SYSTEM = 0       # 系统预留
PRIORITY_HIGH = 50        # 高优先级
PRIORITY_DEFAULT = 100    # 默认
PRIORITY_LOW = 200        # 低优先级
PRIORITY_LAST = 999       # 最后
