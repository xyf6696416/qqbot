"""
转发图片/插画 挑选工具
支持从桌面 转发图片 目录或其子文件夹选图

用法:
  python pick_fwd_image.py [数量]
  python pick_fwd_image.py [数量] --src <子文件夹名>
  python pick_fwd_image.py --reset
  python pick_fwd_image.py --src 萝莉 --reset

示例:
  python pick_fwd_image.py 3              # 从桌面转发图片选3张
  python pick_fwd_image.py 1 --src 萝莉   # 从桌面 转发图片\萝莉 选1张
  python pick_fwd_image.py --reset         # 清空已发记录
"""

import json, os, sys, random, argparse
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

FWD_DIR = Path(r"C:\Users\Administrator\Desktop\转发图片")
STATE_FILE = Path(__file__).parent / ".pick_fwd_state.json"

# --gif 模式：只选 GIF；默认排除 GIF（只选静态图）
_GIF_MODE = False


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_source_dir(src_name):
    """根据 src 参数返回源目录"""
    if not src_name:
        return FWD_DIR
    return FWD_DIR / src_name


def get_all_images(src_dir):
    """返回指定目录下所有图片文件路径"""
    if not src_dir.exists():
        print(f"错误：目录不存在 {src_dir}", file=sys.stderr)
        sys.exit(1)

    exts = {".gif"} if _GIF_MODE else {".jpg", ".jpeg", ".png", ".webp"}
    return sorted(
        p for p in src_dir.iterdir()
        if p.is_file() and p.suffix.lower() in exts
    )


def get_all_images_all_src():
    """没有指定 src 时，扫描所有子文件夹合并所有图片"""
    exts = {".gif"} if _GIF_MODE else {".jpg", ".jpeg", ".png", ".webp"}
    all_images = []
    for d in sorted(FWD_DIR.iterdir()):
        if d.is_dir():
            all_images.extend(
                p for p in d.iterdir()
                if p.is_file() and p.suffix.lower() in exts
            )
    return all_images


def get_state_key(src_name):
    """按来源区分已发送录"""
    if not src_name:
        return "_all_folders"
    return f"src_{src_name}"


def get_available(all_images, state, state_key, use_relpath=False):
    """找出还没发过的图片"""
    sent_names = set(state.get(state_key, []))
    if use_relpath:
        return [img for img in all_images if str(img.relative_to(FWD_DIR)) not in sent_names]
    return [img for img in all_images if img.name not in sent_names]


def main():
    parser = argparse.ArgumentParser(
        description="从桌面 转发图片 目录或其子文件夹随机选图，不重复",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s 3                # 从桌面转发图片选3张
  %(prog)s 1 --src 萝莉     # 从桌面 转发图片\\萝莉 选1张
  %(prog)s --reset          # 清空已发记录
  %(prog)s --src 萝莉 --reset  # 清空萝莉已发记录
        """
    )
    parser.add_argument("count", nargs="?", type=int, default=None, help="选取数量")
    parser.add_argument("--src", type=str, default=None, help="转发图片下的子文件夹名，如: 萝莉, 同人, 色彩图")
    parser.add_argument("--reset", action="store_true", help="清空已发记录")
    parser.add_argument("--list-src", action="store_true", help="列出可用的子文件夹")
    parser.add_argument("--gif", action="store_true", help="只选 GIF 文件（默认排除 GIF）")

    args = parser.parse_args()
    global _GIF_MODE
    _GIF_MODE = args.gif

    # 列表模式
    if args.list_src:
        exts = {".gif"} if _GIF_MODE else {".jpg", ".jpeg", ".png", ".webp"}
        subdirs = [d for d in FWD_DIR.iterdir() if d.is_dir()]
        if not subdirs:
            print("桌面转发图片下没有子文件夹")
        for d in sorted(subdirs):
            count = len([f for f in d.iterdir() if f.is_file() and f.suffix.lower() in exts])
            print(f"  {d.name} ({count} 张)")
        sys.exit(0)

    src_name = args.src
    src_dir = get_source_dir(src_name)
    state_key = get_state_key(src_name)

    # 重置模式
    if args.reset:
        state = load_state()
        state[state_key] = []
        save_state(state)
        label = str(src_dir)
        print(f"已发记录已清空 ({label})")
        sys.exit(0)

    # 选择模式
    if args.count is None:
        parser.print_help()
        sys.exit(0)

    count = args.count
    state = load_state()
    if src_name:
        all_images = get_all_images(src_dir)
    else:
        all_images = get_all_images_all_src()
    available = get_available(all_images, state, state_key, use_relpath=not src_name)

    if not available:
        # 全部发过了，自动重置
        state[state_key] = []
        available = get_available(all_images, state, state_key, use_relpath=not src_name)

    # 顺序窗口选图：按文件名排序 → 随机起始位取连续 count 张
    # 保证图片按时间顺序发送，不跳图不漏图（与 GIF 选图逻辑一致）
    if len(available) <= count:
        picks = available[:]
    else:
        start = random.randint(0, len(available) - count)
        picks = available[start:start + count]

    state.setdefault(state_key, [])
    if src_name:
        state[state_key].extend([img.name for img in picks])
    else:
        # 用相对路径去重（含子文件夹名，防不同文件夹同名文件冲突）
        state[state_key].extend([str(img.relative_to(FWD_DIR)) for img in picks])
    save_state(state)

    for img in picks:
        print(img)


if __name__ == "__main__":
    main()
