import asyncio, json, websockets

async def test():
    try:
        ws = await asyncio.wait_for(websockets.connect("ws://127.0.0.1:18888"), timeout=5)
        print("WS connected OK")
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            print(f"Got message: post_type={data.get('post_type')} msg_type={data.get('message_type')} group_id={data.get('group_id')}")
        except asyncio.TimeoutError:
            print("No message received in 10 seconds")
        await ws.close()
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(test())
