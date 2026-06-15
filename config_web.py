"""
配置管理 Web 前端 — Flask 应用
用法: python config_web.py
访问: http://localhost:7788
"""

import os
import re
import sys
import json
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, render_template, request, jsonify, send_file

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"
RELOAD_FLAG = BASE_DIR / ".reload_config.flag"
PORT = 7788

# 日志（预览路由等会用到）
import logging
log = logging.getLogger("gw")

app = Flask(__name__, template_folder=BASE_DIR / "templates")

# ── 图片去重 ──
from image_dedup import ImageDeduplicator

# 去重器实例（延迟初始化）
_dedup: ImageDeduplicator | None = None
_dedup_lock = threading.Lock()

# 扫描状态
_scan_state = {
    "running": False,
    "progress": {"current": 0, "total": 0, "file": "", "phase": ""},
    "stats": {},
    "start_time": None,
    "end_time": None,
}

# 审计模式重复组（扫描结果，待用户审核）
_dup_groups: list[dict] = []
_dup_groups_lock = threading.Lock()
_dup_applied = False  # 标记本次扫描结果是否已处理


def _get_dedup() -> ImageDeduplicator:
    global _dedup
    with _dedup_lock:
        if _dedup is None:
            _dedup = ImageDeduplicator()
        return _dedup


# ── 图片压缩调度器 ──
from image_compressor import status as comp_status, scan_and_compress, start_scheduler, scheduler as comp_scheduler

# 调度器初始化锁（确保只启动一次）
_compressor_inited = False
_compressor_init_lock = threading.Lock()


def _init_compressor_scheduler():
    """从 config.yaml 读取压缩配置并启动调度器"""
    global _compressor_inited
    with _compressor_init_lock:
        if _compressor_inited:
            return
        _compressor_inited = True
        try:
            cfg = read_config()
            cc = cfg.get("image_compressor", {})
            enabled = cc.get("enabled", True)
            interval = cc.get("interval_minutes", 30)
            max_size_kb = cc.get("max_size_kb", 500)
            scan_dirs = cc.get("scan_dirs", [
                str(BASE_DIR / "tc" / "1"),
                os.path.expanduser("~/Desktop/AI生成"),
            ])
            # 确保 tc/1 存在
            resolved = []
            for d in scan_dirs:
                p = os.path.expanduser(d)
                if not os.path.isabs(p):
                    p = str(BASE_DIR / p)
                resolved.append(p)
            start_scheduler(
                enabled=enabled,
                interval_minutes=interval,
                dirs=resolved,
                max_size_kb=max_size_kb,
            )
        except Exception as e:
            print(f"[压缩机] 初始化失败: {e}")


# ── 配置读写 ─────────────────────────────────────────────

def _signal_reload():
    """通知 bridge.py 重载配置（写标记文件）。"""
    try:
        RELOAD_FLAG.write_text("1", encoding="utf-8")
    except Exception:
        pass


