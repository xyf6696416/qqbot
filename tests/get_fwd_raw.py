"""
获取转发消息原始数据工具
========================
连接到 NapCat WebSocket，获取转发消息（合并转发）的完整原始 JSON 数据。
即 bridge.py 中 get_forward_msg 的 RAW 输出，不做任何解析。

用法:
  python tests/get_fwd_raw.py <forward_id> [选项]

参数:
  <forward_id>    转发消息 ID（必填）
  --pretty, -p    格式化 JSON 输出（默认：单行紧凑）
  --ws-url URL    WebSocket 地址（默认：ws://127.0.0.1:18888）
  --output FILE, -o FILE  保存到文件（可选）
  --summary, -s   只打印概要，不输出完整 JSON

示例:
  python tests/get_fwd_raw.py 123456
  python tests/get_fwd_raw.py 123456 -p
  python tests/get_fwd_raw.py 123456 -p -o fwd_raw.json
  python tests/get_fwd_raw.py 123456 -s
"""

import argparse
import asyncio
import json
import os
import sys


async def fetch(fwd_id, ws_url, pretty):
    """通过 NapCat WebSocket 获取转发消息原始数据"""
    import websockets

    print(f"[WS] 正在连接 {ws_url} ...", file=sys.stderr)

    try:
        async with websockets.connect(ws_url) as ws:
            print("[WS] 连接成功 ✓", file=sys.stderr)

            # 收到第一条消息（心跳/元事件）
            try:
                first = await asyncio.wait_for(ws.recv(), timeout=3)
                data = json.loads(first)
                pt = data.get("post_type", "")
                mt = data.get("message_type", "")
                print(f"[WS] 心跳: post_type={pt} msg_type={mt}", file=sys.stderr)
            except asyncio.TimeoutError:
                print("[WS] 无心跳消息（继续）", file=sys.stderr)

            # 发送 get_forward_msg 请求
            echo = "fwd_raw_" + str(int(asyncio.get_event_loop().time() * 1000))
            request = {
                "action": "get_forward_msg",
                "params": {"id": int(fwd_id)},
                "echo": echo,
            }
            print(f"[WS] 发送: action=get_forward_msg id={fwd_id}", file=sys.stderr)
            await ws.send(json.dumps(request, ensure_ascii=False))

            # 等待匹配的响应
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    print("[ERROR] 等待响应超时（10s），NapCat 未返回结果。", file=sys.stderr)
                    sys.exit(1)

                resp = json.loads(raw)
                if resp.get("echo") != echo:
                    continue  # 跳过其它消息

                return resp

    except websockets.exceptions.WebSocketException as e:
        print(f"[ERROR] WebSocket 连接失败: {e}", file=sys.stderr)
        print(f"       确认 NapCat 已启动且地址正确（{ws_url}）", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] 请求失败: {e}", file=sys.stderr)
        sys.exit(1)


# ─── 输出 ─────────────────────────────────────────────

def output_json(data, pretty):
    """JSON 输出到 stdout"""
    if pretty:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        text = json.dumps(data, ensure_ascii=False)
    print(text)


def output_summary(data):
    """简要分析输出到 stderr"""
    status = data.get("status", "?")
    retcode = data.get("retcode", "?")

    if status == "ok":
        msgs = data.get("data", {}).get("messages", [])
        print(f"[OK] status={status} retcode={retcode} 节点数={len(msgs)}",
              file=sys.stderr)

        # 统计节点结构
        type_counts = {}
        for node in msgs:
            msg_type = node.get("message_type", "?")
            type_counts[msg_type] = type_counts.get(msg_type, 0) + 1
        if type_counts:
            print(f"     消息类型分布: {type_counts}", file=sys.stderr)

        for i, node in enumerate(msgs, 1):
            sender = node.get("sender", {})
            nickname = sender.get("nickname", "?")
            user_id = sender.get("user_id", "?")
            msg_type = node.get("message_type", "?")
            raw_msg = node.get("raw_message", "")
            content = node.get("message", node.get("content", ""))
            content_preview = str(raw_msg or content)[:80]
            print(f"  [{i}] {nickname}({user_id}) [{msg_type}]: {content_preview}",
                  file=sys.stderr)
    else:
        # 尝试修复乱码（NapCat 返回 GBK 编码的错误信息）
        try:
            wording = data.get("wording", "")
            if wording and not all(ord(c) < 128 for c in wording):
                # 已有 Unicode，直接显示
                pass
        except Exception:
            pass

        print(f"[FAIL] status={status} retcode={retcode}", file=sys.stderr)
        if "wording" in data:
            print(f"      原因: {data['wording']}", file=sys.stderr)
        elif data.get("message"):
            print(f"      原因: {data['message']}", file=sys.stderr)

        if data.get("data"):
            print(f"      data: {json.dumps(data['data'], ensure_ascii=False)[:200]}",
                  file=sys.stderr)


# ─── 入口 ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="获取转发消息原始数据（通过 NapCat WebSocket）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s 123456           # 获取 123456 的原始转发数据
  %(prog)s 123456 -p        # 格式化输出
  %(prog)s 123456 -p -o d.json  # 保存到文件
  %(prog)s 123456 -s        # 只看概要
        """,
    )
    parser.add_argument("forward_id", help="转发消息 ID")
    parser.add_argument("-p", "--pretty", action="store_true",
                       help="格式化 JSON 输出")
    parser.add_argument("-s", "--summary", action="store_true",
                       help="只打印概要，不输出完整 JSON")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:18888",
                       help="WebSocket 地址（默认: ws://127.0.0.1:18888）")
    parser.add_argument("-o", "--output", help="输出到文件（默认: stdout）")

    args = parser.parse_args()

    # 获取数据
    result = asyncio.run(fetch(args.forward_id, args.ws_url, args.pretty))

    # 输出完整 JSON（除非只概要）
    if not args.summary:
        output_json(result, args.pretty)

    # 打印概要
    output_summary(result)

    # 保存到文件
    if args.output:
        try:
            output_text = (
                json.dumps(result, ensure_ascii=False, indent=2)
                if args.pretty else
                json.dumps(result, ensure_ascii=False)
            )
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output_text)
                f.write("\n")
            print(f"[OK] 已保存到 {args.output}", file=sys.stderr)
        except OSError as e:
            print(f"[ERROR] 写入文件失败: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
