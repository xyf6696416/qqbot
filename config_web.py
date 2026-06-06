"""
配置管理 Web 前端 — Flask 应用
用法: python config_web.py
访问: http://localhost:7788
"""

import os
import sys
import json
import signal
import subprocess
from pathlib import Path

import yaml
from flask import Flask, render_template, request, jsonify

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"
PORT = 7788

app = Flask(__name__, template_folder=BASE_DIR / "templates")


# ── 配置读写 ─────────────────────────────────────────────

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
    """检查 bridge 和图片服务器运行状态"""
    import socket
    import psutil
    bridges = 0
    img_servers = 0
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                cmd_str = " ".join(cmdline)
                if "bridge.py" not in cmd_str:
                    continue
                if "config_web.py" in cmd_str or "image_server.py" in cmd_str:
                    continue
                if "bridge.py" in cmd_str:
                    bridges += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                cmd_str = " ".join(cmdline)
                if "image_server.py" in cmd_str:
                    img_servers += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass

    img_port_open = False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        img_port_open = s.connect_ex(('127.0.0.1', 7777)) == 0
        s.close()
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "data": {
            "bridge_running": bridges > 0,
            "bridge_count": bridges,
            "img_server_running": img_servers > 0,
            "img_port_open": img_port_open,
        }
    })


# ── 启动 ─────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"""
    ╔══════════════════════════════════╗
    ║  配置管理面板                     ║
    ║  地址: http://localhost:{PORT}    ║
    ║  按 Ctrl+C 停止                  ║
    ╚══════════════════════════════════╝
    """)
    app.run(host="127.0.0.1", port=PORT, debug=False)
