"""
图片工具模块
- compress_image: 图片压缩（>500KB 自动 PNG→JPG）
- ImageServer: 本地图床管理（启动/复制/截图/清理）
"""

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
import uuid

from PIL import Image

from config import IMG_PORT, IMG_URL, IMG_STORAGE, TC_DIR, MAX_IMAGE_SIZE

log = logging.getLogger("gw")


def compress_image(path: str, max_size=MAX_IMAGE_SIZE) -> str:
    """
    压缩图片至 max_size 以下，返回输出路径。
    原图小于 max_size → 直接返回原路径。
    格式转换（PNG→JPG）→ 删除原文件，避免 tc/1/ 残留。
    """
    if not os.path.isfile(path):
        return path
    size = os.path.getsize(path)
    if size <= max_size:
        return path
    try:
        img = Image.open(path)
        ext = os.path.splitext(path)[1].lower()
        base, _ = os.path.splitext(path)
        need_convert = ext in (".png", ".bmp", ".tiff", ".tif")
        has_alpha = img.mode in ("RGBA", "LA", "P") and need_convert
        converted = False
        if need_convert and not has_alpha and img.mode != "RGB":
            img = img.convert("RGB")
            out_path = base + ".jpg"
            converted = True
        else:
            out_path = path
        fmt = "JPEG" if out_path.endswith(".jpg") else "PNG" if out_path.endswith(".png") else "WEBP"
        ratio = size / max_size
        quality = max(30, min(85, int(85 / ratio)))
        opts_final = {"format": fmt}
        if fmt != "PNG":
            opts_final["quality"] = quality
        img.save(out_path, **opts_final)
        if fmt != "PNG" and os.path.getsize(out_path) > max_size:
            quality = max(20, quality - 15)
            opts_final["quality"] = quality
            img.save(out_path, **opts_final)
        final_size = os.path.getsize(out_path)
        log.info("COMPRESS: %s %dKB -> %s %dKB (q=%d)",
                 os.path.basename(path), size // 1024,
                 os.path.basename(out_path), final_size // 1024, quality)
        if converted and os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass
        return out_path
    except Exception as e:
        log.warning("COMPRESS_ERR: %s %s", os.path.basename(path), str(e)[:80])
        return path


class ImageServer:
    """本地图床管理"""

    def __init__(self):
        self._proc = None

    @property
    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    async def ensure_running(self):
        if self.is_running or await self._check_port():
            return
        log.info("Start img server...")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        self._proc = subprocess.Popen(
            [sys.executable, os.path.join(TC_DIR, "image_server.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=TC_DIR, env=env)
        for _ in range(10):
            if await self._check_port():
                return
            await asyncio.sleep(1)

    async def _check_port(self):
        s = socket.socket()
        try:
            s.settimeout(1.0)
            s.connect(("127.0.0.1", IMG_PORT))
            return True
        except Exception:
            return False
        finally:
            s.close()

    def copy_img(self, src):
        if not src or not os.path.isfile(src):
            return None
        ext = os.path.splitext(src)[1].lower()
        os.makedirs(IMG_STORAGE, exist_ok=True)
        dst = os.path.join(IMG_STORAGE, f"{uuid.uuid4().hex}{ext}")
        shutil.copy2(src, dst)
        return f"{IMG_URL}/1/{os.path.basename(dst)}"

    def screenshot(self, url):
        os.makedirs(IMG_STORAGE, exist_ok=True)
        dst = os.path.join(IMG_STORAGE, f"{uuid.uuid4().hex}.png")
        edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        if not os.path.isfile(edge):
            return None
        try:
            subprocess.run([
                edge, "--headless", f"--screenshot={dst}",
                "--window-size=800,600", "--no-sandbox", "--disable-gpu", url,
            ], timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.isfile(dst) and os.path.getsize(dst) > 100:
                return f"{IMG_URL}/1/{os.path.basename(dst)}"
        except Exception:
            return None

    def cleanup(self):
        if self._proc and not self._proc.poll():
            self._proc.terminate()
