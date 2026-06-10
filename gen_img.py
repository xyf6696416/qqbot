"""
AI 生图模块 (12AI GPT-Image-2)
- 纯文本生图 → /v1/images/generations (JSON)
- 带参考图生图 → /v1/images/edits (multipart/form-data 上传文件)
- 日志/额度管理
"""

import base64
import json
import logging
import os
from datetime import datetime
from types import SimpleNamespace

import requests

from config import GEN_IMG_API, GEN_IMG_KEY as _CFG_GEN_IMG_KEY

log = logging.getLogger("gw")

_GEN_IMG_KEY = _CFG_GEN_IMG_KEY

_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
    ".bmp": "image/bmp", ".gif": "image/gif",
}

# 状态码 → 用户可见消息
STATUS_MSGS = {
    400: "请求参数错误，可能是 size、quality 或文件格式不正确",
    401: "API Key 缺失或无效",
    402: "余额不足",
    403: "内容安全策略拦截",
    429: "请求频率过高，稍后再试",
    502: "上游服务异常，可稍后重试",
}


class GenResult:
    """生图结果。data 不为空表示成功，否则看 status/error。"""
    __slots__ = ("data", "status", "error")
    def __init__(self, data=None, status=0, error=""):
        self.data = data
        self.status = status
        self.error = error

    @property
    def ok(self):
        return self.data is not None


def _get_gen_img_key():
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
    from config import GEN_IMG_LOG
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] gid={group_id} uid={user_id} status={status} prompt={prompt[:100]} {detail}"
    try:
        os.makedirs(os.path.dirname(GEN_IMG_LOG), exist_ok=True)
        with open(GEN_IMG_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


async def gen_image(prompt: str, image_paths: list[str] | None = None,
                     size: str | None = None, quality: str | None = None) -> GenResult:
    """调 API 生图。返回 GenResult，data 不为空即成功。"""
    import asyncio
    key = _get_gen_img_key()
    if not key:
        return GenResult(status=401, error="API Key 缺失或无效")

    if image_paths:
        return await asyncio.to_thread(
            _gen_image_edits, prompt, image_paths, key, size, quality)
    else:
        return await asyncio.to_thread(
            _gen_image_generations, prompt, key, size, quality)


def _parse_response(resp: requests.Response) -> GenResult:
    """从 requests.Response 解析 GenResult"""
    status = resp.status_code
    try:
        j = resp.json()
    except Exception:
        return GenResult(status=status, error=f"HTTP {status}")

    if status != 200:
        err_msg = str(j.get("error", j))[:200]
        return GenResult(status=status, error=err_msg)

    if "error" in j:
        return GenResult(status=400, error=str(j["error"])[:200])

    if j.get("data") and j["data"][0].get("b64_json"):
        img = base64.b64decode(j["data"][0]["b64_json"])
        return GenResult(data=img)

    log.error("GEN_IMG: unexpected response: %s", json.dumps(j)[:200])
    return GenResult(status=502, error="响应格式异常")


# ── 纯文本生图：/v1/images/generations (JSON) ──────────

def _gen_image_generations(prompt: str, key: str,
                            size: str | None, quality: str | None) -> GenResult:
    try:
        resp = requests.post(
            GEN_IMG_API,
            json={
                "model": "gpt-image-2",
                "prompt": prompt,
                "n": 1,
                "size": size or "auto",
                "quality": quality or "auto",
                "response_format": "b64_json",
            },
            headers={"Authorization": f"Bearer {key}"},
            timeout=500,
        )
        return _parse_response(resp)
    except requests.exceptions.Timeout:
        return GenResult(status=502, error="请求超时")
    except Exception as e:
        log.error("GEN_IMG_GEN_ERR: %s", str(e)[:200])
        return GenResult(status=502, error=str(e)[:100])


# ── 带参考图生图：/v1/images/edits (multipart) ────────

def _compress_if_needed(path: str, max_size=512 * 1024) -> str:
    """如果图片 > max_size 则压缩，返回输出路径。"""
    if not os.path.isfile(path) or os.path.getsize(path) <= max_size:
        return path
    from image_utils import compress_image
    return compress_image(path, max_size)


def _gen_image_edits(prompt: str, image_paths: list[str], key: str,
                      size: str | None, quality: str | None) -> GenResult:
    edits_url = GEN_IMG_API.replace("/images/generations", "/images/edits")

    files = []
    cleanup = []  # 压缩产生的临时文件，用完删除
    try:
        for p in image_paths[:10]:  # 最多 10 张
            if not os.path.isfile(p):
                continue
            # 超 500KB 先压缩
            compressed = _compress_if_needed(p)
            if compressed != p:
                cleanup.append(compressed)
            ext = os.path.splitext(compressed)[1].lower()
            mime = _MIME.get(ext, "image/jpeg")
            files.append(("image", (os.path.basename(compressed), open(compressed, "rb"), mime)))

        if not files:
            return GenResult(status=400, error="没有有效的参考图")

        try:
            resp = requests.post(
                edits_url,
                data={
                    "model": "gpt-image-2",
                    "prompt": prompt,
                    "n": "1",
                    "size": size or "auto",
                    "quality": quality or "auto",
                    "response_format": "b64_json",
                },
                files=files,
                headers={"Authorization": f"Bearer {key}"},
                timeout=500,
            )
            return _parse_response(resp)
        except requests.exceptions.Timeout:
            return GenResult(status=502, error="请求超时")
    except Exception as e:
        log.error("GEN_IMG_EDIT_ERR: %s", str(e)[:200])
        return GenResult(status=502, error=str(e)[:100])
    finally:
        for _, f in files:
            try:
                f[1].close()
            except Exception:
                pass
        for p in cleanup:
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
