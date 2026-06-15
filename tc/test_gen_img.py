"""测试 12AI API - 用更长超时"""
import json, http.client, time, base64

KEY = "sk-q654BxOLHiKEb6JhjEvQUCNyl3I9nhCU50RG3mghgUPX4wBD"
payload = {"model":"gpt-image-2","prompt":"cat","n":1,"size":"256x256","quality":"standard","response_format":"b64_json"}
body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
auth_val = ("Bearer " + KEY).encode("utf-8").decode("latin-1")

t0 = time.time()
print(f"{time.strftime('%H:%M:%S')} Connecting (timeout=180)...")
conn = http.client.HTTPSConnection("cdn.12ai.org", timeout=180)
try:
    conn.request("POST", "/v1/images/generations", body=body,
                 headers={"Authorization": auth_val, "Content-Type": "application/json; charset=utf-8"})
    print(f"{time.strftime('%H:%M:%S')} Waiting...")
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    j = json.loads(data)
    elapsed = time.time() - t0
    print(f"{time.strftime('%H:%M:%S')} HTTP {resp.status} ({elapsed:.0f}s)")
    if "error" in j:
        print(f"ERROR: {j['error']}")
    elif j.get("data") and j["data"][0].get("b64_json"):
        img = base64.b64decode(j["data"][0]["b64_json"])
        print(f"OK: {len(img)} bytes")
    else:
        print(f"UNEXPECTED: {json.dumps(j)[:200]}")
except Exception as e:
    elapsed = time.time() - t0
    print(f"{time.strftime('%H:%M:%S')} EXCEPTION after {elapsed:.0f}s: {e}")
finally:
    conn.close()
