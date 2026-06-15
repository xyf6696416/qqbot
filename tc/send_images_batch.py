"""
批量图片发送工具 — 多张图一次性发送（同一条消息）
用法: python send_images_batch.py --group 1026442086 --urls url1,url2
"""

import asyncio, json, sys, argparse

try:
    import websockets
except ImportError:
    raise ImportError("缺少 websockets 库，请运行: pip install websockets")

NAPCAT_WS = "ws://127.0.0.1:18888"


async def send_images_batch(group_id: int, image_urls: list[str], ws_url: str = NAPCAT_WS) -> dict:
    """一次 WebSocket 连接发送多张图片"""
    async with websockets.connect(ws_url) as ws:
        try:
            await asyncio.wait_for(ws.recv(), timeout=3)
        except asyncio.TimeoutError:
            pass

        message = [{"type": "image", "data": {"file": url}} for url in image_urls]

        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": message,
            },
            "echo": f"batch_{group_id}_{asyncio.get_event_loop().time():.0f}",
        }

        await ws.send(json.dumps(payload, ensure_ascii=False))
        resp = await asyncio.wait_for(ws.recv(), timeout=15)
        return json.loads(resp)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="批量发送多张图片到QQ群")
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument("--urls", type=str, required=True, help="图片URL列表，逗号分隔")
    args = parser.parse_args()

    urls = [u.strip() for u in args.urls.split(",") if u.strip()]
    result = asyncio.run(send_images_batch(args.group, urls))
    print("批量发送结果:", json.dumps(result, ensure_ascii=False, indent=2))
