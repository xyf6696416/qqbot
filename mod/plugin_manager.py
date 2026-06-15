"""
PluginManager — 插件生命周期全权管理

职责:
  1. 扫描 mod/plugins/ 目录发现插件
  2. 读取 metadata.yaml，导入 main.py（触发 __init_subclass__）
  3. 实例化插件，注册 handler 到 EventBus
  4. 调用 initialize / terminate
  5. 卸载 / 热重载
"""

import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

from .constants import EventType, PRIORITY_DEFAULT
from .context import PluginContext
from .event_bus import EventBus
from .plugin_base import PluginBase, HandlerMeta

log = logging.getLogger("gw.mod.manager")


class PluginManager:
    """插件管理器，单例持有于 Bot 实例中。"""

    def __init__(self, bot_ref, plugins_dir: str = None):
        self._bot = bot_ref
        self._bus = EventBus()

        # 插件扫描目录
        if plugins_dir is None:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            plugins_dir = os.path.join(base, "mod", "plugins")
        self._plugins_dir = plugins_dir
        self._plugins_dir_abs = os.path.abspath(plugins_dir)

        # 已加载的插件实例: {plugin_name: plugin_instance}
        self._instances: dict[str, PluginBase] = {}

        # 已加载的插件元数据: {plugin_name: metadata_dict}
        self._metadatas: dict[str, dict] = {}

        # 自动注册创建的模块列表（用于清理）
        self._imported_modules: list[str] = []

        self._loaded = False

    # ── 属性 ────────────────────────────────────────────

    @property
    def plugins(self) -> dict[str, PluginBase]:
        """返回 {name: instance} 字典"""
        return dict(self._instances)

    @property
    def plugin_list(self) -> list[dict]:
        """返回插件信息列表（供 Web 面板使用）"""
        result = []
        for name, inst in self._instances.items():
            meta = self._metadatas.get(name, {})
            handlers = []
            for event_type, entries in self._bus.stats.items():
                for h in entries:
                    if h.plugin_name == name:
                        handlers.append(event_type)
            result.append({
                "name": name,
                "display_name": meta.get("display_name", name),
                "version": meta.get("version", "?"),
                "author": meta.get("author", "?"),
                "description": meta.get("description", ""),
                "enabled": meta.get("enabled", True),
                "loaded": True,
                "handlers": list(set(handlers)),
            })
        return result

    # ── 加载流程 ────────────────────────────────────────

    async def load_all(self) -> int:
        """
        扫描 plugins_dir 并加载所有插件。
        返回成功加载数。
        """
        if self._loaded:
            log.warning("PLUGIN_MGR: already loaded")
            return len(self._instances)

        plugins_dir = self._plugins_dir_abs
        if not os.path.isdir(plugins_dir):
            log.info("PLUGIN_MGR: plugins dir not found: %s", plugins_dir)
            os.makedirs(plugins_dir, exist_ok=True)
            self._loaded = True
            return 0

        # 清空 pending 队列（防止旧数据干扰）
        PluginBase._pending_classes.clear()

        # 扫描子目录
        count = 0
        for entry in sorted(os.listdir(plugins_dir)):
            plugin_dir = os.path.join(plugins_dir, entry)
            if not os.path.isdir(plugin_dir):
                continue
            # 跳过 __pycache__ 等特殊目录
            if entry.startswith("__"):
                continue
            if entry.startswith("."):
                continue

            try:
                ok = await self._load_plugin(entry)
                if ok:
                    count += 1
            except Exception as e:
                log.error("PLUGIN_LOAD_FAIL: %s err=%s", entry, str(e)[:200],
                          exc_info=True)

        self._loaded = True
        log.info("PLUGIN_MGR: loaded %d plugin(s)", count)
        return count

    async def _load_plugin(self, plugin_dir_name: str) -> bool:
        """
        加载单个插件目录。
        流程: 读 metadata → import → 绑定 class → 实例化 → 注册 handler → initialize
        """
        plugin_dir = os.path.join(self._plugins_dir_abs, plugin_dir_name)

        # 1. 读 metadata.yaml
        meta = self._read_metadata(plugin_dir)
        if meta is None:
            log.warning("PLUGIN_SKIP: %s has no metadata.yaml", plugin_dir_name)
            return False

        plugin_name = meta.get("name", plugin_dir_name)
        enabled = meta.get("enabled", True)
        if not enabled:
            log.info("PLUGIN_SKIP: %s disabled in metadata", plugin_name)
            return False

        # 2. 确保插件目录在 sys.path 中（使 from . import 等可用）
        #    实际不需要——我们用 importlib 直接加载 main.py
        main_py = os.path.join(plugin_dir, "main.py")
        if not os.path.isfile(main_py):
            log.warning("PLUGIN_SKIP: %s has no main.py", plugin_name)
            return False

        # 3. import main.py
        #    构造唯一模块名：mod.plugins.<plugin_dir_name>.main
        module_name = f"_plugin_{plugin_dir_name}"
        spec = importlib.util.spec_from_file_location(module_name, main_py)
        if spec is None or spec.loader is None:
            log.warning("PLUGIN_SKIP: %s spec invalid", plugin_name)
            return False

        #   把插件目录加入 sys.path 以便插件内使用相对/绝对 import
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)

        mod = importlib.util.module_from_spec(spec)
        # 缓存模块名以便后续清理
        self._imported_modules.append(module_name)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)

        # 4. 查找 Pending 类
        plugin_class = self._find_plugin_class()
        if plugin_class is None:
            log.warning("PLUGIN_SKIP: %s no PluginBase subclass found", plugin_name)
            return False

        # 5. 创建 Context 和实例
        context = PluginContext(plugin_name, self._bot, self._plugins_dir_abs)
        instance = plugin_class(context)
        instance._plugin_name = plugin_name  # 显式设置名称

        # 6. 收集 handler 元数据
        handler_metas = plugin_class.collect_handlers()
        regex_patterns = plugin_class.collect_regex_patterns()

        # 7. 注册 handler 到 EventBus
        self._bus.register_plugin_handlers(
            plugin_name, instance, handler_metas)

        # 如果插件有正则 handler，注册到 message_parsed 事件
        if regex_patterns:
            await self._register_regex_handlers(plugin_name, instance, regex_patterns)

        # 8. 调用 initialize()
        try:
            await instance.initialize()
        except Exception as e:
            log.error("PLUGIN_INIT_ERR: %s %s", plugin_name, str(e)[:200])
            # initialize 失败不阻止加载，但标记一下

        # 9. 存储
        self._instances[plugin_name] = instance
        self._metadatas[plugin_name] = meta

        # 发布插件加载事件
        await self._bus.emit(EventType.PLUGIN_LOADED, {"name": plugin_name})

        log.info("PLUGIN_LOADED: %s v%s by %s (%d handlers, %d regex)",
                 plugin_name,
                 meta.get("version", "?"),
                 meta.get("author", "?"),
                 len(handler_metas),
                 len(regex_patterns))
        return True

    def _read_metadata(self, plugin_dir: str) -> dict | None:
        """读取插件目录下的 metadata.yaml。"""
        yaml_path = os.path.join(plugin_dir, "metadata.yaml")
        if not os.path.isfile(yaml_path):
            return None
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                meta = yaml.safe_load(f)
            if not isinstance(meta, dict):
                return None
            return meta
        except Exception as e:
            log.warning("PLUGIN_META_ERR: %s err=%s",
                        os.path.basename(plugin_dir), str(e)[:80])
            return None

    def _find_plugin_class(self) -> type | None:
        """
        从 _pending_classes 中找最新加入且模块路径不在 mod 内部的类。
        由于我们刚刚 import，新类应该是 _pending_classes 中最后一个。
        """
        # 从后往前找，取第一个不是内部基类的
        for cls in reversed(PluginBase._pending_classes):
            # 排除 PluginBase 本身
            if cls is PluginBase:
                continue
            # 排除 mod/ 内部类（间接子类测试等）
            mod_name = getattr(cls, '__module__', '')
            if mod_name.startswith('_plugin_'):
                return cls
        return None

    async def _register_regex_handlers(self, plugin_name: str,
                                        instance: PluginBase,
                                        patterns: list[tuple[str, int, str]]):
        """为插件的 @on_regex 注册 handler。"""
        import re as _re

        async def make_regex_handler(pattern: str, method_name: str):
            compiled = _re.compile(pattern)
            async def handler(event):
                if not hasattr(event, 'text') or not event.text:
                    return
                m = compiled.search(event.text)
                if m:
                    method = getattr(instance, method_name, None)
                    if method:
                        await method(event, match=m)
            return handler

        for pattern, priority, method_name in patterns:
            handler_fn = await make_regex_handler(pattern, method_name)
            self._bus.on("message_parsed", handler_fn,
                         priority=priority, plugin_name=plugin_name)

    # ── 卸载 ────────────────────────────────────────────

    async def unload_plugin(self, plugin_name: str) -> bool:
        """卸载指定插件。"""
        instance = self._instances.pop(plugin_name, None)
        if instance is None:
            return False

        # 调用 terminate
        try:
            await instance.terminate()
        except Exception as e:
            log.warning("PLUGIN_TERM_ERR: %s %s", plugin_name, str(e)[:100])

        # 移除 EventBus 中的 handler
        self._bus.remove_plugin_handlers(plugin_name)

        # 清理 sys.modules 中的缓存（用于热重载）
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith(f"_plugin_{plugin_name}"):
                sys.modules.pop(mod_name, None)

        self._metadatas.pop(plugin_name, None)
        await self._bus.emit(EventType.PLUGIN_UNLOADED, {"name": plugin_name})

        log.info("PLUGIN_UNLOADED: %s", plugin_name)
        return True

    async def unload_all(self):
        """卸载所有插件。"""
        names = list(self._instances.keys())
        for name in names:
            await self.unload_plugin(name)
        self._bus.clear()
        PluginBase._pending_classes.clear()
        self._loaded = False
        log.info("PLUGIN_MGR: all plugins unloaded")

    # ── 重载 ────────────────────────────────────────────

    async def reload_plugin(self, plugin_name: str) -> bool:
        """热重载指定插件。"""
        await self.unload_plugin(plugin_name)
        # 找插件目录名（根据 metadata name 反向查找）
        for entry in os.listdir(self._plugins_dir_abs):
            plugin_dir = os.path.join(self._plugins_dir_abs, entry)
            if not os.path.isdir(plugin_dir):
                continue
            meta = self._read_metadata(plugin_dir)
            if meta and meta.get("name") == plugin_name:
                return await self._load_plugin(entry)
        return False
