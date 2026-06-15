"""快速验证模块导入链"""
import sys, os
# Ensure parent dir is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("1. Testing config...")
from config import (NAPCAT_WS, TRIGGER_KW,
                    GROUP_PROMPTS, DEFAULT_PROMPT, AUTO_CLEAR_GROUPS,
                    SF_KEY, SF_MODEL, IMG_PORT, IMG_URL, IMG_STORAGE,
                    MAX_CONTEXT, GEN_IMG_DAILY_LIMIT)
print(f"   OK - Groups: {list(GROUP_PROMPTS.keys())}")

print("2. Testing intent_router...")
from intent_router import IntentRouter, RefContext
r = IntentRouter.keyword_route("发张图看看", False, False, False)
print(f"   OK - Route '发张图': {r['intent']} conf={r['confidence']}")
r2 = IntentRouter.keyword_route("今天天气不错", False, False, False)
print(f"   OK - Route '聊天': {r2['intent']} conf={r2['confidence']}")
cnt = IntentRouter.extract_count("来两张图")
print(f"   OK - Extract count '来两张图': {cnt}")

print("3. Testing image_utils...")
from image_utils import compress_image, ImageServer
print(f"   OK - ImageServer class loaded")

print("4. Testing gen_img...")
from gen_img import gen_image, _log_gen_img, _get_gen_img_key
print(f"   OK - gen_img module loaded")

print("5. Testing bot...")
from bot import Bot
print(f"   OK - Bot class loaded")

print("\n>>> ALL IMPORTS OK <<<")
