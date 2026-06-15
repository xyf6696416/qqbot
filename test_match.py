"""测试 _match_direct_src 能否匹配到文件夹名"""
import os
import sys
import unicodedata

# 载入模块
sys.path.insert(0, ".")
from intent_router import IntentRouter

fwd_dir = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片")

# 1) 检查所有文件夹的实际字符
print("=" * 60)
print("桌面转发图片下的文件夹:")
for d in sorted(os.listdir(fwd_dir)):
    full = os.path.join(fwd_dir, d)
    if os.path.isdir(full):
        chars = [(c, hex(ord(c))) for c in d]
        nfkc = unicodedata.normalize("NFKC", d)
        print(f"  [{d}] chars={chars} NFKC=[{nfkc}] chars={[hex(ord(c)) for c in nfkc]}")

# 2) 检查 SRC_MAP
print("\n" + "=" * 60)
print("SRC_MAP 内容:")
for folder, keywords in IntentRouter.SRC_MAP.items():
    print(f"  [{folder}] chars={[hex(ord(c)) for c in folder]}")
    for k in keywords:
        print(f"    -> [{k}] chars={[hex(ord(c)) for c in k]}")

# 3) 模拟 _match_direct_src 的匹配逻辑
print("\n" + "=" * 60)
print("匹配测试:")
norm = lambda s: unicodedata.normalize("NFKC", s)

test_cases = ["白穗", "白穂", "百穗", "穗", "白穗图", "发白穗", "来点白穗"]
for tc in test_cases:
    n_tc = norm(tc)
    print(f"\n测试输入: [{tc}] chars={[hex(ord(c)) for c in tc]} NFKC=[{n_tc}]")

    # SRC_MAP 匹配
    for folder, keywords in IntentRouter.SRC_MAP.items():
        if tc in keywords:
            print(f"  -> SRC_MAP精确匹配: {folder}")
        elif n_tc in [norm(k) for k in keywords]:
            print(f"  -> SRC_MAP(NFKC): {folder}")

    # 文件夹名匹配
    for d in os.listdir(fwd_dir):
        if os.path.isdir(os.path.join(fwd_dir, d)):
            if n_tc == norm(d):
                print(f"  -> 文件夹名匹配(NFKC): {d}")
