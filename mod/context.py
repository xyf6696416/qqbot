"""
PluginContext — 注入给插件的上下文
提供: 发消息、读配置、持久化 KV 存储、日志
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("gw.mod.context")


class PluginContext:
    """插件上下文：每个插件实例持有自己的 context。"""

    def __init__(self, plugin_name: str, bot_ref, plugins_dir: str):
        self._plugin_name = plugin_name
        self._bot = bot_ref
        self._plugins_dir = plugins_dir
        self._kv_path = os.path.join(plugins_dir, plugin_name, "kv_store.json")
        self._kv_cache: dict | None = None
        self.log = logging.getLogger(f"plugin.{plugin_name}")

    # ── 发送消息 ────────────────────────────────────────

    async def send_group_msg(self, group_id: str | int, text: str) -> None:
        """向群发送纯文本消息。"""
        if self._bot is None:
            self.log.warning("send_group_msg: bot not available")
            return
        await self._bot._enqueue_send("send_group_msg", {
            "group_id": int(group_id),
            "message": [{"type": "text", "data": {"text": text}}],
        })

    async def send_group_custom(self, group_id: str | int,
                                message: list) -> None:
        """向群发送自定义消息段列表。"""
        if self._bot is None:
            self.log.warning("send_group_custom: bot not available")
            return
        await self._bot._enqueue_send("send_group_msg", {
            "group_id": int(group_id),
            "message": message,
        })

    # ── 配置读取 ────────────────────────────────────────

    def get_group_config(self, group_id: str | int) -> dict:
        """获取某群的配置（prompt 等）。"""
        if self._bot is None:
            return {}
        groups = getattr(self._bot, '_group_configs', {})
        return groups.get(str(group_id), {})

    def get_bot_config(self) -> dict:
        """读取当前 config.yaml 全部内容。"""
        import yaml
        cfg_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            self.log.warning("get_bot_config err: %s", str(e)[:80])
            return {}

    # ── 持久化 KV 存储 ──────────────────────────────────

    async def kv_get(self, key: str, default=None):
        """读取插件自己的持久化键值。"""
        store = self._load_kv()
        return store.get(key, default)

    async def kv_put(self, key: str, value) -> None:
        """写入插件自己的持久化键值。"""
        store = self._load_kv()
        store[key] = value
        self._save_kv(store)

    def _load_kv(self) -> dict:
        if self._kv_cache is not None:
            return self._kv_cache
        try:
            p = Path(self._kv_path)
            if p.exists():
                self._kv_cache = json.loads(p.read_text(encoding="utf-8"))
            else:
                self._kv_cache = {}
        except Exception:
            self._kv_cache = {}
        return self._kv_cache

    def _save_kv(self, store: dict) -> None:
        try:
            p = Path(self._kv_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(store, ensure_ascii=False, indent=2),
                encoding="utf-8")
            self._kv_cache = store
        except Exception as e:
            self.log.warning("kv_save err: %s", str(e)[:80])

    # ── 原始 Bot 访问（高级用法） ──────────────────────

    @property
    def bot(self):
        """获取 Bot 实例引用，供高级插件直接操作。"""
        return self._bot
