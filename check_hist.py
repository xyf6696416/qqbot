import asyncio, json, sys
from aiohttp import ClientSession

# 设置 stdout 编码
sys.stdout.reconfigure(encoding='utf-8')

async def test():
    session = ClientSession()
    ws = await session.ws_connect("ws://127.0.0.1:18888")
    try: await asyncio.wait_for(ws.receive(), timeout=2)
    except: pass
    
    # 获取群最新消息
    await ws.send_str(json.dumps({
        "action": "get_group_msg_history",
        "params": {"group_id": GROUP_ID, "count": 15},
        "echo": "hist"
    }))
    
    for i in range(5):
        try:
            resp = await asyncio.wait_for(ws.receive(), timeout=5)
            d = json.loads(resp.data)
            if d.get("echo") == "hist":
                msgs = d.get("data", {}).get("messages", [])
                for m in msgs:
                    txt = ""
                    for seg in m.get("message", []):
                        if seg.get("type") == "text":
                            txt += seg.get("data", {}).get("text", "")
                    uid = m.get("user_id", "?")
                    t = m.get("time", 0)
                    from datetime import datetime
                    ts = datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "?"
                    # 用 repr 避免编码问题
                    line = f"[{ts}] uid={uid} msg_id={m.get('message_id')}: {txt[:80]}"
                    print(line.encode('utf-8', errors='replace').decode('utf-8'))
                break
        except asyncio.TimeoutError:
            print("Timeout")
            break
    
    await ws.close()
    await session.close()

asyncio.run(test())