def read_config():
    """读取 config.yaml 返回 dict"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        return {"_error": str(e)}


def write_config(data):
    """将 dict 写回 config.yaml"""
    backup = CONFIG_FILE.with_suffix(".yaml.bak")
    try:
        # 先备份
        if CONFIG_FILE.exists():
            import shutil
            shutil.copy2(CONFIG_FILE, backup)
        # 写入新配置
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False,
                      indent=2, width=120)
        return True, "保存成功"
    except Exception as e:
        return False, str(e)


# ── API 路由 ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("config_editor.html")


@app.route("/api/config", methods=["GET"])
def api_get_config():
    data = read_config()
    if "_error" in data:
        return jsonify({"ok": False, "error": data["_error"]})
    return jsonify({"ok": True, "data": data})


@app.route("/api/config", methods=["POST"])
def api_save_config():
    body = request.get_json(force=True)
    ok, msg = write_config(body)
    if ok:
        _signal_reload()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/config/raw", methods=["GET"])
def api_get_raw():
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
        return jsonify({"ok": True, "data": text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/raw", methods=["POST"])
def api_save_raw():
    body = request.get_json(force=True)
    raw = body.get("yaml", "")
    backup = CONFIG_FILE.with_suffix(".yaml.bak")
    try:
        # 验证 YAML 合法性
        parsed = yaml.safe_load(raw)
        if parsed is None:
            parsed = {}
        # 备份
        if CONFIG_FILE.exists():
            import shutil
            shutil.copy2(CONFIG_FILE, backup)
        # 写入
        CONFIG_FILE.write_text(raw, encoding="utf-8")
        _signal_reload()
        return jsonify({"ok": True, "message": "保存成功"})
    except yaml.YAMLError as e:
        return jsonify({"ok": False, "error": f"YAML 格式错误: {e.problem}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    """重启 bridge.py 进程"""
    try:
        script = str(BASE_DIR / "bridge.py")
        # 查找并 kill 旧 bridge 进程
        killed = 0
        try:
            result = subprocess.run(
                ['wmic', 'process', 'where', 'name="python.exe"',
                 'get', 'ProcessId,CommandLine'],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                if "bridge.py" in line and "config_web.py" not in line and "image_server.py" not in line:
                    parts = line.split()
                    for part in parts:
                        if part.isdigit():
                            try:
                                os.kill(int(part), signal.SIGTERM)
                                killed += 1
                            except (OSError, ValueError):
                                pass
                            break
        except Exception:
            pass

        # 启动新 bridge
        subprocess.Popen(
            [sys.executable, script],
            cwd=str(BASE_DIR),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )

        msg = f"已 kill {killed} 个旧进程，新 bridge 已启动"
        return jsonify({"ok": True, "message": msg})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/status", methods=["GET"])
def api_status():
    """检查所有服务运行状态：NapCat、图床、桥接、配置后端自身"""
    import socket
    import psutil

    def check_process(keyword, exclude=None):
        count = 0
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    cmdline = proc.info.get("cmdline") or []
                    cmd_str = " ".join(cmdline)
                    if keyword not in cmd_str:
                        continue
                    if exclude and exclude in cmd_str:
                        continue
                    count += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass
        return count

    def check_port(port):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            ok = s.connect_ex(('127.0.0.1', port)) == 0
            s.close()
            return ok
        except Exception:
            return False

    bridges = check_process("bridge.py", "config_web.py")
    img_servers = check_process("image_server.py")
    napcat_proc = check_process("NapCatWinBootMain")

    return jsonify({
        "ok": True,
        "data": {
            # 桥接
            "bridge_running": bridges > 0,
            "bridge_count": bridges,
            # 图床
            "img_server_running": img_servers > 0,
            "img_port_open": check_port(7777),
            # NapCat QQ
            "napcat_running": napcat_proc > 0,
            "napcat_port_open": check_port(18888),
            # 配置后端自身（能响应请求即表明在线）
            "config_web_running": True,
        }
    })


# ── 图片压缩 API ──────────────────────────────────────

@app.route("/api/compressor/status", methods=["GET"])
def api_compressor_status():
    """获取压缩器状态"""
    return jsonify({
        "ok": True,
        "data": comp_status.to_dict(),
    })


@app.route("/api/compressor/config", methods=["GET"])
def api_compressor_get_config():
    """获取压缩配置"""
    cc = read_config().get("image_compressor", {})
    return jsonify({
        "ok": True,
        "data": {
            "enabled": cc.get("enabled", True),
            "interval_minutes": cc.get("interval_minutes", 30),
            "max_size_kb": cc.get("max_size_kb", 500),
            "scan_dirs": cc.get("scan_dirs", [
                str(BASE_DIR / "tc" / "1"),
                os.path.expanduser("~/Desktop/AI生成"),
            ]),
            "scheduler": comp_scheduler.to_dict(),
        }
    })


@app.route("/api/compressor/config", methods=["POST"])
def api_compressor_set_config():
    """更新压缩配置并重启调度器"""
    body = request.get_json(force=True)
    cfg = read_config()
    cc = cfg.setdefault("image_compressor", {})

    if "enabled" in body:
        cc["enabled"] = bool(body["enabled"])
    if "interval_minutes" in body:
        cc["interval_minutes"] = max(1, int(body["interval_minutes"]))
    if "max_size_kb" in body:
        cc["max_size_kb"] = max(50, int(body["max_size_kb"]))
    if "scan_dirs" in body and isinstance(body["scan_dirs"], list):
        cc["scan_dirs"] = body["scan_dirs"]

    ok, msg = write_config(cfg)
    if not ok:
        return jsonify({"ok": False, "error": msg})
    _signal_reload()

    # 重启调度器
    enabled = cc.get("enabled", True)
    interval = cc.get("interval_minutes", 30)
    max_size_kb = cc.get("max_size_kb", 500)
    scan_dirs = cc.get("scan_dirs", [])

    resolved = []
    for d in scan_dirs:
        p = os.path.expanduser(d)
        if not os.path.isabs(p):
            p = str(BASE_DIR / p)
        resolved.append(p)

    global _compressor_inited
    _compressor_inited = True
    start_scheduler(
        enabled=enabled,
        interval_minutes=interval,
        dirs=resolved,
        max_size_kb=max_size_kb,
    )

    return jsonify({"ok": True, "message": "压缩配置已更新"})


@app.route("/api/compressor/run", methods=["POST"])
def api_compressor_run():
    """手动触发一次扫描压缩"""
    if comp_status.running:
        return jsonify({"ok": False, "error": "压缩扫描正在进行中，请等待完成"})

    cfg = read_config().get("image_compressor", {})
    scan_dirs = cfg.get("scan_dirs", [str(BASE_DIR / "tc" / "1"), os.path.expanduser("~/Desktop/AI生成")])
    max_size_kb = cfg.get("max_size_kb", 500)

    resolved = []
    for d in scan_dirs:
        p = os.path.expanduser(d)
        if not os.path.isabs(p):
            p = str(BASE_DIR / p)
        resolved.append(p)

    # 在后台线程执行，不阻塞 API
    def _run():
        scan_and_compress(resolved, max_size_kb * 1024)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return jsonify({"ok": True, "message": "压缩扫描已启动"})


# ── 一键重命名 API ──────────────────────────────────

_SUPPORTED_RENAME_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif"}

_RENAME_RE = re.compile(r"^\d{8}_\d{6}_\d{3}\.")  # 匹配已重命名格式


@app.route("/api/compressor/rename", methods=["POST"])
def api_compressor_rename():
    """一键重命名扫描目录中的图片文件，按修改时间排序命名。"""
    cfg = read_config().get("image_compressor", {})
    scan_dirs = cfg.get("scan_dirs", [str(BASE_DIR / "tc" / "1"),
                                       os.path.expanduser("~/Desktop/AI生成")])

    resolved_dirs = []
    for d in scan_dirs:
        p = os.path.expanduser(d)
        if os.path.isdir(p):
            resolved_dirs.append(p)

    if not resolved_dirs:
        return jsonify({"ok": False, "error": "没有有效的扫描目录"})

    results = {"renamed": 0, "skipped": 0, "errors": [], "files": []}

    for base_dir in resolved_dirs:
        # 收集所有图片文件
        all_files = []
        for root, _dirs, fnames in os.walk(base_dir):
            for fname in fnames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _SUPPORTED_RENAME_EXT:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                except OSError:
                    continue
                all_files.append((mtime, fpath))

        if not all_files:
            continue

        # 按 mtime 排序
        all_files.sort(key=lambda x: x[0])

        renamed_count = 0
        for mtime, fpath in all_files:
            folder = os.path.dirname(fpath)
            ext = os.path.splitext(fpath)[1].lower()
            fname = os.path.basename(fpath)

            # 跳过已是 YYYYMMDD_HHmmss_NNN.ext 格式的文件
            if _RENAME_RE.match(fname):
                results["skipped"] += 1
                continue

            # 生成时间戳部分
            ts = datetime.fromtimestamp(mtime).strftime("%Y%m%d_%H%M%S")
            seq = 1
            while True:
                new_name = f"{ts}_{seq:03d}{ext}"
                new_path = os.path.join(folder, new_name)
                if not os.path.exists(new_path):
                    break
                seq += 1

            try:
                os.rename(fpath, new_path)
                renamed_count += 1
                results["renamed"] += 1
                results["files"].append({
                    "old": fname,
                    "new": new_name,
                    "folder": os.path.relpath(folder, base_dir) if folder != base_dir else ".",
                })
            except OSError as e:
                results["errors"].append({"file": fname, "error": str(e)[:80]})

    return jsonify({
        "ok": True,
        "data": results,
        "message": f"重命名完成：{results['renamed']} 个成功，{results['skipped']} 个跳过"
    })


# ── 每日发图用量 API ─────────────────────────────────

DAILY_USAGE_FILE = BASE_DIR / ".daily_img_usage.json"


@app.route("/api/daily-img-usage", methods=["GET"])
def api_daily_img_usage():
    """返回所有群今日已发图片数。"""
    usage = {}
    try:
        if DAILY_USAGE_FILE.exists():
            raw = json.loads(DAILY_USAGE_FILE.read_text(encoding="utf-8"))
            today = datetime.now().strftime("%Y-%m-%d")
            for key, count in raw.items():
                date, gid = key.split("|", 1)
                if date == today:
                    usage[gid] = count
    except Exception:
        pass
    return jsonify({"ok": True, "data": usage})


# ── 图片去重 API ────────────────────────────────────────


@app.route("/api/dedup/status", methods=["GET"])
def api_dedup_status():
    """获取去重器状态、扫描进度、待审核重复组信息"""
    global _scan_state, _dup_groups, _dup_applied
    dedup = _get_dedup()
    with _dup_groups_lock:
        dup_count = len(_dup_groups)
        dup_files = sum(g["count"] for g in _dup_groups)
    return jsonify({
        "ok": True,
        "data": {
            **dedup.get_status(),
            "scan": {
                "running": _scan_state["running"],
                "progress": _scan_state["progress"],
                "stats": _scan_state.get("stats", {}),
                "start_time": _scan_state.get("start_time"),
                "end_time": _scan_state.get("end_time"),
            },
            "audit": {
                "has_groups": dup_count > 0,
                "group_count": dup_count,
                "duplicate_files": dup_files,
                "applied": _dup_applied,
            },
        },
    })


@app.route("/api/dedup/scan", methods=["POST"])
def api_dedup_scan():
    """
    启动全量扫描去重（审计模式）。
    扫描结果存入 _dup_groups，不自动删除，等待用户审核。
    """
    global _scan_state, _dup_groups, _dup_applied
    if _scan_state["running"]:
        return jsonify({"ok": False, "error": "扫描正在进行中，请等待完成"})

    base_dir = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片")
    if not os.path.isdir(base_dir):
        return jsonify({"ok": False, "error": f"目录不存在: {base_dir}"})

    def _do_scan():
        global _scan_state, _dup_groups, _dup_applied
        _scan_state = {
            "running": True,
            "progress": {"current": 0, "total": 0, "file": "", "phase": ""},
            "stats": {},
            "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": None,
        }
        _dup_applied = False
        try:
            dedup = _get_dedup()

            def _progress(cur, total, fpath, phase=""):
                _scan_state["progress"] = {"current": cur, "total": total, "file": fpath, "phase": phase}

            result = dedup.find_duplicates(base_dir, progress_callback=_progress)
            _scan_state["stats"] = {**result["stats"], "mode": "audit"}

            # 写入内存供审核页面读取
            with _dup_groups_lock:
                _dup_groups = result["groups"]

        except Exception as e:
            _scan_state["stats"] = {"error": str(e)}
            log.error("DEDUP_SCAN_ERR: %s", str(e)[:200])
        finally:
            _scan_state["running"] = False
            _scan_state["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _scan_state["progress"] = {"current": 0, "total": 0, "file": "", "phase": ""}

    t = threading.Thread(target=_do_scan, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "全量扫描已启动，完成后请在「重复文件」中审核"})


@app.route("/api/dedup/duplicates", methods=["GET"])
def api_dedup_get_duplicates():
    """获取待审核的重复组列表"""
    global _dup_groups, _dup_applied
    with _dup_groups_lock:
        return jsonify({
            "ok": True,
            "data": {
                "groups": _dup_groups,
                "count": len(_dup_groups),
                "applied": _dup_applied,
            },
        })


@app.route("/api/dedup/duplicates", methods=["POST"])
def api_dedup_apply_duplicates():
    """
    提交审核结果：
    删除用户勾选的文件，保留的文件记录到去重库。
    body: { "groups": [ { "files": [{"path": "...", "delete": true}, ...] }, ... ] }
    """
    global _dup_groups, _dup_applied
    if _dup_applied:
        return jsonify({"ok": False, "error": "本次扫描的结果已经处理过了，请重新扫描"})

    body = request.get_json(force=True)
    selection = body.get("groups", [])
    if not selection:
        return jsonify({"ok": False, "error": "没有提交审核结果"})

    # 1. 执行删除
    del_result = ImageDeduplicator.apply_deletion(selection)

    # 2. 保留的文件写入去重库
    dedup = _get_dedup()
    rec_result = ImageDeduplicator.record_remaining(selection, dedup)

    # 3. 清理数据库失效记录
    prune_result = dedup.prune()

    _dup_applied = True

    return jsonify({
        "ok": True,
        "data": {
            "deleted": del_result["deleted"],
            "delete_failed": del_result["failed"],
            "recorded": rec_result["recorded"],
            "prune_removed": prune_result["removed"],
            "remaining_records": prune_result["remaining"],
        },
        "message": (
            f"已删除 {del_result['deleted']} 张重复图片"
            + (f"，{del_result['failed']} 张删除失败" if del_result["failed"] else "")
            + f"，去重库已记录 {rec_result['recorded']} 张保留图片 ✅"
        ),
    })


@app.route("/api/dedup/prune", methods=["POST"])
def api_dedup_prune():
    """清理失效记录（文件已被删除的脏数据）"""
    dedup = _get_dedup()
    result = dedup.prune()
    return jsonify({
        "ok": True,
        "data": result,
        "message": f"已清理 {result['removed']} 条失效记录，剩余 {result['remaining']} 条",
    })


# ── 图片预览 ────────────────────────────────────────────

_FWD_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片")


@app.route("/api/dedup/preview")
def api_dedup_preview():
    """返回重复图片的缩略图（最大 300px），用于审核预览。"""
    path = request.args.get("path", "")
    if not path:
        return "", 400

    # 安全校验：只允许访问 ~/Desktop/转发图片/ 下文件
    try:
        real_path = os.path.realpath(path)
        real_base = os.path.realpath(_FWD_DIR)
        if not real_path.startswith(real_base + os.sep) and real_path != real_base:
            return "", 403
        if not os.path.isfile(real_path):
            return "", 404
    except Exception:
        return "", 400

    try:
        from PIL import Image
        import io

        img = Image.open(real_path)
        img.thumbnail((300, 300), Image.LANCZOS)

        # 统一转 JPEG 输出
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")
    except Exception as e:
        log.warning("PREVIEW_ERR: %s %s", os.path.basename(path), str(e)[:80])
        return "", 500


# ── 启动前初始化 ──────────────────────────────────────
# 在首次请求时初始化压缩器，避免启动时的文件锁冲突
_compressor_hook_fired = False

@app.before_request
def _ensure_compressor_init():
    global _compressor_hook_fired
    if _compressor_hook_fired:
        return
    _compressor_hook_fired = True
    _init_compressor_scheduler()
    # 只执行一次，移除 hook
    try:
        app.before_request_funcs[None].remove(_ensure_compressor_init)
    except (ValueError, KeyError):
        pass


# ── 启动 ─────────────────────────────────────────────────

if __name__ == "__main__":
    # 也可以在启动时直接初始化（before_request 也做了同样的事）
    _init_compressor_scheduler()
    print(f"""
    ╔══════════════════════════════════╗
    ║  配置管理面板                     ║
    ║  地址: http://localhost:{PORT}    ║
    ║  按 Ctrl+C 停止                  ║
    ╚══════════════════════════════════╝
    """)
    app.run(host="127.0.0.1", port=PORT, debug=False)
