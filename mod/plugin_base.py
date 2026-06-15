"""
插件基类 — 所有插件必须继承 PluginBase

使用方式:
    from mod.plugin_base import PluginBase
    from mod import on_message, on_intent

    class MyPlugin(PluginBase):
        @on_message(priority=100)
        async def handle(self, event):
            event.consume()
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from .constants import PRIORITY_DEFAULT

log = logging.getLogger("gw.mod.pluginbase")


# ── Handler 元数据（装饰器附加到方法上的标记） ────────────

@dataclass
class HandlerMeta:
    """单个 handler 的元数据"""
    event_type: str
    priority: int
    method_name: str


# ── 事件对象（传递给 handler） ─────────────────────────────

@dataclass
class MessageEvent:
    """原始消息事件（群消息到达，JSON 尚未解析）"""
    group_id: str
    user_id: str
    raw_data: dict
    raw_message: str
    message: list
    consumed: bool = False

    def consume(self):
        """阻止后续 handler 和默认处理流程。"""
        self.consumed = True


@dataclass
class ParsedMessageEvent:
    """消息解析完成事件（text/img_urls/at/reply 已提取）"""
    group_id: str
    user_id: str
    text: str
    img_urls: list
    at_bot: bool
    reply_to: str | None
    consumed: bool = False

    def consume(self):
        self.consumed = True


@dataclass
class IntentEvent:
    """意图路由完成事件"""
    group_id: str
    user_id: str
    text: str
    intent: str
    confidence: str
    params: dict
    consumed: bool = False

    def consume(self):
        self.consumed = True


@dataclass
class ReplyEvent:
    """即将发送的回复事件"""
    group_id: str
    user_id: str
    text: str
    message_segments: list = field(default_factory=list)
    consumed: bool = False

    def consume(self):
        self.consumed = True


# ── 装饰器定义 ────────────────────────────────────────────

def on_message(priority: int = PRIORITY_DEFAULT):
    """监听所有群原始消息"""
    def decorator(func):
        _attach_handler(func, "message", priority)
        return func
    return decorator


def on_parsed_message(priority: int = PRIORITY_DEFAULT):
    """监听解析后的消息"""
    def decorator(func):
        _attach_handler(func, "message_parsed", priority)
        return func
    return decorator


def on_intent(intent: str = None, priority: int = PRIORITY_DEFAULT):
    """
    监听特定意图。
    intent=None 时监听所有意图路由事件。
    intent="chat" 时仅监听聊天意图。
    """
    def decorator(func):
        _attach_handler(func, f"intent.{intent}" if intent else "intent_resolved", priority)
        return func
    return decorator


def on_command(cmd: str, priority: int = PRIORITY_DEFAULT):
    """监听 /cmd 命令"""
    def decorator(func):
        _attach_handler(func, f"cmd.{cmd.lower()}", priority)
        return func
    return decorator


def on_regex(pattern: str, priority: int = PRIORITY_DEFAULT):
    """正则匹配消息（内部通过 message_parsed 事件实现）"""
    # 存储 pattern 在 handler 上
    def decorator(func):
        _attach_handler(func, "regex", priority, pattern=pattern)
        return func
    return decorator


def _attach_handler(func, event_type: str, priority: int,
                    **extra):
    """给方法附加 handler 元数据。"""
    if not hasattr(func, '_plugin_handlers'):
        func._plugin_handlers = []
    func._plugin_handlers.append(HandlerMeta(
        event_type=event_type,
        priority=priority,
        method_name=func.__name__,
    ))
    for k, v in extra.items():
        setattr(func, f'_plugin_{k}', v)
    return func


# ── 插件基类 ──────────────────────────────────────────────

class PluginBase:
    """
    所有插件必须继承自此类。

    子类可覆盖:
        async def initialize(self)     — 启动时初始化
        async def terminate(self)      — 卸载时清理

    通过 self.context 访问 PluginContext。
    """

    # PluginManager 通过这两个属性追踪插件类
    _pending_classes: list[type] = []

    def __init_subclass__(cls, **kwargs):
        """自动注册：任何 PluginBase 子类定义时自动记录。"""
        super().__init_subclass__(**kwargs)
        PluginBase._pending_classes.append(cls)
        log.debug("PLUGIN_PENDING: %s (module=%s)", cls.__name__, cls.__module__)

    def __init__(self, context):
        self.context = context
        self.log = logging.getLogger(f"plugin.{self.name}")

    @property
    def name(self) -> str:
        """插件名称，由 PluginManager 在注册时设置。"""
        return getattr(self, '_plugin_name', self.__class__.__name__)

    # ── 生命周期钩子（子类可覆盖） ──────────────────────

    async def initialize(self):
        """异步初始化。插件加载后调用。"""
        pass

    async def terminate(self):
        """异步清理。插件卸载/重载/关闭时调用。"""
        pass

    # ── handler 收集 ──────────────────────────────────────

    @classmethod
    def collect_handlers(cls) -> list[HandlerMeta]:
        """
        扫描类中所有带有 _plugin_handlers 的方法。
        返回 HandlerMeta 列表。
        """
        handlers = []
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name, None)
            if attr is None:
                continue
            metas = getattr(attr, '_plugin_handlers', None)
            if metas:
                handlers.extend(metas)
        return handlers

    @classmethod
    def collect_regex_patterns(cls) -> list[tuple[str, int, str]]:
        """
        扫描类中所有正则 handler。
        返回 [(pattern, priority, method_name), ...]
        """
        patterns = []
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name, None)
            if attr is None:
                continue
            metas = getattr(attr, '_plugin_handlers', None)
            pattern = getattr(attr, '_plugin_pattern', None)
            if metas and pattern:
                for m in metas:
                    if m.event_type == "regex":
                        patterns.append((pattern, m.priority, m.method_name))
        return patterns
