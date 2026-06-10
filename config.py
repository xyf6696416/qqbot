"""
napcat-greywind 配置模块
从 config.yaml 加载所有配置项，提供全局常量。
"""

import os
import re
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

# ── NapCat ──
NAPCAT_WS = cfg["napcat"]["ws_url"]

# ── Trigger ──
TRIGGER_KW = cfg.get("trigger_keywords", [])

# ── 群聊 ──
GROUP_PROMPTS = cfg.get("groups", {})
DEFAULT_PROMPT = GROUP_PROMPTS.get("*", {}).get("prompt", "QQ群聊机器人灰风。像真人聊天，说人话，30字以内。")
QQ_GROUP_ID = int(next(iter(GROUP_PROMPTS))) if GROUP_PROMPTS else 0
AUTO_CLEAR_GROUPS = set(cfg.get("auto_clear_groups", []))
AUTO_SE_TU_GROUPS = {gid for gid, gcfg in GROUP_PROMPTS.items() if gcfg.get("auto_se_tu")}

# ── 权限 ──
ADMIN_UIDS = cfg.get("admin_uids", ["653020384"])
FORBIDDEN_OPS = cfg.get("forbidden_ops", ["删", "删除", "移除", "重置", "清空记录", "清空已发", "reset", "rm ", "del "])

# ── SiliconFlow ──
SF_KEY = "sk-ydqpvfaohcftjbxwpzpyzqcpdwweqvgdddkcavgmvxajkmja"
SF_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
SF_CHAT_MODEL = cfg.get("siliconflow", {}).get("chat_model", "Qwen/Qwen2.5-14B-Instruct")

# ── 图床 ──
TC_DIR = os.path.join(BASE_DIR, "tc")
IMG_PORT = 7777
IMG_URL = f"http://localhost:{IMG_PORT}"
IMG_STORAGE = os.path.join(TC_DIR, "1")

# ── 正则 ──
MEDIA_R = re.compile(r"MEDIA:\s*(.+?)(?:\s|$)")
QQMEDIA_R = re.compile(r"<qqmedia>(.*?)</qqmedia>")
EMBED_R = re.compile(r'\[embed\s+[^\]]*url="(.*?)"[^\]]*\]')

# ── 上下文 ──
MAX_CONTEXT = 200

# ── 图片压缩 ──
MAX_IMAGE_SIZE = 500 * 1024  # 500KB

# ── AI 生图 ──
GEN_IMG_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "AI生成")
GEN_IMG_API = "https://cdn.12ai.org/v1/images/generations"
GEN_IMG_KEY = None  # 懒加载
GEN_IMG_DAILY_LIMIT = cfg.get("gen_img", {}).get("daily_limit", 99)
GEN_IMG_URL_PREFIX = f"http://127.0.0.1:{IMG_PORT}/1/"
GEN_IMG_LOG = os.path.join(os.path.expanduser("~"), "Desktop", "AI生成", "gen_img.log")
