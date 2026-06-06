"""
图片发送工具 — 通过 NapCat WebSocket 发送图片到 QQ 群。

用法：
    from tc.send_image import send_image
    asyncio.run(send_image(group_id=123456, image_url="http://..."))
    
    或命令行：
    python tc/send_image.py --group 123456 --url http://...
"""

import asyncio
import json
import argparse

try:
    import websockets
except ImportError:
    raise ImportError("缺少 websockets 库，请运行: pip install websockets")

NAPCAT_WS = "ws://127.0.0.1:18888"


async def send_image(
    group_id: int,
    image_url: str,
    caption: str = "",
    ws_url: str = NAPCAT_WS,
) -> dict:
    """
    向 QQ 群发送图文消息。

    参数：
        group_id: 目标群号
        image_url: 图片 URL（支持 http://, https://, base64://, 本地路径）
        caption:  图片说明文字（可选）
        ws_url:   NapCat WebSocket 地址
    返回：
        NapCat 的响应 dict
    """
    async with websockets.connect(ws_url) as ws:
        # 吃掉 NapCat 的 lifecycle connect 事件
        try:
            await asyncio.wait_for(ws.recv(), timeout=3)
        except asyncio.TimeoutError:
            pass

        message = []
        if caption:
            message.append({"type": "text", "data": {"text": caption + "\n"}})
        message.append({"type": "image", "data": {"file": image_url}})

        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": message,
            },
            "echo": f"img_{group_id}_{asyncio.get_event_loop().time():.0f}",
        }

        await ws.send(json.dumps(payload, ensure_ascii=False))
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        return json.loads(resp)


async def send_text(group_id: int, text: str, ws_url: str = NAPCAT_WS) -> dict:
    """向 QQ 群发送纯文本消息。"""
    async with websockets.connect(ws_url) as ws:
        # 吃掉 lifecycle
        try:
            await asyncio.wait_for(ws.recv(), timeout=3)
        except asyncio.TimeoutError:
            pass

        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": text,
            },
            "echo": f"txt_{group_id}_{asyncio.get_event_loop().time():.0f}",
        }
        await ws.send(json.dumps(payload, ensure_ascii=False))
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        return json.loads(resp)


# ── 命令行入口 ──
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="通过 NapCat 向 QQ 群发送图片")
    parser.add_argument("--group", type=int, required=True, help="目标群号")
    parser.add_argument("--url", type=str, required=True, help="图片 URL")
    parser.add_argument("--caption", type=str, default="", help="图片说明文字")
    args = parser.parse_args()

    result = asyncio.run(send_image(args.group, args.url, args.caption))
    print("NapCat 响应:", json.dumps(result, ensure_ascii=False, indent=2))
