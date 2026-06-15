"""
GreyWind x NapCat Bridge v6.3
=============================
- Intent routing (keyword + SF fallback)
- Reference caching before routing
- Per-intent prompt splitting
- Group message context (max 200)
- Vision pipeline: SF raw
- Image compression (auto-compress >500KB on save_img)
- AI image generation (12AI GPT-Image-2)

模块化拆分：config / intent_router / image_utils / gen_img / bot
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys

# Windows 控制台 UTF-8 编码修复
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from config import (
    NAPCAT_WS, SF_MODEL, GEN_IMG_DAILY_LIMIT, MAX_CONTEXT, GROUP_PROMPTS,
)
from bot import Bot

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_test.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("gw")
logging.getLogger("httpx").setLevel(logging.WARNING)


def kill_old_bridges():
    """启动时干掉旧 bridge.py 进程，避免多实例并发"""
    current_pid = os.getpid()
    killed = []
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                cmd_str = " ".join(cmdline)
                if "bridge.py" not in cmd_str:
                    continue
                if "image_server.py" in cmd_str:
                    continue
                if proc.info["pid"] == current_pid:
                    continue
                log.info("KILL_OLD: PID=%d started=%s",
                         proc.info["pid"], proc.info.get("create_time", "?"))
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except psutil.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
                killed.append(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except ImportError:
        log.warning("psutil not installed, fallback to wmic")
        try:
            result = subprocess.run(
                ['wmic', 'process', 'where', 'name="python.exe"',
                 'get', 'ProcessId,CommandLine'],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                line = line.strip()
                if "bridge.py" in line and "image_server.py" not in line:
                    parts = line.split()
                    for part in parts:
                        if part.isdigit() and int(part) != current_pid:
                            pid = int(part)
                            try:
                                os.kill(pid, signal.SIGTERM)
                                log.info("KILL_OLD: PID=%d (wmic)", pid)
                                killed.append(pid)
                            except OSError:
                                pass
                            break
        except Exception as e:
            log.warning("KILL_OLD_ERR: %s", str(e)[:80])

    if killed:
        log.info("KILL_OLD_DONE: killed %d old bridge(s): %s", len(killed), killed)
    else:
        log.info("KILL_OLD: no old bridge found")


# ── Main ──────────────────────────────────────────────

log.info("=" * 56)
log.info("  NapCat Bridge v6.3 Mod + IntentRouter + GenImg")
log.info("  WS: %s", NAPCAT_WS)
log.info("  Vision: %s", SF_MODEL)
log.info("  GenImg: gpt-image-2 (daily limit %d)", GEN_IMG_DAILY_LIMIT)
log.info("  Router: keyword + SF fallback")
log.info("  Context: %d msgs/group", MAX_CONTEXT)
log.info("  Groups: %s", list(GROUP_PROMPTS.keys()))
log.info("=" * 56)

kill_old_bridges()

bot = Bot()
try:
    asyncio.run(bot.run())
except KeyboardInterrupt:
    asyncio.run(bot.close())
