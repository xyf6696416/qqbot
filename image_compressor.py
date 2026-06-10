"""
图片压缩扫描器 v2.1
========================
- 扫描指定目录，调用 image_utils.compress_image 压缩超限图片
- 用 Get-ChildItem 快速获取超限文件列表
- 记录文件夹总大小缓存，未变化则跳过扫描
- 线程安全状态追踪，支持定时调度
"""

import os
import json
import subprocess
import logging
import threading
import traceback
from datetime import datetime

from image_utils import compress_image

log = logging.getLogger("gw")

SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".compressor_cache.json")


# ═══════════════════════════════════════════════════════════════
#  文件夹大小缓存
# ═══════════════════════════════════════════════════════════════

def _load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(data):
    os.makedirs(os.path.dirname(CACHE_FILE) or ".", exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_folder_snapshot(path):
    """获取目录递归总大小和文件数。返回 (total_bytes, file_count)。"""
    try:
        cmd = [
            "powershell", "-NoProfile", "-Command",
            f"Get-ChildItem -Path '{path}' -Recurse -File "
            "| Measure-Object -Property Length -Sum "
            "| ForEach-Object { $_.Count.ToString() + '|' + $_.Sum.ToString() }"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            out = result.stdout.strip()
            if "|" in out:
                parts = out.split("|")
                return int(parts[1]), int(parts[0])
    except Exception:
        pass
    # fallback
    total, count = 0, 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return total, count


def check_folder_changed(path):
    """
    检测文件夹是否有变化。
    返回 True=有变化需要扫描, False=没变可跳过。
    """
    cache = _load_cache()
    key = os.path.abspath(path)
    entry = cache.get("folder_sizes", {}).get(key, {})
    last_total = entry.get("total_size", -1)
    last_count = entry.get("file_count", -1)

    current_total, current_count = get_folder_snapshot(path)
    if current_total == 0 and current_count == 0:
        return True

    if current_total == last_total and current_count == last_count:
        return False

    # 更新缓存
    cache.setdefault("folder_sizes", {})[key] = {
        "total_size": current_total,
        "file_count": current_count,
        "last_scan": datetime.now().isoformat(),
    }
    _save_cache(cache)
    return True


def get_large_files(path, max_size):
    """
    用 PowerShell 获取目录中超过 max_size 的图片文件列表。
    返回 [filepath, ...]。
    """
    ext_filter = " -or ".join(f"$_.Extension -eq '{e}'" for e in SUPPORTED_EXT)
    cmd = (
        f"Get-ChildItem -Path '{path}' -Recurse -File "
        f"| Where-Object {{ $_.Length -gt {max_size} -and ({ext_filter}) }} "
        f"| ForEach-Object {{ $_.FullName }}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            files = [l.strip() for l in result.stdout.splitlines() if l.strip()]
            return files
    except subprocess.TimeoutExpired:
        log.warning("COMPRESSOR: Get-ChildItem 超时，回退到 os.walk")
    except Exception:
        pass

    # fallback
    large = []
    for root, _dirs, fnames in os.walk(path):
        for f in fnames:
            if os.path.splitext(f)[1].lower() not in SUPPORTED_EXT:
                continue
            fpath = os.path.join(root, f)
            try:
                if os.path.getsize(fpath) > max_size:
                    large.append(fpath)
            except OSError:
                pass
    return large


# ═══════════════════════════════════════════════════════════════
#  状态追踪
# ═══════════════════════════════════════════════════════════════

class CompressorStatus:
    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.last_run_start = None
        self.last_run_end = None
        self.last_run_duration = 0.0
        self.last_run_ok = False
        self.last_run_summary = ""
        self.total_scanned = 0
        self.total_compressed = 0
        self.total_bytes_saved = 0
        self.total_skipped = 0
        self.total_errors = 0
        self.errors = []
        self.last_results = []
        self.history = []
        self.skipped_dirs = []

    def start_run(self):
        with self._lock:
            self.running = True
            self.last_run_start = datetime.now()
            self.total_scanned = 0
            self.total_compressed = 0
            self.total_bytes_saved = 0
            self.total_skipped = 0
            self.total_errors = 0
            self.errors = []
            self.last_results = []
            self.skipped_dirs = []

    def end_run(self):
        with self._lock:
            self.running = False
            self.last_run_end = datetime.now()
            if self.last_run_start:
                self.last_run_duration = (self.last_run_end - self.last_run_start).total_seconds()
            self.last_run_ok = self.total_errors == 0
            parts = []
            if self.total_scanned > 0:
                parts.append(f"扫描 {self.total_scanned} 文件")
            if self.total_compressed > 0:
                m = self.total_bytes_saved / (1024*1024)
                parts.append(f"压缩 {self.total_compressed} 个 (省 {m:.1f}MB)")
            if self.total_skipped > 0:
                parts.append(f"跳过 {self.total_skipped} 个")
            if self.total_errors > 0:
                parts.append(f"错误 {self.total_errors} 个")
            if self.skipped_dirs:
                parts.append(f"{len(self.skipped_dirs)} 个目录未变化跳过")
            self.last_run_summary = ", ".join(parts) or "无操作"
            self.history.append({
                "start": self.last_run_start.isoformat() if self.last_run_start else "",
                "end": self.last_run_end.isoformat() if self.last_run_end else "",
                "duration": round(self.last_run_duration, 1),
                "scanned": self.total_scanned,
                "compressed": self.total_compressed,
                "saved_bytes": self.total_bytes_saved,
                "ok": self.last_run_ok,
                "summary": self.last_run_summary,
            })
            if len(self.history) > 100:
                self.history = self.history[-100:]

    def record_scan(self):
        with self._lock:
            self.total_scanned += 1

    def record_compress(self, orig_size, saved, path, quality):
        with self._lock:
            self.total_compressed += 1
            self.total_bytes_saved += saved
            self.last_results.append({
                "path": path,
                "original_size": orig_size,
                "final_size": orig_size - saved,
                "saved": saved,
                "saved_kb": round(saved / 1024, 1),
                "quality_used": quality,
                "method": f"q{quality}",
                "error": "",
                "skipped": False,
            })
            if len(self.last_results) > 50:
                self.last_results = self.last_results[-50:]

    def record_skip(self):
        with self._lock:
            self.total_skipped += 1

    def record_error(self, path, msg):
        with self._lock:
            self.total_errors += 1
            self.errors.append({"path": path, "msg": str(msg)[:120]})
            if len(self.errors) > 20:
                self.errors = self.errors[-20:]

    def to_dict(self):
        with self._lock:
            return {
                "running": self.running,
                "last_run_start": self.last_run_start.isoformat() if self.last_run_start else None,
                "last_run_end": self.last_run_end.isoformat() if self.last_run_end else None,
                "last_run_duration": round(self.last_run_duration, 1),
                "last_run_ok": self.last_run_ok,
                "last_run_summary": self.last_run_summary,
                "total_scanned": self.total_scanned,
                "total_compressed": self.total_compressed,
                "total_bytes_saved": self.total_bytes_saved,
                "total_saved_mb": round(self.total_bytes_saved / (1024*1024), 2),
                "total_skipped": self.total_skipped,
                "total_errors": self.total_errors,
                "errors": self.errors[-5:],
                "last_results": self.last_results[-10:],
                "history": self.history[-20:],
                "skipped_dirs": self.skipped_dirs,
            }


status = CompressorStatus()


# ═══════════════════════════════════════════════════════════════
#  扫描入口
# ═══════════════════════════════════════════════════════════════

def scan_and_compress(dirs, max_size=512 * 1024):
    """扫描多个目录，调用 compress_image 压缩超限图片。返回 status.to_dict()"""
    if status.running:
        log.warning("COMPRESSOR: 上次扫描仍在运行，跳过")
        return status.to_dict()

    status.start_run()
    try:
        for d in dirs:
            resolved = os.path.expanduser(d)
            if not os.path.isdir(resolved):
                log.info("COMPRESSOR: 目录不存在，跳过: %s", resolved)
                status.record_error(resolved, "目录不存在")
                continue

            # 文件夹大小检查
            if not check_folder_changed(resolved):
                log.info("COMPRESSOR: 目录未变化，跳过: %s", resolved)
                with status._lock:
                    status.skipped_dirs.append(resolved)
                continue

            # 获取超限文件列表
            log.info("COMPRESSOR: 扫描目录 %s", resolved)
            large_files = get_large_files(resolved, max_size)
            log.info("COMPRESSOR: 发现 %d 个超限文件", len(large_files))

            for fpath in large_files:
                status.record_scan()
                orig_size = os.path.getsize(fpath)
                if orig_size <= max_size:
                    continue

                out = compress_image(fpath, max_size)
                if out == fpath and os.path.getsize(fpath) == orig_size:
                    # 没被压缩（可能是出错或已经小于阈值）
                    status.record_error(fpath, "压缩失败或未变化")
                    continue

                # 计算实际的节省量
                final_size = os.path.getsize(out)
                saved = orig_size - final_size
                # 从 method 名提取 quality（compress_image 内部日志有 quality，但我们取不到）
                status.record_compress(orig_size, saved, out, 0)

            log.info("COMPRESSOR: 目录完成 %s", resolved)

        status.end_run()
        log.info("COMPRESSOR: 扫描完成 — %s", status.last_run_summary)
    except Exception as e:
        status.record_error("scan_and_compress", str(e)[:200])
        status.end_run()
    return status.to_dict()


# ═══════════════════════════════════════════════════════════════
#  定时调度器
# ═══════════════════════════════════════════════════════════════

class CompressorScheduler:
    def __init__(self):
        self._timer = None
        self._lock = threading.Lock()
        self._enabled = False
        self._interval = 1800
        self._dirs = []
        self._max_size = 512 * 1024
        self._stop_event = threading.Event()

    @property
    def enabled(self):
        return self._enabled

    @property
    def interval_minutes(self):
        return self._interval // 60

    def configure(self, enabled=True, interval_minutes=30,
                  dirs=None, max_size_kb=500):
        with self._lock:
            self._enabled = enabled
            self._interval = max(60, interval_minutes * 60)
            self._dirs = dirs or []
            self._max_size = max_size_kb * 1024

    def to_dict(self):
        with self._lock:
            return {
                "enabled": self._enabled,
                "interval_minutes": self._interval // 60,
                "dirs": self._dirs,
                "max_size_kb": self._max_size // 1024,
            }

    def start(self):
        self.stop()
        with self._lock:
            if not self._enabled or not self._dirs:
                return
        self._schedule_next()

    def stop(self):
        self._stop_event.set()
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        self._stop_event.clear()

    def _schedule_next(self):
        with self._lock:
            if not self._enabled or not self._dirs:
                return
            self._timer = threading.Timer(self._interval, self._run)
            self._timer.daemon = True
            self._timer.start()

    def _run(self):
        if self._stop_event.is_set():
            return
        try:
            scan_and_compress(self._dirs, self._max_size)
        except Exception:
            log.error("COMPRESSOR_SCHED: %s", traceback.format_exc())
        self._schedule_next()


scheduler = CompressorScheduler()


def start_scheduler(enabled=True, interval_minutes=30,
                    dirs=None, max_size_kb=500):
    scheduler.configure(enabled=enabled, interval_minutes=interval_minutes,
                        dirs=dirs, max_size_kb=max_size_kb)
    scheduler.start()
    if enabled and dirs:
        log.info("COMPRESSOR_SCHED: 已启动，间隔=%d分钟, 目录=%s",
                 interval_minutes, dirs)
    return scheduler.to_dict()
