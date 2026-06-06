"""
AI 生图模块 (12AI GPT-Image-2)
- 生图 API 调用
- 日志/额度管理
"""

import base64
import json
import logging
import os
from datetime import datetime

from config import GEN_IMG_API, GEN_IMG_KEY as _CFG_GEN_IMG_KEY

log = logging.getLogger("gw")

# 模块级 key 缓存
_GEN_IMG_KEY = _CFG_GEN_IMG_KEY


def _get_gen_img_key():
    """读取 12AI API key，带缓存"""
    global _GEN_IMG_KEY
    if _GEN_IMG_KEY:
        return _GEN_IMG_KEY
    key_file = os.path.expanduser("~/Desktop/key.txt")
    try:
        with open(key_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    _GEN_IMG_KEY = line.split()[0]
                    return _GEN_IMG_KEY
    except Exception as e:
        log.error("GEN_IMG_KEY_ERR: %s", str(e)[:80])
    return None


def _log_gen_img(group_id, user_id, prompt, status, detail=""):
    """写生图日志到桌面/AI生成/gen_img.log"""
    from config import GEN_IMG_LOG
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] gid={group_id} uid={user_id} status={status} prompt={prompt[:100]} {detail}"
    try:
        os.makedirs(os.path.dirname(GEN_IMG_LOG), exist_ok=True)
        with open(GEN_IMG_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        log.error("GEN_IMG_LOG_ERR: %s", str(e)[:80])


async def gen_image(prompt: str, image_urls: list[str] | None = None) -> bytes | None:
    """调 12AI API 生成图片，返回 PNG 字节。
    image_urls: 参考图 URL 列表（已上传到图床），直接拼入 prompt。
    """
    import asyncio
    key = _get_gen_img_key()
    if not key:
        log.error("GEN_IMG: no API key")
        return None

    # 如果带了参考图，把 URL 拼进 prompt（GPT-Image-2 原生支持 URL 参考图）
    final_prompt = prompt
    if image_urls:
        img_refs = "  ".join(url for url in image_urls)
        final_prompt = f"{img_refs}  {prompt}".strip()

    payload = {
        "model": "gpt-image-2",
        "prompt": final_prompt,
        "n": 1,
        "size": "auto",
        "quality": "auto",
        "response_format": "b64_json",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return await asyncio.to_thread(_gen_image_sync, body, key)


def _gen_image_sync(body: bytes, key: str) -> bytes | None:
    """同步版生图（用 http.client 规避 header 编码问题）"""
    import http.client
    import socket
    url = GEN_IMG_API.replace("https://", "").replace("http://", "")
    host, path = url.split("/", 1)
    auth_val = ("Bearer " + key).encode("utf-8").decode("latin-1")
    conn = http.client.HTTPSConnection(host, timeout=500)
    try:
        conn.request("POST", "/" + path, body=body,
            headers={
                "Authorization": auth_val,
                "Content-Type": "application/json; charset=utf-8",
            })
        resp = conn.getresponse()
        j = json.loads(resp.read().decode("utf-8"))
        if "error" in j:
            log.error("GEN_IMG_API_ERR: %s", j["error"])
            return None
        if j.get("data") and len(j["data"]) > 0 and j["data"][0].get("b64_json"):
            return base64.b64decode(j["data"][0]["b64_json"])
        log.error("GEN_IMG: unexpected response: %s", json.dumps(j)[:200])
        return None
    except (http.client.HTTPException, socket.timeout, TimeoutError,
            ConnectionError, OSError, json.JSONDecodeError) as e:
        log.error("GEN_IMG_EXCEPTION: %s", str(e)[:200])
        return None
    finally:
        conn.close()
