"""测试 codex API 端点 (Responses API 格式)"""
import json, urllib.request

url = "https://new.sharedchat.cc/codex/v1/responses"
key = "sk-n9ADUSn7bbeGSUd716ZyjDK4b4p0fxJy"

payload = json.dumps({
    "model": "gpt-5.5",
    "input": "你好，用一句话介绍自己",
    "max_output_tokens": 200
}).encode()

req = urllib.request.Request(url, data=payload, headers={
    "Authorization": f"Bearer {key}",
    "Content-Type": "application/json"
})

try:
    proxy = urllib.request.ProxyHandler({"https": "http://127.0.0.1:7890"})
    opener = urllib.request.build_opener(proxy)
    with opener.open(req, timeout=30) as r:
        data = json.loads(r.read())
        print(f"状态码: {r.status}")
        print(f"完整返回: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}")
except urllib.error.HTTPError as e:
    print(f"HTTP错误 {e.code}: {e.read().decode()[:300]}")
except Exception as e:
    print(f"错误: {e}")
