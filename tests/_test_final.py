"""Full test: Qwen3-8B vs 2.5-7B vs 30B-A3B for intent routing"""
import httpx, json, time, sys, os
sys.stdout.reconfigure(encoding="utf-8")

SF_KEY = "YOUR_SILICONFLOW_API_KEY"
SF_BASE = "https://api.siliconflow.cn/v1/chat/completions"

PROMPT = (
    "你是一个QQ群聊消息的意图分类器。"
    "根据用户消息输出意图类别，只输出一个词："
    "send_img(发图), vision(识图/看用户发的图片), "
    "chat(普通聊天), save_img(存图)。"
    "不要输出其他任何内容。"
)

tests = [
    ("发张萝莉图看看", "send_img"),
    ("发两张脚的色图", "send_img"),
    ("来点好康的", "send_img"),
    ("发色图给我", "send_img"),
    ("这张图里是啥", "vision"),
    ("识别这张图片", "vision"),
    ("帮我看看这个图", "vision"),
    ("今天天气不错啊", "chat"),
    ("晚上吃啥", "chat"),
    ("我去发个消息给老板", "chat"),
    ("这张图帮我存起来", "save_img"),
    ("存图到萝莉", "save_img"),
    ("保存这张", "save_img"),
    ("晚安兄弟们", "chat"),     # new: common chat
    ("你看这张怎么样", "vision"),  # new: ambiguous
]

models = ["Qwen/Qwen3-8B", "Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen3-30B-A3B-Instruct-2507"]

for model in models:
    print(f"\n{'='*60}")
    print(f"  Model: {model}")
    print(f"{'='*60}")
    correct = 0
    lats = []
    for text, expected in tests:
        start = time.time()
        with httpx.Client(timeout=15) as c:
            r = c.post(SF_BASE, json={
                "model": model,
                "messages": [
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": text}
                ],
                "max_tokens": 10,
                "temperature": 0.1,
            }, headers={"Authorization": f"Bearer {SF_KEY}"})
        ms = round((time.time() - start) * 1000)
        result = r.json()["choices"][0]["message"]["content"].strip()
        lats.append(ms)
        ok = result == expected
        if ok: correct += 1
        tag = "OK" if ok else f"X({expected})"
        # Only show failures with detail
        if ok:
            print(f"  [{ms:>4}ms] {text:<26s} {result:<12s} OK", flush=True)
        else:
            print(f"  [{ms:>4}ms] {text:<26s} {result:<12s} {tag}", flush=True)
    
    avg = sum(lats)/len(lats)
    print(f"  {'='*40}")
    print(f"  >> {correct}/{len(tests)} ({correct*100//len(tests)}%)  "
          f"avg:{avg:.0f}ms  min:{min(lats)}ms  max:{max(lats)}ms", flush=True)

# Summary
print("\n" + "=" * 60)
print("  SUMMARY")
print("=" * 60)
print(f"  {'Model':<40s} {'Acc':>5s} {'Avg ms':>7s}")
print(f"  {'-'*40} {'-'*5} {'-'*7}")
for m in models:
    print(f"  {m:<40s} {'?':>5s} {'?':>7s}")

# Cleanup
fp = os.path.join(os.path.dirname(__file__), "_test_final.py")
if os.path.exists(fp):
    os.remove(fp)
