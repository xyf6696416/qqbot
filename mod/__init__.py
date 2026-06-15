"""
插件系统公共 API
"""

from .constants import EventType
from .plugin_base import (
    PluginBase,
    MessageEvent,
    ParsedMessageEvent,
    IntentEvent,
    ReplyEvent,
    HandlerMeta,
    on_message,
    on_parsed_message,
    on_intent,
    on_command,
    on_regex,
)
from .context import PluginContext
from .event_bus import EventBus
from .plugin_manager import PluginManager

__all__ = [
    # 基类
    "PluginBase",
    # 上下文
    "PluginContext",
    # 事件总线
    "EventBus",
    "EventType",
    # 插件管理器
    "PluginManager",
    # 事件对象
    "MessageEvent",
    "ParsedMessageEvent",
    "IntentEvent",
    "ReplyEvent",
    # 装饰器
    "on_message",
    "on_parsed_message",
    "on_intent",
    "on_command",
    "on_regex",
    # 元数据
    "HandlerMeta",
]
