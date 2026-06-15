"""
图片工具模块
- compress_image: 图片压缩（>500KB 所有格式统一转 JPG，阶梯降 quality）
- ImageServer: 本地图床管理（启动/复制/截图/清理）
"""

import asyncio
import io
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

# 阶梯 quality：从高到低，取第一个 ≤ max_size 的结果
QUALITY_LADDER = [93, 85, 75, 65, 55, 45, 35, 25, 15, 8, 5]


def _to_rgb(img):
    """任意模式转 RGB，透明通道合成为白色背景。"""
    if img.mode in ("RGBA", "LA", "P"):
        if img.mode == "P":
            img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "RGBA":
            bg.paste(img, mask=img.split()[3])
        else:
            bg.paste(img)
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def compress_image(path: str, max_size=MAX_IMAGE_SIZE) -> str:
    """
    压缩图片至 max_size 以下，统一转 JPG，返回输出路径。
    原图 ≤ max_size → 直接返回原路径。
    使用阶梯 quality 从高到低尝试，找到刚好 ≤ max_size 的最大 quality。
    """
    if not os.path.isfile(path):
        return path
    size = os.path.getsize(path)
    if size <= max_size:
        return path
    try:
        img = Image.open(path)
        img_rgb = _to_rgb(img)
        size = os.path.getsize(path)

        # 阶梯降 quality
        best_data = None
        best_q = 0
        for q in QUALITY_LADDER:
            buf = io.BytesIO()
            img_rgb.save(buf, format="JPEG", quality=q, optimize=True,
                         progressive=True, subsampling=-1)
            if buf.tell() <= max_size:
                best_data = buf.getvalue()
                best_q = q
                break

        if best_data is None:
            # 最低 quality 仍超限 → 用最低 quality
            buf = io.BytesIO()
            img_rgb.save(buf, format="JPEG", quality=QUALITY_LADDER[-1],
                         optimize=True, progressive=True, subsampling=-1)
            best_data = buf.getvalue()
            best_q = QUALITY_LADDER[-1]

        final_size = len(best_data)
        ext = os.path.splitext(path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            out_path = path
            tmp = path + ".tmp_c"
            with open(tmp, "wb") as f:
                f.write(best_data)
            os.replace(tmp, path)
        else:
            out_path = os.path.splitext(path)[0] + ".jpg"
            with open(out_path, "wb") as f:
                f.write(best_data)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

        log.info("COMPRESS: %s %dKB -> %dKB (q=%d)",
                 os.path.basename(out_path), size // 1024,
                 final_size // 1024, best_q)
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
