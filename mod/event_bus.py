"""
事件总线 — 发布-订阅模式
- 按优先级调度 handler
- 支持 cancel（阻止后续 handler）
- 异常隔离：单个 handler 崩溃不影响其他
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from .constants import EventType, PRIORITY_DEFAULT

log = logging.getLogger("gw.mod.eventbus")


@dataclass
class EventResult:
    """事件执行结果"""
    consumed: bool = False
    results: list = field(default_factory=list)


HandlerFn = Callable[..., Coroutine[Any, Any, None]]


@dataclass
class HandlerEntry:
    """已注册的 handler"""
    handler: HandlerFn
    event_type: str
    priority: int
    plugin_name: str
    instance_ref: Any  # 弱引用保留给 PluginManager 校验，当前简单持有


class EventBus:
    """全局事件总线，单例。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._handlers: dict[str, list[HandlerEntry]] = {}
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._handlers: dict[str, list[HandlerEntry]] = {}
        self._initialized = True

    # ── 注册 ────────────────────────────────────────────

    def on(self, event_type: str, handler_fn: HandlerFn,
           priority: int = PRIORITY_DEFAULT, plugin_name: str = "?") -> None:
        """注册一个事件监听。"""
        entry = HandlerEntry(
            handler=handler_fn,
            event_type=event_type,
            priority=priority,
            plugin_name=plugin_name,
            instance_ref=None,
        )
        self._handlers.setdefault(event_type, []).append(entry)
        self._handlers[event_type].sort(key=lambda h: h.priority)
        log.debug("EVENT_REG: event=%s plugin=%s priority=%d",
                  event_type, plugin_name, priority)

    def register_plugin_handlers(self, plugin_name: str, instance: Any,
                                 handler_metas: list['HandlerMeta']) -> None:
        """
        批量注册某插件的所有 handler。
        handler_metas: [HandlerMeta(event_type, priority, method_name), ...]
        """
        for hm in handler_metas:
            # 从 instance 获取方法引用并绑定
            method = getattr(instance, hm.method_name, None)
            if method is None:
                log.warning("EVENT_REG_MISS: plugin=%s method=%s not found",
                            plugin_name, hm.method_name)
                continue
            entry = HandlerEntry(
                handler=method,
                event_type=hm.event_type,
                priority=hm.priority,
                plugin_name=plugin_name,
                instance_ref=instance,
            )
            self._handlers.setdefault(hm.event_type, []).append(entry)

        # 按优先级重排所有受影响的类型
        touched = set(hm.event_type for hm in handler_metas)
        for et in touched:
            self._handlers[et].sort(key=lambda h: h.priority)

        log.info("EVENT_REG_PLUGIN: plugin=%s handlers=%d",
                 plugin_name, len(handler_metas))

    # ── 移除 ────────────────────────────────────────────

    def remove_plugin_handlers(self, plugin_name: str) -> None:
        """移除某插件的所有 handler（卸载时调用）。"""
        removed = 0
        for event_type in list(self._handlers.keys()):
            before = len(self._handlers[event_type])
            self._handlers[event_type] = [
                h for h in self._handlers[event_type]
                if h.plugin_name != plugin_name
            ]
            removed += before - len(self._handlers[event_type])
            if not self._handlers[event_type]:
                del self._handlers[event_type]
        if removed:
            log.info("EVENT_REMOVE: plugin=%s handlers=%d", plugin_name, removed)

    # ── 发布 ────────────────────────────────────────────

    async def emit(self, event_type: str, event, *,
                   cancellable: bool = False) -> EventResult:
        """
        发布事件，按优先级调用 handler。
        cancellable=True 时，任一 handler 调用 event.consume() 即停止传播。
        """
        result = EventResult()
        entries = self._handlers.get(event_type, [])
        if not entries:
            return result

        log.debug("EVENT_EMIT: type=%s handlers=%d", event_type, len(entries))

        for entry in entries:
            if cancellable and result.consumed:
                break
            try:
                await entry.handler(event)
                result.results.append(True)
                if cancellable and hasattr(event, 'consumed') and event.consumed:
                    result.consumed = True
            except Exception as e:
                result.results.append(False)
                log.error("EVENT_HANDLER_ERR: plugin=%s event=%s err=%s",
                          entry.plugin_name, event_type, str(e)[:200],
                          exc_info=True)
                # 同时发布错误事件
                self._emit_error(event_type, entry.plugin_name, e)

        return result

    def _emit_error(self, event_type: str, plugin_name: str, error: Exception):
        """内部：发布插件错误事件（同步触发，不循环）。"""
        err_entries = self._handlers.get(EventType.PLUGIN_ERROR, [])
        for entry in err_entries:
            try:
                import asyncio
                if asyncio.iscoroutinefunction(entry.handler):
                    import asyncio as _a
                    # 不 await，仅记录
                    log.warning("PLUGIN_ERR_HANDLER_SKIP: %s", entry.plugin_name)
            except Exception:
                pass

    # ── 状态 ────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        return {
            et: len(entries) for et, entries in self._handlers.items()
        }

    def clear(self):
        """清空所有 handler（关闭时调用）。"""
        self._handlers.clear()
        log.info("EVENT_BUS: cleared")
