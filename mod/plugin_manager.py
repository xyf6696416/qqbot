"""
PluginManager — 插件生命周期全权管理

职责:
  1. 扫描 mod/plugins/ 目录发现插件
  2. 读取 metadata.yaml，导入 main.py（触发 __init_subclass__）
  3. 实例化插件，注册 handler 到 EventBus
  4. 调用 initialize / terminate
  5. 卸载 / 热重载 / 定时扫描
"""

import importlib
import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

from .constants import EventType
from .context import PluginContext
from .event_bus import EventBus
from .plugin_base import PluginBase

log = logging.getLogger("gw.mod.manager")

# 插件命令标记文件（供 config_web.py 跨进程通信）
PLUGIN_CMD_FLAG = None  # 在 Bot.init 时设置为绝对路径


def _resolve_cmd_flag():
    """解析插件命令标记文件路径（懒加载）。"""
    global PLUGIN_CMD_FLAG
    if PLUGIN_CMD_FLAG is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        PLUGIN_CMD_FLAG = os.path.join(base, ".plugin_cmd.json")
    return PLUGIN_CMD_FLAG


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

        # 插件目录名 → plugin_name 映射（用于卸载时查 module）
        self._dir_to_name: dict[str, str] = {}

        # mtime 跟踪: {plugin_name: {main_mtime, meta_mtime, dir_mtime}}
        self._mtimes: dict[str, dict] = {}

        self._loaded = False

    # ── 属性 ────────────────────────────────────────────

    @property
    def plugins(self) -> dict[str, PluginBase]:
        return dict(self._instances)

    @property
    def metadatas(self) -> dict[str, dict]:
        return dict(self._metadatas)

    @property
    def plugin_list(self) -> list[dict]:
        """返回插件信息列表（供 Web 面板使用）。"""
        result = []
        for name, inst in self._instances.items():
            meta = self._metadatas.get(name, {})
            handlers = self._get_plugin_handler_types(name)
            result.append({
                "name": name,
                "dir": self._find_plugin_dir(name),
                "display_name": meta.get("display_name", name),
                "version": meta.get("version", "?"),
                "author": meta.get("author", "?"),
                "description": meta.get("description", ""),
                "enabled": meta.get("enabled", True),
                "loaded": True,
                "handlers": sorted(handlers),
            })
        # 补充已发现但未加载的插件
        seen_names = {r["name"] for r in result}
        for entry in sorted(os.listdir(self._plugins_dir_abs)):
            plugin_dir = os.path.join(self._plugins_dir_abs, entry)
            if not os.path.isdir(plugin_dir) or entry.startswith(("__", ".")):
                continue
            meta = self._read_metadata(plugin_dir)
            if meta is None:
                continue
            pname = meta.get("name", entry)
            if pname in seen_names:
                continue
            result.append({
                "name": pname,
                "dir": entry,
                "display_name": meta.get("display_name", pname),
                "version": meta.get("version", "?"),
                "author": meta.get("author", "?"),
                "description": meta.get("description", ""),
                "enabled": meta.get("enabled", True),
                "loaded": False,
                "handlers": [],
            })
        return result

    def _get_plugin_handler_types(self, plugin_name: str) -> list[str]:
        """获取插件注册的事件类型列表。"""
        types = set()
        for event_type, entries in self._bus._handlers.items():
            for h in entries:
                if h.plugin_name == plugin_name:
                    types.add(event_type)
        return list(types)

    def get_plugin_info(self, name: str) -> dict | None:
        """获取单个插件的详细信息。"""
        for p in self.plugin_list:
            if p["name"] == name:
                return p
        return None

    # ── 加载流程 ────────────────────────────────────────

    async def load_all(self) -> int:
        """扫描 plugins_dir 并加载所有插件。"""
        if self._loaded:
            return len(self._instances)

        plugins_dir = self._plugins_dir_abs
        if not os.path.isdir(plugins_dir):
            os.makedirs(plugins_dir, exist_ok=True)
            self._loaded = True
            return 0

        PluginBase._pending_classes.clear()

        count = 0
        for entry in sorted(os.listdir(plugins_dir)):
            plugin_dir = os.path.join(plugins_dir, entry)
            if not os.path.isdir(plugin_dir) or entry.startswith(("__", ".")):
                continue
            try:
                if await self._load_plugin(entry):
                    count += 1
            except Exception as e:
                log.error("PLUGIN_LOAD_FAIL: %s err=%s", entry, str(e)[:200],
                          exc_info=True)

        self._loaded = True
        log.info("PLUGIN_MGR: loaded %d plugin(s)", count)
        return count

    async def _load_plugin(self, plugin_dir_name: str) -> bool:
        """加载单个插件目录。"""
        plugin_dir = os.path.join(self._plugins_dir_abs, plugin_dir_name)

        # 1. 读 metadata.yaml
        meta = self._read_metadata(plugin_dir)
        if meta is None:
            return False

        plugin_name = meta.get("name", plugin_dir_name)
        if not meta.get("enabled", True):
            log.info("PLUGIN_SKIP: %s disabled", plugin_name)
            return False

        main_py = os.path.join(plugin_dir, "main.py")
        if not os.path.isfile(main_py):
            log.warning("PLUGIN_SKIP: %s no main.py", plugin_name)
            return False

        # 2. Import main.py
        module_name = f"_plugin_{plugin_dir_name}"
        # 先清理旧模块缓存（热重载时有用）
        if module_name in sys.modules:
            del sys.modules[module_name]

        spec = importlib.util.spec_from_file_location(module_name, main_py)
        if spec is None or spec.loader is None:
            return False

        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)

        # 3. 查找 PluginBase 子类
        plugin_class = self._find_plugin_class()
        if plugin_class is None:
            log.warning("PLUGIN_SKIP: %s no PluginBase subclass", plugin_name)
            return False

        # 4. 创建实例
        context = PluginContext(plugin_name, self._bot, self._plugins_dir_abs)
        instance = plugin_class(context)
        instance._plugin_name = plugin_name

        # 5. 收集 & 注册 handler
        handler_metas = plugin_class.collect_handlers()
        regex_patterns = plugin_class.collect_regex_patterns()
        self._bus.register_plugin_handlers(plugin_name, instance, handler_metas)
        if regex_patterns:
            await self._register_regex_handlers(plugin_name, instance, regex_patterns)

        # 6. initialize()
        try:
            await instance.initialize()
        except Exception as e:
            log.error("PLUGIN_INIT_ERR: %s %s", plugin_name, str(e)[:200])

        # 7. 存储
        self._instances[plugin_name] = instance
        self._metadatas[plugin_name] = meta
        self._dir_to_name[plugin_dir_name] = plugin_name
        self._track_mtime(plugin_name, plugin_dir)

        await self._bus.emit(EventType.PLUGIN_LOADED, {"name": plugin_name})
        log.info("PLUGIN_LOADED: %s v%s (%d handlers, %d regex)",
                 plugin_name, meta.get("version", "?"),
                 len(handler_metas), len(regex_patterns))
        return True

    # ── mtime 跟踪（热加载用）──────────────────────────────

    def _track_mtime(self, plugin_name: str, plugin_dir: str):
        """记录插件文件的当前 mtime。"""
        main_py = os.path.join(plugin_dir, "main.py")
        meta_yaml = os.path.join(plugin_dir, "metadata.yaml")
        self._mtimes[plugin_name] = {
            "main_mtime": os.path.getmtime(main_py) if os.path.isfile(main_py) else 0,
            "meta_mtime": os.path.getmtime(meta_yaml) if os.path.isfile(meta_yaml) else 0,
            "dir_mtime": os.path.getmtime(plugin_dir) if os.path.isdir(plugin_dir) else 0,
            "dir": plugin_dir,
        }

    def _check_mtime_changed(self, plugin_name: str) -> bool:
        """检查插件文件是否有修改。"""
        info = self._mtimes.get(plugin_name)
        if info is None:
            return False
        plugin_dir = info["dir"]
        main_py = os.path.join(plugin_dir, "main.py")
        meta_yaml = os.path.join(plugin_dir, "metadata.yaml")
        try:
            if os.path.isfile(main_py) and os.path.getmtime(main_py) != info["main_mtime"]:
                return True
            if os.path.isfile(meta_yaml) and os.path.getmtime(meta_yaml) != info["meta_mtime"]:
                return True
            if os.path.isdir(plugin_dir) and os.path.getmtime(plugin_dir) != info["dir_mtime"]:
                return True
        except OSError:
            return True
        return False

    # ── 热加载扫描 ──────────────────────────────────────

    async def check_hot_reload(self) -> int:
        """
        扫描所有已加载插件和命令标记，执行热重载。
        返回重载/变更数。
        """
        reloaded = 0

        # 1) mtime 变更检查
        for plugin_name in list(self._instances.keys()):
            if self._check_mtime_changed(plugin_name):
                log.info("HOT_RELOAD: %s changed, reloading...", plugin_name)
                try:
                    ok = await self.reload_plugin(plugin_name)
                    if ok:
                        reloaded += 1
                        log.info("HOT_RELOAD_OK: %s", plugin_name)
                    else:
                        log.warning("HOT_RELOAD_FAIL: %s", plugin_name)
                except Exception as e:
                    log.error("HOT_RELOAD_ERR: %s %s", plugin_name, str(e)[:200])

        # 2) 检查新增/删除的插件目录
        await self._check_new_plugins()

        # 3) 读取命令标记（来自 config_web.py）
        cmd_count = await self._process_command_flag()
        reloaded += cmd_count

        return reloaded

    async def _check_new_plugins(self):
        """扫描是否有新增或删除的插件目录。"""
        # 已加载的目录名集合
        loaded_dirs = set()
        for dname, pname in list(self._dir_to_name.items()):
            plugin_dir = os.path.join(self._plugins_dir_abs, dname)
            if not os.path.isdir(plugin_dir):
                # 目录已被删除 → 卸载插件
                log.info("HOT_RELOAD: plugin dir removed, unloading %s", pname)
                await self.unload_plugin(pname)
                self._dir_to_name.pop(dname, None)
                self._mtimes.pop(pname, None)
            else:
                loaded_dirs.add(dname)

        # 扫描新增目录
        for entry in sorted(os.listdir(self._plugins_dir_abs)):
            if entry.startswith(("__", ".")):
                continue
            plugin_dir = os.path.join(self._plugins_dir_abs, entry)
            if not os.path.isdir(plugin_dir):
                continue
            if entry in loaded_dirs:
                continue
            # 新目录 → 尝试加载
            log.info("HOT_RELOAD: new plugin dir %s, loading...", entry)
            try:
                if await self._load_plugin(entry):
                    log.info("HOT_RELOAD_NEW_OK: %s", entry)
            except Exception as e:
                log.error("HOT_RELOAD_NEW_ERR: %s %s", entry, str(e)[:200])

    async def _process_command_flag(self) -> int:
        """读取并处理 .plugin_cmd.json 命令标记。"""
        flag_path = _resolve_cmd_flag()
        if not os.path.isfile(flag_path):
            return 0
        try:
            with open(flag_path, "r", encoding="utf-8") as f:
                cmds = json.load(f)
            if not isinstance(cmds, list):
                cmds = [cmds]
        except Exception as e:
            log.warning("PLUGIN_CMD_FLAG_ERR: %s", str(e)[:80])
            try:
                os.remove(flag_path)
            except Exception:
                pass
            return 0

        # 删除标记文件（防重复处理）
        try:
            os.remove(flag_path)
        except Exception:
            pass

        count = 0
        for cmd in cmds:
            action = cmd.get("action", "")
            name = cmd.get("plugin", "")
            if not name:
                continue
            try:
                if action == "reload":
                    ok = await self.reload_plugin(name)
                    log.info("PLUGIN_CMD_RELOAD: %s ok=%s", name, ok)
                    if ok:
                        count += 1
                elif action == "toggle":
                    # 切换启用/禁用
                    await self._toggle_plugin(name)
                    count += 1
            except Exception as e:
                log.error("PLUGIN_CMD_ERR: %s %s", name, str(e)[:100])
        return count

    async def _toggle_plugin(self, plugin_name: str):
        """切换插件的启用状态（修改 metadata.yaml + 卸载/加载）。"""
        plugin_dir = self._find_plugin_dir(plugin_name)
        if not plugin_dir:
            # 尝试按目录名查找
            for dname, pname in self._dir_to_name.items():
                if pname == plugin_name:
                    plugin_dir = dname
                    break
        if not plugin_dir:
            log.warning("TOGGLE: plugin dir not found for %s", plugin_name)
            return

        full_path = os.path.join(self._plugins_dir_abs, plugin_dir)
        meta_path = os.path.join(full_path, "metadata.yaml")

        # 读取当前元数据
        meta = self._read_metadata(full_path) or {}
        current = meta.get("enabled", True)

        if current and plugin_name in self._instances:
            # 禁用：卸载插件
            await self.unload_plugin(plugin_name)
            meta["enabled"] = False
            self._write_plugin_meta(meta_path, meta)
        elif not current and plugin_name not in self._instances:
            # 启用：加载插件
            meta["enabled"] = True
            self._write_plugin_meta(meta_path, meta)
            await self._load_plugin(plugin_dir)
        else:
            # 状态已符合要求，不需要操作
            pass

    # ── 元数据读取 ─────────────────────────────────────

    def _read_metadata(self, plugin_dir: str) -> dict | None:
        yaml_path = os.path.join(plugin_dir, "metadata.yaml")
        if not os.path.isfile(yaml_path):
            return None
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                meta = yaml.safe_load(f)
            return meta if isinstance(meta, dict) else None
        except Exception as e:
            log.warning("PLUGIN_META_ERR: %s %s",
                        os.path.basename(plugin_dir), str(e)[:80])
            return None

    def _write_plugin_meta(self, meta_path: str, meta: dict):
        """写回 metadata.yaml。"""
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                yaml.dump(meta, f, allow_unicode=True,
                          default_flow_style=False, sort_keys=False)
        except Exception as e:
            log.warning("META_WRITE_ERR: %s", str(e)[:80])

    def _find_plugin_class(self) -> type | None:
        for cls in reversed(PluginBase._pending_classes):
            if cls is PluginBase:
                continue
            mod_name = getattr(cls, '__module__', '')
            if mod_name.startswith('_plugin_'):
                return cls
        return None

    async def _register_regex_handlers(self, plugin_name, instance, patterns):
        import re as _re

        async def make_regex_handler(pattern, method_name):
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

    # ── 查找 ───────────────────────────────────────────

    def _find_plugin_dir(self, plugin_name: str) -> str | None:
        """根据插件名反查目录名。"""
        for entry in os.listdir(self._plugins_dir_abs):
            plugin_dir = os.path.join(self._plugins_dir_abs, entry)
            if not os.path.isdir(plugin_dir) or entry.startswith(("__", ".")):
                continue
            meta = self._read_metadata(plugin_dir)
            if meta and meta.get("name") == plugin_name:
                return entry
            if entry == plugin_name:
                return entry
        return None

    # ── 卸载 ───────────────────────────────────────────

    async def unload_plugin(self, plugin_name: str) -> bool:
        instance = self._instances.pop(plugin_name, None)
        if instance is None:
            return False

        try:
            await instance.terminate()
        except Exception as e:
            log.warning("PLUGIN_TERM_ERR: %s %s", plugin_name, str(e)[:100])

        self._bus.remove_plugin_handlers(plugin_name)

        # 清理模块缓存
        dir_name = next(
            (d for d, n in self._dir_to_name.items() if n == plugin_name),
            None
        )
        if dir_name:
            mod_name = f"_plugin_{dir_name}"
            sys.modules.pop(mod_name, None)
            self._dir_to_name.pop(dir_name, None)

        self._metadatas.pop(plugin_name, None)
        self._mtimes.pop(plugin_name, None)
        await self._bus.emit(EventType.PLUGIN_UNLOADED, {"name": plugin_name})
        log.info("PLUGIN_UNLOADED: %s", plugin_name)
        return True

    async def unload_all(self):
        names = list(self._instances.keys())
        for name in names:
            await self.unload_plugin(name)
        self._bus.clear()
        PluginBase._pending_classes.clear()
        self._loaded = False

    # ── 重载 ───────────────────────────────────────────

    async def reload_plugin(self, plugin_name: str) -> bool:
        """热重载指定插件。"""
        await self.unload_plugin(plugin_name)
        dir_name = self._find_plugin_dir(plugin_name)
        if dir_name:
            return await self._load_plugin(dir_name)

        # 按目录名再试一次
        for entry in os.listdir(self._plugins_dir_abs):
            if entry == plugin_name or entry.startswith(("__", ".")):
                continue
            plugin_dir = os.path.join(self._plugins_dir_abs, entry)
            if not os.path.isdir(plugin_dir):
                continue
            meta = self._read_metadata(plugin_dir)
            if meta and meta.get("name") == plugin_name:
                return await self._load_plugin(entry)
        return False

    # ── 全量重载 ──────────────────────────────────────

    async def reload_all(self):
        """卸载所有插件并重新加载。"""
        await self.unload_all()
        self._loaded = False
        return await self.load_all()
