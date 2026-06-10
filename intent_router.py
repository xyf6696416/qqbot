"""
意图路由模块
- 关键词匹配 + 参数提取
- 低置信度路由到 SF 兜底分类
"""

import os
import re
import unicodedata
from dataclasses import dataclass


@dataclass
class RefContext:
    """引用消息上下文"""
    type: str | None = None           # "image" | "forward" | "text" | None
    has_images: bool = False
    has_forward: bool = False
    cached_paths: list = None
    cached_count: int = 0
    forward_img_count: int = 0

    def __post_init__(self):
        if self.cached_paths is None:
            self.cached_paths = []


class IntentRouter:
    """意图路由：关键词匹配 + SF 兜底"""

    SEND_IMG = [r"发图|发张|发几张?|发一?张|来张|来[\d一二两三四五六七八九十百千零]+张|来点|来一?张|发色图|来个|色图|涩图"]
    SEND_IMG += [r"整点图|整?几张?|搞张图|给张图"]
    SEND_IMG += [r"来点好康"]
    SEND_IMG += [r"发出来|发到群里"]

    SAVE_IMG = [r"存图|保存|存起来|存一下|存这张|收图|收藏|收了"]
    SAVE_IMG += [r"(?<!缓)存到\w+|保存到\w+"]
    SAVE_IMG += [r"全存|全部存|都存了"]
    SAVE_IMG += [r"第\d+张存|第\d+张保存"]

    VISION = [r"这是啥|这是什么|这啥"]
    VISION += [r"图里|图片里"]
    VISION += [r"看看|让我看看|瞅瞅"]
    VISION += [r"识别|帮我看看|帮我看|帮我认"]
    VISION += [r"第\d+张看看"]
    VISION += [r"什么图|谁啊|是谁"]

    GEN_IMG = [r"/生图"]

    SRC_MAP = {
        "萝莉": ["萝莉", "loli"],
        "脚": ["脚", "足", "脚丫", "玉足"],
        "大雷": ["大雷"],
        "白穂": ["白穂", "白穗", "穗"],
        "轮奸": ["轮奸", "轮"],
    }

    @staticmethod
    def _cn2num(s: str) -> int:
        """中文数字转阿拉伯数字"""
        cn_map = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
                  "十": 10, "百": 100, "千": 1000}
        total = 0
        cur = 0
        for ch in s:
            v = cn_map.get(ch)
            if v is None:
                break
            if v >= 10:
                if cur == 0:
                    cur = v
                else:
                    cur *= v
                total += cur
                cur = 0
            else:
                cur += v
        return total + cur

    @staticmethod
    def extract_count(text):
        m = re.search(r"(\d+)张", text)
        if m:
            return min(int(m.group(1)), 30)
        m = re.search(r"([一二两三四五六七八九十百千零]+)张", text)
        if m:
            return min(IntentRouter._cn2num(m.group(1)), 30)
        return None

    @staticmethod
    def extract_src(text):
        # 去掉开头的发图命令前缀（如"色图""来张"），剩下的精确匹配文件夹名
        # 防止"色图蔡萝莉"中的"蔡萝莉"被"萝莉"子串误匹配
        SEND_IMG_PREFIX = (
            r"^(?:发图|发张|发几张?|发一?张|来张|来[\d一二两三四五六七八九十百千零]+张|"
            r"来点|来一?张|发色图|来个|色图|涩图|"
            r"整点图|整?几张?|搞张图|给张图|"
            r"来点好康|发出来|发到群里)\s*"
        )
        m = re.match(SEND_IMG_PREFIX, text)
        stripped = text[m.end():].strip() if m else text
        # NFKC 归一化，处理 "穂"(U+7A42) vs "穗"(U+7A57) 等异体字差异
        norm = lambda s: unicodedata.normalize("NFKC", s)
        n_stripped = norm(stripped)

        # 1) SRC_MAP 别名匹配（精确匹配 + NFKC 归一化 + 中文边界检查）
        for folder, keywords in IntentRouter.SRC_MAP.items():
            n_keywords = [norm(k) for k in keywords]
            n_folder = norm(folder)
            # 精确匹配整个 stripped 文本（NFKC 归一化）
            if n_stripped in n_keywords or n_stripped == n_folder:
                return folder
            # 子串匹配 + 边界检查：确保关键词不在另一个中文词中间
            for kw, n_kw in zip(keywords, n_keywords):
                pattern = re.compile(
                    rf"(?<![一-鿿]){re.escape(n_kw)}(?![一-鿿])"
                )
                if pattern.search(n_stripped):
                    return folder

        # 2) 扫描桌面 \转发图片\ 下的实际子文件夹名（NFKC 归一化 + 边界检查）
        fwd_dir = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片")
        try:
            for d in os.listdir(fwd_dir):
                if os.path.isdir(os.path.join(fwd_dir, d)):
                    n_d = norm(d)
                    if n_stripped == n_d:
                        return d
                    pattern = re.compile(
                        rf"(?<![一-鿿]){re.escape(n_d)}(?![一-鿿])"
                    )
                    if pattern.search(n_stripped):
                        return d
        except OSError:
            pass
        return None

    @staticmethod
    def extract_save_to(text):
        m = re.search(r"(?<!缓)(?:存到|保存到)\s*(\w+)", text)
        return m.group(1) if m else None

    @staticmethod
    def extract_nth(text):
        m = re.search(r"第(\d+)张", text)
        return int(m.group(1)) if m else None

    @staticmethod
    def extract_braced_prompt(text):
        """提取【】中的内容作为生图 prompt"""
        m = re.search(r'【(.+?)】', text)
        return m.group(1).strip() if m else None

    @classmethod
    def keyword_route(cls, text, has_image=False, has_ref_image=False, has_ref_forward=False):
        txt = text.lower()

        # 0) gen_img — 优先检查，避免 st 开头的生图 prompt 被发图关键词截胡
        for p in cls.GEN_IMG:
            if re.search(p, txt):
                return {"intent": "gen_img", "confidence": "medium", "params": {}}

        # 1) save_img
        for p in cls.SAVE_IMG:
            if re.search(p, txt):
                return {"intent": "save_img", "confidence": "high", "params": {
                    "save_to": cls.extract_save_to(text),
                    "target_index": cls.extract_nth(text),
                }}

        # 2) send_img
        for p in cls.SEND_IMG:
            if re.search(p, txt):
                return {"intent": "send_img", "confidence": "high", "params": {
                    "count": cls.extract_count(text),
                    "src": cls.extract_src(text),
                }}

        # 3) vision 关键词
        for p in cls.VISION:
            if re.search(p, txt):
                return {"intent": "vision", "confidence": "medium", "params": {
                    "target_index": cls.extract_nth(text),
                }}

        # 4) 用户自己发了图片（非引用）→ 默认 vision
        if has_image:
            return {"intent": "vision", "confidence": "high", "params": {}}

        # 5) 引用转发无关键词 → low confidence
        if has_ref_forward:
            return {"intent": "chat", "confidence": "low", "params": {}}

        # 6) 其他 → chat
        return {"intent": "chat", "confidence": "high", "params": {}}
