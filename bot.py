"""
Bot 核心模块
- WebSocket 连接管理 (NapCat)
- 消息解析与队列
- 意图处理 (chat / vision / send_img / save_img / gen_img / cmd)
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
import unicodedata
import uuid
from collections import deque
from datetime import datetime

import yaml

import httpx
from aiohttp import ClientSession, WSMsgType

from config import (
    NAPCAT_WS,
    TRIGGER_KW, ADMIN_UIDS, FORBIDDEN_OPS,
    GROUP_PROMPTS, DEFAULT_PROMPT, AUTO_CLEAR_GROUPS, AUTO_SE_TU_GROUPS,
    SF_KEY, SF_MODEL, SF_CHAT_MODEL, IMG_PORT, IMG_URL, IMG_STORAGE,
    MAX_CONTEXT, MAX_IMAGE_SIZE, GEN_IMG_DIR, GEN_IMG_DAILY_LIMIT,
    PICK_MODE, PICK_SKIP,
)
from intent_router import IntentRouter, RefContext
from image_utils import ImageServer, compress_image

from image_dedup import ImageDeduplicator
from gen_img import gen_image, _log_gen_img, _get_gen_img_key

# 插件系统
from mod import PluginManager, EventBus, MessageEvent, ParsedMessageEvent, IntentEvent

log = logging.getLogger("gw")


class Bot:
    def __init__(self):
        self.img = ImageServer()
        self.ws_url = NAPCAT_WS
        self._ws = None
        self._session = None
        self.self_id = None
        self._echo = 0
        self.nicknames = {}
        # 生图额度跟踪: {"YYYY-MM-DD|gid|uid": count}
        self.gen_img_usage = {}
        # 每日发图计数: {"YYYY-MM-DD|gid": count}
        self.daily_img_usage = {}
        # Group message context: {group_id: deque([(user_id, text, ts), ...])}
        self.context = {}
        # 消息队列：每个群一个队列，保证按序处理
        self.group_queues: dict[str, asyncio.Queue] = {}
        self.group_workers: dict[str, asyncio.Task] = {}
        # 涩图冷却：{group_id: last_trigger_time}
        self.se_tu_cooldown: dict[str, float] = {}
        # 定时涩图任务：{group_id: asyncio.Task}
        self._se_tu_tasks: dict[str, asyncio.Task] = {}
        # 复用 httpx 客户端（连接池）
        self._sf_client = httpx.AsyncClient(timeout=60)
        # 优雅关闭标志
        self._shutdown = False
        # 启动时清理 tc/1/ 旧文件（>24h）
        self._cleanup_tc_files()
        # 发送队列（每条消息间隔 3 秒，防封）
        self.send_queue = asyncio.Queue()
        self._send_worker_task = None

        # 图片去重器（基于 phash，全局去重 ~/Desktop/转发图片/）
        self.dedup = ImageDeduplicator()

        # 插件系统
        self.plugin_manager = PluginManager(self)
        self._bus = EventBus()

        # 配置文件缓存（支持热加载）
        self._group_configs = GROUP_PROMPTS
        self._auto_clear_groups = AUTO_CLEAR_GROUPS
        self._trigger_kw = TRIGGER_KW
        self._admin_uids = ADMIN_UIDS
        self._forbidden_ops = FORBIDDEN_OPS
        self._pick_mode = PICK_MODE
        self._pick_skip = PICK_SKIP

        # 标记热加载配置可用（_reload_config 已定义）
        if not hasattr(self, '_reload_config'):
            pass

    def _cleanup_tc_files(self):
        """清理 tc/1/ 中超过 24 小时的临时文件"""
        tc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tc", "1")
        if not os.path.isdir(tc_dir):
            return
        now = time.time()
        cutoff = now - 86400  # 24h
        cleaned = 0
        for fname in os.listdir(tc_dir):
            fpath = os.path.join(tc_dir, fname)
            try:
                if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    cleaned += 1
            except OSError:
                pass
        if cleaned:
            log.info("TC_CLEANUP: removed %d stale files from tc/1/", cleaned)

    async def run(self):
        await self.img.ensure_running()
        self._send_worker_task = asyncio.create_task(self._send_worker())
        self._start_se_tu_schedulers()

        # 加载插件
        plugin_count = await self.plugin_manager.load_all()
        await self._bus.emit("bot_start", {"plugin_count": plugin_count})
        log.info("PLUGIN_SYSTEM: loaded %d plugin(s)", plugin_count)

        # 热加载循环心跳
        self._hot_reload_task = asyncio.create_task(self._hot_reload_loop())
        while not self._shutdown:
            try:
                await self.connect()
                async for msg in self._ws:
                    if self._shutdown:
                        break
                    if msg.type == WSMsgType.TEXT:
                        await self._handle(msg.data)
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
            except Exception as e:
                if not self._shutdown:
                    log.error("Conn err: %s", e)
            await asyncio.sleep(5)

    async def _hot_reload_loop(self):
        """每 5 秒检查一次插件热加载和命令标记。"""
        HOT_RELOAD_INTERVAL = 5
        while not self._shutdown:
            await asyncio.sleep(HOT_RELOAD_INTERVAL)
            try:
                n = await self.plugin_manager.check_hot_reload()
                if n > 0:
                    log.info("HOT_RELOAD: %d plugin(s) reloaded", n)
            except Exception as e:
                log.debug("HOT_RELOAD_LOOP_ERR: %s", str(e)[:100])

    async def connect(self):
        self._session = ClientSession()
        self._ws = await self._session.ws_connect(self.ws_url)

    async def send(self, action, params):
        self._echo += 1
        eid = f"e{self._echo}_{int(time.time())}"
        await self._ws.send_str(json.dumps(
            {"action": action, "params": params, "echo": eid}, ensure_ascii=False))
        return eid

    async def req(self, action, params, timeout=8):
        self._echo += 1
        eid = f"r{self._echo}_{int(time.time())}"
        await self._ws.send_str(json.dumps(
            {"action": action, "params": params, "echo": eid}, ensure_ascii=False))
        try:
            while True:
                resp = await asyncio.wait_for(self._ws.receive(), timeout=timeout)
                if resp.type == WSMsgType.TEXT:
                    d = json.loads(resp.data)
                    if d.get("echo") == eid:
                        return d
        except Exception:
            return None

    def _is_admin(self, uid):
        return str(uid) in (getattr(self, '_admin_uids', ADMIN_UIDS))

    def _has_forbidden_op(self, text):
        for kw in (getattr(self, '_forbidden_ops', FORBIDDEN_OPS)):
            if kw in text:
                return True
        return False

    async def _handle(self, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return
        if data.get("self_id"):
            self.self_id = data.get("self_id")

        # 检查热加载标记（每条消息处理前，降低延迟）
        self._check_reload_flag()

        if data.get("echo"):
            return
        if data.get("post_type") == "message" and data.get("message_type") == "group":
            await self._handle_group(data)

    # ─── 转发消息解析 ──────────────────────────────────

    async def _parse_forward_nodes(self, nodes, fwd_img_urls=None, depth=0):
        if depth > 3:
            return "(嵌套过深，已截断)"
        lines = []
        for n in nodes:
            nick = n.get("sender", {}).get("nickname", "未知")
            ts = n.get("time", 0)
            t_str = datetime.fromtimestamp(ts).strftime("%H:%M")
            raw_content = n.get("message", n.get("content", None))
            if raw_content is None:
                raw_content = []
            if isinstance(raw_content, list) and len(raw_content) == 0:
                log.info("FORWARD_EMPTY_NODE: sender=%s keys=%s", nick, list(n.keys()))

            is_nested_forward = False
            if isinstance(raw_content, list):
                for seg in raw_content:
                    if seg.get("type") == "forward":
                        is_nested_forward = True
                        break
                if not is_nested_forward and len(raw_content) == 0:
                    raw_msg = n.get("raw_message", "")
                    if raw_msg and '[CQ:forward' in raw_msg:
                        is_nested_forward = True
            elif isinstance(raw_content, str):
                if '[CQ:forward' in raw_content:
                    is_nested_forward = True
                else:
                    raw_msg = n.get("raw_message", "")
                    if raw_msg and '[CQ:forward' in raw_msg:
                        is_nested_forward = True

            if is_nested_forward:
                lines.append(f"[{t_str}] {nick}: [嵌套转发(无法展开)]")
                continue

            msg_parts = []
            if isinstance(raw_content, str):
                cq_clean = re.sub(r'\[CQ:[^\]]*\]', '', raw_content)
                if cq_clean.strip():
                    msg_parts.append(cq_clean.strip())
                if '[CQ:image' in raw_content or '[CQ:video' in raw_content:
                    msg_parts.append("[图片/视频]")
                    url_m = re.search(r'url=([^,\]]+)', raw_content)
                    if url_m and fwd_img_urls is not None:
                        url = url_m.group(1)
                        if url.startswith("http"):
                            fwd_img_urls.append(url)
                        else:
                            log.info("FORWARD_CQ_VIDEO_SKIP: url=%s", url[:80])
            elif isinstance(raw_content, list):
                for seg in raw_content:
                    st2 = seg.get("type", "")
                    sd2 = seg.get("data", {})
                    if st2 == "text":
                        msg_parts.append(sd2.get("text", ""))
                    elif st2 == "image":
                        msg_parts.append("[图片]")
                        u2 = sd2.get("url", "")
                        if u2 and u2.startswith("http") and fwd_img_urls is not None:
                            fwd_img_urls.append(u2)
                    elif st2 == "video":
                        msg_parts.append("[视频]")
                        u2 = sd2.get("url") or sd2.get("file") or ""
                        if u2:
                            if fwd_img_urls is not None:
                                fwd_img_urls.append(u2)
                        else:
                            log.info("FORWARD_VIDEO_SKIP: data=%s", str(sd2)[:200])
                    elif st2 == "face":
                        msg_parts.append("[表情]")
                    elif st2 == "at":
                        msg_parts.append("@某人")
                    elif st2 == "mface":
                        msg_parts.append("[动画表情]")
                    elif st2 == "gift":
                        msg_parts.append("[礼物]")
                    else:
                        msg_parts.append(f"[{st2}]")
            if not msg_parts:
                raw_msg = n.get("raw_message", "")
                if raw_msg:
                    cq_clean = re.sub(r'\[CQ:[^\]]*\]', '', raw_msg).strip()
                    if cq_clean:
                        msg_parts.append(cq_clean)
                    if '[CQ:image' in raw_msg or '[CQ:video' in raw_msg:
                        msg_parts.append("[图片/视频]")
            content_text = "".join(msg_parts) if msg_parts else "(消息)"
            lines.append(f"[{t_str}] {nick}: {content_text}")

        return "\n".join(lines)

    @staticmethod
    def _detect_image_ext(path):
        """读取文件头魔数判断真实图片格式。"""
        try:
            with open(path, "rb") as f:
                head = f.read(12)
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return ".gif"
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return ".png"
            if head[:2] == b"\xff\xd8":
                return ".jpg"
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                return ".webp"
            if head[:2] == b"BM":
                return ".bmp"
            if head[4:8] == b"ftyp":
                return ".mp4"
        except Exception:
            pass
        return ".jpg"  # 默认

    async def _cache_fwd_images(self, urls):
        if not urls:
            return []
        cached = []
        img_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tc", "1")
        os.makedirs(img_dir, exist_ok=True)
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 复用 httpx 客户端（用于外部图片下载）
        for idx, url in enumerate(urls, 1):
            try:
                tmp = os.path.join(img_dir, f"tmp_{now}_{idx}")
                if url.startswith("http"):
                    async with httpx.AsyncClient(timeout=30, verify=False) as hc:
                        resp = await hc.get(url)
                        if resp.status_code == 200:
                            with open(tmp, "wb") as _f:
                                _f.write(resp.content)
                        else:
                            log.warning("CACHE_IMG_HTTP_ERR: url=%s status=%d", url[:50], resp.status_code)
                            continue
                elif os.path.isfile(url):
                    import shutil
                    shutil.copy2(url, tmp)
                else:
                    fname_only = os.path.basename(url)
                    resolved = False
                    try:
                        async with httpx.AsyncClient() as hc:
                            resp = await hc.post(
                                "http://127.0.0.1:5283/api/get_file",
                                json={"file": fname_only},
                                timeout=5)
                            if resp.status_code == 200:
                                data = resp.json().get("data", {})
                                file_url = data.get("url") or data.get("file", "")
                                if file_url and file_url.startswith("http"):
                                    async with httpx.AsyncClient(timeout=30) as hc2:
                                        r2 = await hc2.get(file_url)
                                        if r2.status_code == 200:
                                            with open(tmp, "wb") as _f:
                                                _f.write(r2.content)
                                            resolved = True
                                elif file_url and os.path.isfile(file_url):
                                    shutil.copy2(file_url, tmp)
                                    resolved = True
                    except Exception as api_e:
                        log.info("CACHE_IMG_API_FAIL: url=%s err=%s",
                                 url[:40], str(api_e)[:60])
                    if not resolved:
                        log.info("CACHE_IMG_SKIP: url=%s not available", url[:50])
                        continue
                ext = self._detect_image_ext(tmp)
                fname = f"fwd_{now}_{idx}{ext}"
                dst = os.path.join(img_dir, fname)
                if tmp != dst:
                    os.rename(tmp, dst)
                cached.append(dst)
            except Exception as e:
                log.warning("CACHE_IMG_ERR: url=%s err=%s", url[:50], str(e)[:100])
        return cached

    def _extract_imgs(self, segs):
        urls = []
        for s in segs:
            s_type = s.get("type")
            if s_type in ("image", "video"):
                u = s.get("data", {}).get("url") or s.get("data", {}).get("file") or ""
                if u:
                    urls.append(u)
        return urls

    async def _extract_collection_imgs(self, collection_json):
        """解析 QQ 收藏笔记，从 sharechain 页面提取所有图片 URL 并缓存"""
        import re as _re
        import httpx as _httpx

        jump_url = collection_json.get("meta", {}).get("news", {}).get("jumpUrl", "")
        if not jump_url:
            log.warning("COLLECTION: no jumpUrl")
            return []

        log.info("COLLECTION: fetching %s", jump_url)
        try:
            async with _httpx.AsyncClient(timeout=15, verify=False) as cli:
                resp = await cli.get(jump_url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    log.warning("COLLECTION: HTTP %d", resp.status_code)
                    return []
                html = resp.text
        except Exception as e:
            log.warning("COLLECTION: fetch err %s", str(e)[:80])
            return []

        # 从 preview URL 提取 user_id（如 653020384），然后从 HTML 提取所有 collector 图片
        preview = collection_json.get("meta", {}).get("news", {}).get("preview", "")
        uid_match = _re.search(r'collector/(\d+)/', preview)
        collector_uid = uid_match.group(1) if uid_match else "653020384"
        img_urls = sorted(set(
            f"https://shp.{m}" for m in _re.findall(
                rf'qpic\.cn/collector/{collector_uid}/[a-f0-9-]+/', html)
        ))
        if not img_urls:
            log.warning("COLLECTION: no images found in sharechain page")
            return []

        log.info("COLLECTION: found %d images from sharechain", len(img_urls))
        cached = await self._cache_fwd_images(img_urls)
        log.info("COLLECTION: cached %d images", len(cached))
        return cached

    def _build_context(self, gid, current_text=""):
        ctx = self.context.get(str(gid), deque(maxlen=MAX_CONTEXT))
        if not ctx:
            return ""
        lines = []
        for uid, text, ts in ctx:
            t = datetime.fromtimestamp(ts).strftime("%H:%M")
            lines.append(f"[{t}] 用户{uid}: {text}")
        return "\n".join(lines[-50:])

    def _cache_msg(self, gid, uid, text):
        gs = str(gid)
        if gs not in self.context:
            self.context[gs] = deque(maxlen=MAX_CONTEXT)
        self.context[gs].append((uid, text, time.time()))

    async def _call_sf_chat(self, system_prompt, context_str, user_text):
        """通过 SiliconFlow 聊天 API 生成回复（OC 关闭时替代）"""
        msgs = [{"role": "system", "content": system_prompt}]
        if context_str:
            msgs.append({"role": "system", "content": f"【近期群消息上下文】\n{context_str}"})
        msgs.append({"role": "user", "content": user_text})
        try:
            r = await self._sf_client.post(
                "https://api.siliconflow.cn/v1/chat/completions",
                json={
                    "model": SF_CHAT_MODEL,
                    "messages": msgs,
                    "max_tokens": 300,
                    "temperature": 0.7,
                }, headers={"Authorization": f"Bearer {SF_KEY}"})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.error("SF_CHAT_ERR: %s", str(e)[:200])
            return None

    async def _vision_raw(self, url):
        try:
            r = await self._sf_client.get(url)
            if r.status_code != 200:
                return None
            b64 = base64.b64encode(r.content).decode()
            r2 = await self._sf_client.post(
                "https://api.siliconflow.cn/v1/chat/completions",
                json={
                    "model": SF_MODEL,
                    "messages": [
                        {"role": "system", "content": (
                            "你是一个图片描述助手。请仔细看图，用一段自然的话详细介绍图片内容："
                            "画面里有什么人、什么物体、什么场景，颜色构图风格如何，有没有文字。"
                            "客观描述，不要脑补画面没有的东西，不要回答图片之外的问题。"
                            "控制在100字以内，不分点。")},
                        {"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                            {"type": "text", "text": "请详细介绍这张图片的内容。"},
                        ]},
                    ],
                    "max_tokens": 1024,
                }, headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {SF_KEY}",
                })
            r2.raise_for_status()
            return r2.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.error("SF vision err: %s", str(e)[:200])
            return None

    # ─── 消息处理入口 ──────────────────────────────────

    async def _handle_group(self, data):
        gid = str(data["group_id"])
        uid = str(data["user_id"])
        msg = data.get("message", [])
        if uid == str(self.self_id):
            return

        # ── 插件事件：原始消息到达 ──────────────────────
        msg_event = MessageEvent(
            group_id=gid, user_id=uid,
            raw_data=data, raw_message=data.get("raw_message", ""),
            message=msg,
        )
        result = await self._bus.emit("message", msg_event, cancellable=True)
        if result.consumed:
            log.info("PLUGIN_CONSUMED: gid=%s uid=%s msg=message", gid, uid)
            return

        # 管理员 /add 命令：不受白名单限制
        raw_text = ""
        for s in msg:
            if s.get("type") == "text":
                raw_text += s.get("data", {}).get("text", "")
        if str(uid) in (getattr(self, '_admin_uids', ADMIN_UIDS)) and raw_text.strip().lower() == "/add":
            log.info("CMD_ADD_BYPASS: gid=%s uid=%s", gid, uid)
            await self._handle_add_group(gid, uid)
            return

        if str(gid) not in (getattr(self, '_group_configs', GROUP_PROMPTS)):
            log.info("GROUP_NOT_IN_WHITELIST: gid=%s", gid)
            return

        log.info("[%s] <%s> raw_message=%s", gid, uid, data.get("raw_message", "")[:100])
        seg_types = [(s.get("type"), list(s.get("data", {}).keys())) for s in msg]
        log.info("MSG_SEGMENTS: %s", seg_types)

        # Parse message
        reply = False
        text = ""
        img_urls = []
        rid = None
        at_bot = False
        _all_cached_paths = []
        for s in msg:
            st = s.get("type", "")
            sd = s.get("data", {})
            if st == "at" and str(sd.get("qq", "")) == str(self.self_id):
                reply = True
                at_bot = True
            elif st == "reply":
                rid = sd.get("id")
            elif st == "text":
                text += sd.get("text", "")
            elif st == "image":
                u = sd.get("url", "")
                if u:
                    img_urls.append(u)
                    text += " [图片]"
            elif st == "video":
                u = sd.get("url", "")
                if u and u.startswith("http"):
                    img_urls.append(u)
                    text += " [视频]"
                log.info("VIDEO_DEBUG: gid=%s data=%s", gid, str(sd)[:300])
            elif st == "forward":
                fid = sd.get("id", "")
                log.info("FORWARD_DEBUG: fid=%s text_before=%s", fid, text[:50] if text else "(empty)")
                if fid:
                    fwd = await self.req("get_forward_msg", {"id": fid}, timeout=15)
                    log.info("FORWARD_DEBUG: get_forward_msg result=%s",
                             str(fwd)[:200] if fwd else "None")
                    if fwd and fwd.get("status") == "ok":
                        nodes = fwd.get("data", {}).get("messages", [])
                        if nodes:
                            fwd_imgs = []
                            forward_text = await self._parse_forward_nodes(nodes, fwd_imgs)
                            text += "\n【合并转发】\n" + forward_text
                            if fwd_imgs:
                                cached = await self._cache_fwd_images(fwd_imgs)
                                _all_cached_paths.extend(cached)
                                text += f"\n(转发中包含{len(fwd_imgs)}张图片，已缓存到图床)"

        # Check quoted msg
        if rid:
            q = await self.req("get_msg", {"message_id": rid})
            if q and q.get("status") == "ok":
                q_msg = q.get("data", {}).get("message", [])
                q_imgs = self._extract_imgs(q_msg)
                img_urls = q_imgs + img_urls
                if q_imgs:
                    q_img_cached = await self._cache_fwd_images(q_imgs)
                    _all_cached_paths.extend(q_img_cached)
                quoted_forward = None
                quoted_collection = None  # QQ 收藏
                for seg in q_msg:
                    st = seg.get("type")
                    sd = seg.get("data", {})
                    if st == "forward":
                        quoted_forward = sd.get("id", "")
                        break
                    if st == "json":
                        try:
                            jd = json.loads(sd.get("data", "{}"))
                            if jd.get("bizsrc") == "favorites.note":
                                quoted_collection = jd
                        except (json.JSONDecodeError, TypeError):
                            pass
                if quoted_forward:
                    log.info("QUOTED_FORWARD: id=%s", quoted_forward)
                    qf = await self.req("get_forward_msg", {"id": quoted_forward}, timeout=15)
                    if qf and qf.get("status") == "ok":
                        nodes = qf.get("data", {}).get("messages", [])
                        if nodes:
                            qf_imgs = []
                            qf_text = await self._parse_forward_nodes(nodes, qf_imgs)
                            if qf_imgs:
                                qf_cached = await self._cache_fwd_images(qf_imgs)
                                _all_cached_paths.extend(qf_cached)
                            text = (
                                f"[引用合并转发]\n{qf_text}\n"
                                f"(转发中包含{len(qf_imgs)}张图片，已缓存到图床)\n"
                                f"[用户回复] {text}"
                            )
                    else:
                        text = "[引用合并转发(展开失败)] " + text
                elif quoted_collection:
                    log.info("QUOTED_COLLECTION: jumpUrl=%s",
                             quoted_collection.get("meta", {}).get("news", {}).get("jumpUrl", ""))
                    coll_imgs = await self._extract_collection_imgs(quoted_collection)
                    if coll_imgs:
                        _all_cached_paths.extend(coll_imgs)
                        text = f"[引用收藏]\n(收藏中包含{len(coll_imgs)}张图片，已缓存)\n[用户回复] {text}"
                    else:
                        text = "[引用收藏(展开失败)] " + text
                else:
                    text = "[引用消息] " + text

        text = text.strip()
        log.info("PARSE_RESULT: reply=%s at_bot=%s text_len=%d text_start=%s",
                 reply, at_bot, len(text), text[:80] if text else "(empty)")

        # ── 插件事件：消息解析完成 ──────────────────────
        parsed_event = ParsedMessageEvent(
            group_id=gid, user_id=uid, text=text,
            img_urls=img_urls, at_bot=at_bot, reply_to=rid,
        )
        pr = await self._bus.emit("message_parsed", parsed_event, cancellable=True)
        if pr.consumed:
            log.info("PLUGIN_CONSUMED: gid=%s uid=%s msg=message_parsed", gid, uid)
            return

        # 涩图直发
        stripped = text.strip()
        if stripped in ("色图", "涩图", "ɫͼ", "ɬͼ"):
            log.info("SE_TU_DIRECT: gid=%s", gid)
            reply = True

        # 色图列表
        if not reply and stripped == "色图列表":
            log.info("SE_TU_LIST: gid=%s", gid)
            reply = True

        # 色图规则
        if not reply and stripped in ("色图规则", "涩图规则"):
            log.info("SE_TU_RULES: gid=%s", gid)
            reply = True

        # 直接喊文件夹名
        if not reply:
            matched_src = self._match_direct_src(stripped)
            if matched_src:
                log.info("DIRECT_SRC_NAME: gid=%s text=%s matched=%s", gid, stripped, matched_src)
                reply = True

        if not reply:
            for kw in (getattr(self, '_trigger_kw', TRIGGER_KW)):
                if kw in text:
                    reply = True
                    log.info("TRIGGERED_BY_KEYWORD: kw=%s", kw)
                    break

        # 管理员 / 命令直接触发（无需关键词前缀）
        if not reply and text.startswith("/") and str(uid) in (getattr(self, '_admin_uids', ADMIN_UIDS)):
            reply = True
            log.info("TRIGGERED_BY_ADMIN_CMD: gid=%s cmd=%s", gid, text.split()[0])

        # /生图 触发
        if not reply:
            if re.search(r"/生图", stripped):
                reply = True
                log.info("TRIGGERED_BY_SHENG_TU: gid=%s text=%s", gid, stripped[:50])

        # GIF 触发（在进入队列前检查，让消息能到达 _process_queue_item 的 GIF 处理逻辑）
        if not reply:
            gif_lower = stripped.lower()
            if gif_lower == "gif 列表" or gif_lower.startswith("gif "):
                reply = True
                log.info("TRIGGERED_BY_GIF: gid=%s text=%s", gid, stripped[:50])

        if text.startswith("/") and reply:
            log.info("CMD_PASSTHROUGH: gid=%s cmd=%s", gid, text.split()[0])

        self._cache_msg(gid, uid, text or "[图片]")

        if not reply or not text:
            return

        log.info("[%s] Trigger: %s", gid, text[:60])

        # 权限检查
        if not self._is_admin(uid):
            for kw in (getattr(self, '_forbidden_ops', FORBIDDEN_OPS)):
                if kw in text:
                    log.info("PERMISSION_BLOCK: uid=%s text=%s matched_kw=%s", uid, text[:60], kw)
                    await self._enqueue_send("send_group_msg", {
                        "group_id": int(gid),
                        "message": [
                            {"type": "at", "data": {"qq": int(uid)}},
                            {"type": "text", "data": {"text": " 你没有权限执行这个操作哦"}},
                        ],
                    })
                    return

        # 如果文字含存图/收图关键词，把当前消息直接发的图片也缓存到 _all_cached_paths
        # （供 _handle_save_img 使用；其他意图时 finally 块会自动清理缓存）
        # 注：用全局 import re，不要在方法内 import（会遮蔽全局 re）
        if img_urls and not _all_cached_paths:
            save_pattern = re.compile(
                r"(?<!缓)存到|存图|保存|存起来|存一下|收图|收藏|收了|全存|全部存|都存了")
            if save_pattern.search(text):
                log.info("CACHE_DIRECT_IMG: gid=%s img_count=%d save_keyword=yes",
                         gid, len(img_urls))
                direct_cached = await self._cache_fwd_images(img_urls)
                _all_cached_paths.extend(direct_cached)

        # Build ref_context
        ref_context = RefContext()
        ref_context.cached_paths = _all_cached_paths
        ref_context.cached_count = len(_all_cached_paths)
        if rid:
            if "[引用合并转发]" in text:
                ref_context.type = "forward"
                ref_context.has_forward = True
                m = re.search(r"(\d+)张图片", text)
                if m:
                    ref_context.forward_img_count = int(m.group(1))
            elif "[引用消息]" in text:
                if _all_cached_paths or (img_urls and not text.startswith("[引用合并转发")):
                    ref_context.type = "image"
                    ref_context.has_images = True
                else:
                    ref_context.type = "text"
        if "【合并转发】" in text:
            ref_context.type = "forward"
            ref_context.has_forward = True
            m = re.search(r"(\d+)张图片", text)
            if m:
                ref_context.forward_img_count = int(m.group(1))
        if not ref_context.type and img_urls:
            ref_context.type = "image"
            ref_context.has_images = True

        # 放入队列
        if gid not in self.group_queues:
            self.group_queues[gid] = asyncio.Queue()

        await self.group_queues[gid].put({
            "gid": gid, "uid": uid, "text": text,
            "at_bot": at_bot, "img_urls": img_urls,
            "ref_context": ref_context,
        })

        if gid not in self.group_workers or self.group_workers[gid].done():
            self.group_workers[gid] = asyncio.create_task(self._group_worker(gid))
            log.info("QUEUE_WORKER_START: gid=%s", gid)

    # ─── 队列 Worker ───────────────────────────────────

    async def _group_worker(self, gid):
        while True:
            try:
                item = await asyncio.wait_for(self.group_queues[gid].get(), timeout=300)
            except asyncio.TimeoutError:
                if gid in self.group_queues and self.group_queues[gid].empty():
                    del self.group_queues[gid]
                self.group_workers.pop(gid, None)
                log.info("QUEUE_WORKER_END: gid=%s (timeout)", gid)
                return

            try:
                await self._process_queue_item(item)
            except Exception as e:
                log.error("QUEUE_PROCESS_ERR: gid=%s err=%s", gid, str(e)[:200])

            if self.group_queues[gid].empty():
                self.group_workers.pop(gid, None)
                log.info("QUEUE_WORKER_END: gid=%s (drained)", gid)
                return

    async def _process_queue_item(self, item):
        gid = item["gid"]
        uid = item["uid"]
        text = item["text"]
        at_bot = item["at_bot"]
        img_urls = item.get("img_urls", [])
        ref = item.get("ref_context", RefContext())

        # /生图 不走 / 命令，直接走意图路由
        if text.startswith("/") and not re.search(r"/生图", text):
            await self._handle_cmd(gid, uid, text, at_bot)
            return

        # 色图规则
        stripped = text.strip()
        if stripped in ("色图规则", "涩图规则"):
            log.info("SE_TU_RULES_PROCESS: gid=%s", gid)
            await self._handle_rules(gid, uid)
            return

        # 涩图直发
        stripped = text.strip()
        log.info("SE_TU_CHECK: text=%s repr=%s", stripped[:20], repr(stripped)[:50])
        if stripped in ("色图", "涩图", "ɫͼ", "ɬͼ"):
            now = time.time()
            last = self.se_tu_cooldown.get(gid, 0)
            if now - last >= 10:
                self.se_tu_cooldown[gid] = now
                log.info("SE_TU_TRIGGER: gid=%s", gid)
                await self._se_tu_send(gid, uid)
            return

        # 色图列表
        if stripped == "色图列表":
            lines = ["📁 可用文件夹："]
            for name, aliases in self._get_available_srcs():
                depth = name.count("/")
                if depth == 0:
                    lines.append(f"  {name}")
                else:
                    lines.append(f"    {name}")  # 带父路径的子文件夹
                if aliases:
                    lines[-1] += f"（{'、'.join(aliases)}）"
            lines.append("")
            lines.append("直接输入文件夹名即可发图（仅支持第一层文件夹）")
            log.info("SE_TU_LIST_REPLY: gid=%s srcs=%d", gid, len(lines) - 2)
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "text", "data": {"text": "\n".join(lines)}},
            ]})
            return

        # GIF 触发
        gif_lower = stripped.lower()
        if gif_lower == "gif 列表":
            gif_dir = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片", "GIF")
            folders = []
            try:
                for d in sorted(os.listdir(gif_dir)):
                    if os.path.isdir(os.path.join(gif_dir, d)):
                        count = len([f for f in os.listdir(os.path.join(gif_dir, d))
                                     if f.lower().endswith(".gif")])
                        folders.append(f"  {d} ({count} 个GIF)")
            except OSError:
                pass
            if not folders:
                reply = "📁 还没有 GIF 分类"
            else:
                reply = "📁 GIF 分类列表：\n" + "\n".join(folders) + "\n\n发送 gif <分类名> 发3张GIF"
            log.info("GIF_LIST_REPLY: gid=%s", gid)
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "text", "data": {"text": reply}},
            ]})
            return

        if gif_lower.startswith("gif "):
            gif_src = stripped[4:].strip()
            if not gif_src:
                await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                    {"type": "at", "data": {"qq": int(uid)}},
                    {"type": "text", "data": {"text": " 用法: gif <分类名>  如: gif zmd"}},
                ]})
                return
            now = time.time()
            last = self.se_tu_cooldown.get(gid, 0)
            if now - last >= 10:
                self.se_tu_cooldown[gid] = now
                log.info("GIF_TRIGGER: gid=%s src=%s", gid, gif_src)
                await self._se_tu_send_gif(gid, uid, gif_src)
            return

        # 直接喊文件夹名发图
        matched_src = self._match_direct_src(stripped)
        if matched_src:
            now = time.time()
            last = self.se_tu_cooldown.get(gid, 0)
            if now - last >= 10:
                self.se_tu_cooldown[gid] = now
                log.info("DIRECT_SRC_TRIGGER: gid=%s src=%s", gid, matched_src)
                await self._se_tu_send(gid, uid, matched_src)
            return

        # 意图路由
        route = await self._route_intent(
            text, has_image=bool(img_urls),
            has_ref_image=ref.has_images,
            has_ref_forward=ref.has_forward)
        intent = route["intent"]
        log.info("ROUTE: gid=%s intent=%s conf=%s params=%s",
                 gid, intent, route.get("confidence"), route.get("params"))

        # ── 插件事件：意图路由完成 ──────────────────────
        intent_event = IntentEvent(
            group_id=gid, user_id=uid, text=text,
            intent=intent, confidence=route.get("confidence", ""),
            params=route.get("params", {}),
        )
        ir = await self._bus.emit("intent_resolved", intent_event, cancellable=True)
        if ir.consumed:
            log.info("PLUGIN_CONSUMED: gid=%s uid=%s msg=intent_resolved", gid, uid)
            return
        # 插件可能修改了意图
        intent = intent_event.intent
        route["params"] = intent_event.params

        try:
            if intent == "send_img":
                await self._handle_send_img(gid, uid, text, route["params"], ref, at_bot)
            elif intent == "vision":
                await self._handle_vision(gid, uid, text, img_urls, ref, at_bot)
            elif intent == "save_img":
                await self._handle_save_img(gid, uid, text, ref, at_bot)
            elif intent == "gen_img":
                await self._handle_gen_img(gid, uid, text, img_urls, ref, at_bot)
            else:
                await self._handle_chat(gid, uid, text, at_bot)
        except Exception as e:
            log.error("HANDLER_ERR: gid=%s intent=%s err=%s", gid, intent, str(e)[:200])
        finally:
            # 清理 tc/1/ 缓存
            if intent != "save_img" and ref.cached_paths:
                for p in ref.cached_paths:
                    try:
                        if os.path.isfile(p):
                            os.remove(p)
                    except Exception:
                        pass
                log.info("CACHE_CLEANUP: removed %d files", len(ref.cached_paths))

    # ─── 涩图直发 ──────────────────────────────────────

    @staticmethod
    def _match_direct_src(text):
        """精确匹配：只匹配第一层文件夹名或 SRC_MAP 别名（不匹配子文件夹，不含 GIF）"""
        if not text:
            return None
        norm = lambda s: unicodedata.normalize("NFKC", s)
        n_text = norm(text)

        # 1) SRC_MAP 别名匹配
        for folder, keywords in IntentRouter.SRC_MAP.items():
            if text in keywords or n_text in [norm(k) for k in keywords]:
                return folder

        # 2) 第一层文件夹精确匹配（不含 GIF）
        fwd_dir = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片")
        try:
            for d in sorted(os.listdir(fwd_dir)):
                full = os.path.join(fwd_dir, d)
                if os.path.isdir(full) and d != "GIF" and n_text == norm(d):
                    return d
        except OSError:
            pass
        return None

    @staticmethod
    def _get_available_srcs():
        """递归扫描所有文件夹（含子文件夹），返回 (显示路径, 别名列表)。排除 GIF 及 GIF/ 下内容。"""
        srcs = []
        # 1) SRC_MAP
        for folder, keywords in IntentRouter.SRC_MAP.items():
            aliases = [kw for kw in keywords if kw != folder]
            srcs.append((folder, aliases))
        # 2) 递归扫描转发图片/ 下所有子文件夹（排除 GIF）
        fwd_dir = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片")
        seen = set(IntentRouter.SRC_MAP.keys())
        try:
            for root, dirs, _ in os.walk(fwd_dir):
                for d in sorted(dirs):
                    sub = os.path.join(root, d)
                    rel = os.path.relpath(sub, fwd_dir).replace("\\", "/")
                    # 跳过顶层的 GIF 目录本身（GIF/zmd 等子文件夹会展示）
                    if rel == "GIF":
                        continue
                    if rel in seen:
                        continue
                    seen.add(rel)
                    srcs.append((rel, []))
        except OSError:
            pass
        return srcs

    async def _se_tu_send(self, gid, uid, src=None):
        """
        发送图片到群。
        src=None → 全文件夹盲抽；src=<文件夹名> → 指定文件夹选图
        """
        if not await self._check_daily_img_limit(gid, needed=1):
            log.info("SE_TU_SKIP: gid=%s daily limit reached", gid)
            return
        import shutil
        tc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tc", "1")
        os.makedirs(tc_dir, exist_ok=True)
        pick_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tc", "pick_fwd_image.py")
        batch_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tc", "send_images_batch.py")

        cmd = [sys.executable, pick_script, "10", "--mode", self._pick_mode, "--skip", str(self._pick_skip)]
        if src:
            cmd += ["--src", src]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            paths = [p.strip() for p in stdout.decode("utf-8").split("\n") if p.strip()]
        except Exception as e:
            log.error("SE_TU_PICK_ERR: %s", str(e)[:80])
            return

        if not paths:
            log.warning("SE_TU: no images")
            return

        log.info("SE_TU_PICKED: gid=%s src=%s mode=%s skip=%d count=%d paths=[%s]",
                 gid, src, self._pick_mode, self._pick_skip, len(paths),
                 "|".join(os.path.basename(p) for p in paths[:6]))
        BATCH_SIZE = 6
        for i in range(0, len(paths), BATCH_SIZE):
            batch = paths[i:i + BATCH_SIZE]
            urls = []
            for img_path in batch:
                ext = os.path.splitext(img_path)[1].lower()
                fname = f"{uuid.uuid4().hex}{ext}"
                dst = os.path.join(tc_dir, fname)
                shutil.copy2(img_path, dst)
                urls.append(f"http://127.0.0.1:{IMG_PORT}/1/{fname}")
            url_str = ",".join(urls)
            cmd_send = [sys.executable, batch_script, "--group", str(gid), "--urls", url_str]

            async def _send_batch(c=cmd_send, b_len=len(batch)):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *c, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
                    if stdout:
                        log.info("SE_TU_SEND_RESULT: gid=%s batch=%d out=%s",
                                 gid, b_len, stdout.decode("utf-8", errors="replace")[:200])
                    if stderr:
                        log.warning("SE_TU_SEND_STDERR: gid=%s batch=%d err=%s",
                                    gid, b_len, stderr.decode("utf-8", errors="replace")[:200])
                    log.info("SE_TU_SEND_OK: gid=%s batch=%d", gid, b_len)
                except Exception as e2:
                    log.error("SE_TU_SEND_ERR: %s", str(e2)[:80])

            await self.send_queue.put(_send_batch())

        self._incr_daily_img_usage(gid, len(paths))
        log.info("DAILY_IMG_USAGE: gid=%s today=%d", gid, self._get_daily_img_used(gid))

    # ─── GIF 发送 ──────────────────────────────────────

    async def _se_tu_send_gif(self, gid, uid, src):
        """
        从 GIF/<src>/ 发 3 个 GIF。
        与 _se_tu_send 一致的流水线：pick_fwd_image.py 选图 → 复制到 tc/1/ → 一次 WS 批量发送。
        GIF 已预先优化，直接发送无需压缩。
        """
        GALLERY = 3
        if not await self._check_daily_img_limit(gid, needed=GALLERY):
            log.info("GIF_SKIP: gid=%s daily limit reached", gid)
            return

        tc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tc", "1")
        os.makedirs(tc_dir, exist_ok=True)
        pick_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tc", "pick_fwd_image.py")
        batch_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tc", "send_images_batch.py")

        # 1) 选图 — 与发图共用 pick_fwd_image.py
        cmd = [sys.executable, pick_script, str(GALLERY), "--mode", self._pick_mode, "--skip", str(self._pick_skip),
               "--gif", "--src", f"GIF/{src}"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            paths = [p.strip() for p in stdout.decode("utf-8").split("\n") if p.strip()]
        except Exception as e:
            log.error("GIF_PICK_ERR: gid=%s src=%s err=%s", gid, src, str(e)[:80])
            return

        if not paths:
            log.warning("GIF: no gifs picked for gid=%s src=%s", gid, src)
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "at", "data": {"qq": int(uid)}},
                {"type": "text", "data": {"text": f" 「{src}」里还没有 GIF 了"}},
            ]})
            return

        log.info("GIF_PICKED: gid=%s src=%s mode=%s skip=%d count=%d paths=[%s]",
                 gid, src, self._pick_mode, self._pick_skip, len(paths),
                 "|".join(os.path.basename(p) for p in paths[:GALLERY]))

        # 2) 复制到临时目录，准备 URL
        urls = []
        for img_path in paths:
            ext = os.path.splitext(img_path)[1].lower()
            fname = f"{uuid.uuid4().hex}{ext}"
            dst = os.path.join(tc_dir, fname)
            try:
                shutil.copy2(img_path, dst)
            except OSError as e:
                log.warning("GIF_COPY_ERR: %s", str(e)[:80])
                continue
            urls.append(f"http://127.0.0.1:{IMG_PORT}/1/{fname}")

        if not urls:
            log.warning("GIF: all copies failed for gid=%s src=%s", gid, src)
            return

        # 3) 批量发送 — 一次 WS 连接发所有 GIF（与发图一致）
        url_str = ",".join(urls)
        cmd_send = [sys.executable, batch_script, "--group", str(gid), "--urls", url_str]

        async def _send_batch(c=cmd_send, total=len(urls)):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *c, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                if stdout:
                    log.info("GIF_SEND_RESULT: gid=%s out=%s",
                             gid, stdout.decode("utf-8", errors="replace")[:200])
                if stderr:
                    log.warning("GIF_SEND_STDERR: gid=%s err=%s",
                                gid, stderr.decode("utf-8", errors="replace")[:200])
                if proc.returncode != 0:
                    log.error("GIF_SEND_FAIL: gid=%s retcode=%d", gid, proc.returncode)
                    return
                log.info("GIF_SEND_OK: gid=%s src=%s sent=%d/%d", gid, src, total, GALLERY)
                self._incr_daily_img_usage(gid, total)
            except Exception as e2:
                log.error("GIF_SEND_ERR: gid=%s err=%s", gid, str(e2)[:80])

        await self.send_queue.put(_send_batch())

    # ─── 色图规则说明（转发消息包裹） ─────────────────

    async def _handle_rules(self, gid, uid):
        """处理"色图规则" - 返回当前存图发图逻辑说明，包裹在转发消息中防止刷屏"""
        nodes = []

        # ── Node 1: 发图规则 ──
        send_lines = []
        send_lines.append(f"📤 发图规则")
        send_lines.append("")
        send_lines.append(f"选图模式：{PICK_MODE}")
        send_lines.append("")
        send_lines.append("📁 可用文件夹：")

        for name, aliases in self._get_available_srcs():
            depth = name.count("/")
            prefix = "  " * depth
            line = f"{prefix}• {name}"
            if aliases:
                line += f"（{'/'.join(aliases)}）"
            send_lines.append(line)

        send_lines.append("")
        send_lines.append("触发方式：")
        send_lines.append("• 发送「色图」/「涩图」→ 全文件夹盲抽10张")
        send_lines.append("• 发送「色图列表」→ 查看可用文件夹")
        send_lines.append("• 直接发送文件夹名（如：萝莉、脚）→ 指定文件夹发图")
        send_lines.append("• 发送「gif <分类>」→ 随机发3张GIF")

        nodes.append({
            "type": "node",
            "data": {
                "name": "灰风",
                "uin": self.self_id or "0",
                "content": [{"type": "text", "data": {"text": "\n".join(send_lines)}}],
            }
        })

        # ── Node 2: 存图规则 ──
        save_lines = []
        save_lines.append(f"📥 存图规则")
        save_lines.append("")
        save_lines.append("触发方式：")
        save_lines.append("• 发送「存到<分类>」→ 保存到指定分类文件夹")
        save_lines.append("• 发送「收图」/「存图」/「收了」→ 保存到「其他」文件夹")
        save_lines.append("• 发送「全存」/「都存了」→ 全部保存")
        save_lines.append("")
        save_lines.append("保存路径：桌面/转发图片/<分类>/")
        save_lines.append("")
        save_lines.append("自动分类：")
        save_lines.append("• 静图 → 直接存到分类文件夹")
        save_lines.append("• GIF → 自动分到 GIF/<分类>/")
        save_lines.append("• MP4 → 自动分到 视频/<分类>/")
        save_lines.append("")
        save_lines.append("自动压缩：超过500KB的图片自动压缩（GIF除外）")

        nodes.append({
            "type": "node",
            "data": {
                "name": "灰风",
                "uin": self.self_id or "0",
                "content": [{"type": "text", "data": {"text": "\n".join(save_lines)}}],
            }
        })

        log.info("SE_TU_RULES_SEND: gid=%s nodes=%d", gid, len(nodes))
        # 用 req 而不是 _enqueue_send，因为 forward_msg 不属于限速队列要等一条条发
        # 但为了避免混乱，仍然通过 send_queue 发送
        await self.send_queue.put(
            self.req("send_group_forward_msg", {
                "group_id": int(gid),
                "messages": nodes,
            })
        )

    # ─── 热加载配置 ────────────────────────────────────

    CONFIG_RELOAD_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       ".reload_config.flag")

    def _reload_config(self):
        """重新读取 config.yaml，使改动即时生效。"""
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            log.warning("RELOAD_CFG_ERR: %s", e)
            return

        old_se_tu = set(self._se_tu_tasks.keys())

        # 更新自身缓存
        self._group_configs = cfg.get("groups", {})
        self._auto_clear_groups = set(cfg.get("auto_clear_groups", []))
        self._trigger_kw = cfg.get("trigger_keywords", [])
        self._admin_uids = cfg.get("admin_uids", [])
        self._forbidden_ops = cfg.get("forbidden_ops", [])
        self._gen_img_cfg = cfg.get("gen_img", {})
        self._pick_mode = cfg.get("pick_mode", "random")
        self._pick_skip = cfg.get("pick_skip", 0)

        # 处理 auto_se_tu 定时器变化
        new_se_tu = {gid for gid, gc in self._group_configs.items() if gc.get("auto_se_tu")}

        # 停止已关闭的
        for gid in old_se_tu - new_se_tu:
            if gid in self._se_tu_tasks:
                self._se_tu_tasks[gid].cancel()
                del self._se_tu_tasks[gid]
                log.info("RELOAD_CFG: stopped auto_se_tu for gid=%s", gid)

        # 启动新增的
        for gid in new_se_tu - old_se_tu:
            if gid not in self._se_tu_tasks:
                task = asyncio.create_task(self._se_tu_scheduler(gid))
                self._se_tu_tasks[gid] = task
                log.info("RELOAD_CFG: started auto_se_tu for gid=%s", gid)

        log.info("RELOAD_CFG: done groups=%d se_tu=%d", len(self._group_configs), len(self._se_tu_tasks))

    def _check_reload_flag(self):
        """检查是否有重载配置的标记文件。"""
        try:
            if os.path.isfile(self.CONFIG_RELOAD_FLAG):
                self._reload_config()
                os.remove(self.CONFIG_RELOAD_FLAG)
        except Exception:
            pass

    # ─── 定时色图 ──────────────────────────────────────

    def _start_se_tu_schedulers(self):
        """为启用 auto_se_tu 的群启动定时器"""
        for gid in AUTO_SE_TU_GROUPS:
            if gid in self._se_tu_tasks:
                continue
            task = asyncio.create_task(self._se_tu_scheduler(gid))
            self._se_tu_tasks[gid] = task
            log.info("AUTO_SE_TU_START: gid=%s interval=1800s", gid)

    async def _se_tu_scheduler(self, gid):
        """每 30 分钟自动发一次色图"""
        # 首次延迟5分钟启动，给桥接充分初始化时间
        await asyncio.sleep(300)
        while not self._shutdown:
            self._check_reload_flag()
            # 每次循环检查该群是否仍开启 auto_se_tu
            gcfg = getattr(self, '_group_configs', GROUP_PROMPTS).get(str(gid), {})
            if not gcfg.get("auto_se_tu"):
                log.info("AUTO_SE_TU_STOP: gid=%s disabled via config", gid)
                break
            try:
                log.info("AUTO_SE_TU: gid=%s", gid)
                await self._se_tu_send(gid, "0")
            except Exception as e:
                log.error("AUTO_SE_TU_ERR: gid=%s %s", gid, str(e)[:100])
            await asyncio.sleep(1800)

    # ─── 意图路由 ──────────────────────────────────────

    async def _route_intent(self, text, has_image=False, has_ref_image=False, has_ref_forward=False):
        """关键词意图路由，低置信度直接走 chat，不再调用 SF 兜底。"""
        result = IntentRouter.keyword_route(text, has_image, has_ref_image, has_ref_forward)
        # gen_img 保持关键词直接返回
        if result["intent"] == "gen_img":
            return result
        # 低置信度兜底为 chat（正常对话）
        if result["confidence"] == "low":
            return {"intent": "chat", "confidence": "fallback", "params": {}}
        return result

    # ─── 意图处理器 ────────────────────────────────────

    def _build_system_prompt(self, gid, intent, extra=""):
        prompts = getattr(self, '_group_configs', GROUP_PROMPTS)
        base = prompts.get(gid, {}).get("prompt", DEFAULT_PROMPT)
        intent_labels = {
            "chat": "【当前意图】普通聊天\n像在群里跟群友聊天一样，自然简短，30字以内。",
            "vision": (
                "【当前意图】识图\n用户发了一张图片，AI视觉模型已经分析了图片内容（见下方描述）。"
                "根据描述回复用户的问题或点评图片。不要说自己看不到图。30字以内。"),
            "save_img": (
                "【当前意图】存图\n用户想要保存图片到本地。图片已就绪，你确认存图即可。"
                "可以问存到哪个文件夹，答【存好啦】。30字以内。"),
            "send_img": (
                "【当前意图】发图\n用户想让你发图。桥接已经自动选图发图了，你只需回复一句配图语。"
                "30字以内。不想说话可回复 (no_reply)。"),
            "gen_img": "【当前意图】生图\n用户想让桥接画图，桥接会处理。你正常聊天回复即可。",
        }
        return f"当前QQ群号: {gid}\n\n{base}\n\n{intent_labels.get(intent, '')}\n{extra}"

    async def _call_and_send(self, gid, uid, system_prompt, user_text, at_bot):
        ctx = self._build_context(gid, user_text)
        reply = await self._call_sf_chat(system_prompt, ctx, user_text)
        if not reply or reply.strip() == "(no_reply)":
            return

        segments = self._split_reply(reply)
        for i, seg in enumerate(segments):
            segs = []
            if i == 0 and at_bot:
                segs.append({"type": "at", "data": {"qq": int(uid)}})
            segs.append({"type": "text", "data": {"text": seg}})
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": segs})

    @staticmethod
    def _split_reply(text):
        """将回复按句切段，逐条发送提升活人感。最后一段为剩余全部内容。"""
        import re
        if len(text) < 15:
            return [text]
        # 按句子结束符 + 换行切分
        parts = re.split(r'(?<=[。！？.!?\n])\s*', text)
        parts = [p.strip() for p in parts if p.strip()]
        if not parts:
            return [text]
        # 合并过短片段
        merged = []
        for p in parts:
            if merged and len(p) < 3:
                merged[-1] += p
            else:
                merged.append(p)
        # 前4条逐条发，第5条开始合并为最后一条
        if len(merged) <= 5:
            return merged
        return merged[:4] + ["".join(merged[4:])]

    async def _handle_chat(self, gid, uid, text, at_bot):
        sp = self._build_system_prompt(gid, "chat")
        user_text = f"[QQ:{uid}] {text}"
        await self._call_and_send(gid, uid, sp, user_text, at_bot)

    async def _handle_vision(self, gid, uid, text, img_urls, ref, at_bot):
        if ref.cached_paths:
            target_urls = [
                f"http://127.0.0.1:{IMG_PORT}/1/{os.path.basename(p)}"
                for p in ref.cached_paths if os.path.isfile(p)
            ]
        else:
            target_urls = img_urls

        if not target_urls:
            await self._handle_chat(gid, uid, text, at_bot)
            return

        log.info("VISION: %d images", len(target_urls))
        await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
            {"type": "at", "data": {"qq": int(uid)}},
            {"type": "text", "data": {"text": " 让我看看..."}},
        ]})

        user_question = re.sub(
            r'\[图片\]|\[引用消息\]|\[引用合并转发\]|\[合并转发\]', '', text).strip()

        raw_result = await self._vision_raw(target_urls[0])
        if raw_result:
            log.info("VISION_RAW: %s", raw_result[:80])
            user_text = (
                f"[QQ:{uid}] {user_question}\n\n"
                f"【AI视觉描述】{raw_result}"
            )
        else:
            user_text = f"[QQ:{uid}] 用户发了图但视觉模型无法识别。"

        sp = self._build_system_prompt(gid, "vision")
        await self._call_and_send(gid, uid, sp, user_text, at_bot)

    async def _handle_send_img(self, gid, uid, text, params, ref, at_bot):
        if not await self._check_daily_img_limit(gid, needed=1):
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "at", "data": {"qq": int(uid)}},
                {"type": "text", "data": {"text": " 今天的发图量已经用完了，明天再来吧~"}},
            ]})
            return
        count = params.get("count") or random.randint(4, 10)
        src = params.get("src")
        tc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tc", "1")
        pick_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tc", "pick_fwd_image.py")

        os.makedirs(tc_dir, exist_ok=True)

        if not src:
            pass  # 由 pick_fwd_image.py 扫描所有子文件夹合并选图

        images_to_send = []
        cmd = [sys.executable, pick_script, str(count), "--mode", PICK_MODE]
        if src:
            cmd += ["--src", src]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            cmd_str = " ".join(cmd)
            log.info("SEND_IMG_CMD: %s", cmd_str)
            _log_gen_img(gid, uid, f"send_img:{src}", "cmd", cmd_str)
            if stderr:
                _log_gen_img(gid, uid, f"send_img:{src}", "cmd_stderr",
                            stderr.decode("utf-8", errors="replace")[:200])
            paths = [p.strip() for p in stdout.decode("utf-8").split("\n") if p.strip()]
            for p in paths:
                dst = os.path.join(tc_dir, os.path.basename(p))
                shutil.copy2(p, dst)
                images_to_send.append(dst)
        except Exception as e:
            log.error("PICK_IMG_ERR: %s", str(e)[:100])
            _log_gen_img(gid, uid, f"send_img:{src}", "pick_fail", str(e)[:100])

        if not images_to_send:
            log.warning("SEND_IMG: no images to send")
            _log_gen_img(gid, uid, f"send_img:{src}", "no_images")
            return

        BATCH_SIZE = 6
        batch_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tc", "send_images_batch.py")
        for i in range(0, len(images_to_send), BATCH_SIZE):
            batch = images_to_send[i:i + BATCH_SIZE]
            urls = []
            for img_path in batch:
                ext = os.path.splitext(img_path)[1].lower()
                fname = f"{uuid.uuid4().hex}{ext}"
                dst = os.path.join(tc_dir, fname)
                shutil.copy2(img_path, dst)
                urls.append(f"http://127.0.0.1:{IMG_PORT}/1/{fname}")
            url_str = ",".join(urls)
            cmd_send = [sys.executable, batch_script, "--group", str(gid), "--urls", url_str]

            async def _send_batch(c=cmd_send, u=url_str, b_len=len(batch)):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *c, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
                    if stdout:
                        log.info("SEND_BATCH_RESULT: gid=%s batch=%d out=%s",
                                 gid, b_len, stdout.decode("utf-8", errors="replace")[:200])
                    if stderr:
                        log.warning("SEND_BATCH_STDERR: gid=%s batch=%d err=%s",
                                    gid, b_len, stderr.decode("utf-8", errors="replace")[:200])
                    log.info("SEND_BATCH_OK: gid=%s batch=%d urls=%s", gid, b_len, u)
                    _log_gen_img(gid, uid, f"send_img:{src}", "sent", f"ok={b_len}")
                except Exception as e2:
                    log.error("SEND_BATCH_ERR: %s", str(e2)[:80])
                    _log_gen_img(gid, uid, f"send_img:{src}", "send_fail", str(e2)[:80])

            await self.send_queue.put(_send_batch())

        log.info("SEND_IMG_REPLY: gid=%s uid=%s reply=发了%d张~", gid, uid, len(images_to_send))
        reply = f"发了{len(images_to_send)}张~"
        segs = []
        if at_bot:
            segs.append({"type": "at", "data": {"qq": int(uid)}})
        segs.append({"type": "text", "data": {"text": reply}})
        await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": segs})
        self._incr_daily_img_usage(gid, len(images_to_send))
        log.info("SEND_IMG_DONE: gid=%s images=%d today=%d",
                 gid, len(images_to_send), self._get_daily_img_used(gid))

    async def _handle_save_img(self, gid, uid, text, ref, at_bot):
        fwd_dir = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片")
        gif_base = os.path.join(fwd_dir, "GIF")
        video_base = os.path.join(fwd_dir, "视频")

        save_to = IntentRouter.extract_save_to(text) or "其他"
        target_dir = os.path.join(fwd_dir, save_to)          # 静图 → <分类>/
        gif_target_dir = os.path.join(gif_base, save_to)     # GIF → GIF/<分类>/
        video_target_dir = os.path.join(video_base, save_to) # MP4 → 视频/<分类>/

        # 模糊匹配：提取的文件夹名精确不存在时，尝试匹配已有文件夹前缀
        # 例如 "存到萝莉泡泡" → "萝莉泡泡" → 已有"萝莉" → 匹配到"萝莉"
        if not os.path.isdir(target_dir) and not os.path.isdir(gif_target_dir) and not os.path.isdir(video_target_dir):
            try:
                for d in os.listdir(fwd_dir):
                    if os.path.isdir(os.path.join(fwd_dir, d)) and save_to.startswith(d):
                        log.info("SAVE_IMG_DIR_FUZZY: input=%s matched=%s", save_to, d)
                        save_to = d
                        target_dir = os.path.join(fwd_dir, save_to)
                        gif_target_dir = os.path.join(gif_base, save_to)
                        video_target_dir = os.path.join(video_base, save_to)
                        break
            except OSError:
                pass

        saved = 0
        dedup_skipped = 0
        cached = ref.cached_paths or []

        if not cached:
            log.info("SAVE_IMG: no cached images, gid=%s uid=%s save_to=%s", gid, uid, save_to)
            reply = "没有可存的图片"
            segs = []
            if at_bot:
                segs.append({"type": "at", "data": {"qq": int(uid)}})
            segs.append({"type": "text", "data": {"text": reply}})
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": segs})
            log.info("SAVE_IMG_DONE: gid=%s saved=%d target=%s", gid, saved, save_to)
            return

        os.makedirs(fwd_dir, exist_ok=True)

        from datetime import datetime as _dt
        import imagehash
        from PIL import Image

        # 重新加载去重库内存缓存（config_web 的一键去重可能刚更新了 DB）
        self.dedup.reload()

        for src_path in cached:
            if not os.path.isfile(src_path):
                continue
            ext = os.path.splitext(src_path)[1].lower()

            # ── GIF：不压缩，只读第一帧做 phash 去重 ──
            if ext == ".gif":
                dst_dir = gif_target_dir
                os.makedirs(dst_dir, exist_ok=True)
                try:
                    gif_hash = self.dedup.compute_hash(src_path)
                    if gif_hash is not None:
                        is_dup, _ = self.dedup.is_duplicate_by_hash(gif_hash)
                        if is_dup:
                            dedup_skipped += 1
                            log.info("SAVE_IMG_DEDUP_SKIP: gif hash match, %s", os.path.basename(src_path))
                            continue
                except Exception:
                    pass  # hash 失败也继续存
                mtime = self._src_mtime(src_path)
                ts = _dt.fromtimestamp(mtime).strftime("%Y%m%d_%H%M%S")
                seq = 1
                while True:
                    fname = f"{ts}_{seq:03d}{ext}"
                    dst = os.path.join(dst_dir, fname)
                    if not os.path.exists(dst):
                        break
                    seq += 1
                try:
                    shutil.copy2(src_path, dst)
                    saved += 1
                    if gif_hash is not None:
                        self.dedup.record(dst, gif_hash)
                except Exception as e:
                    log.warning("SAVE_IMG_GIF_ERR: %s", str(e)[:80])
                continue

            # ── MP4：跳过去重，直接存 ──
            if ext == ".mp4":
                dst_dir = video_target_dir
                os.makedirs(dst_dir, exist_ok=True)
                mtime = self._src_mtime(src_path)
                ts = _dt.fromtimestamp(mtime).strftime("%Y%m%d_%H%M%S")
                seq = 1
                while True:
                    fname = f"{ts}_{seq:03d}{ext}"
                    dst = os.path.join(dst_dir, fname)
                    if not os.path.exists(dst):
                        break
                    seq += 1
                try:
                    shutil.copy2(src_path, dst)
                    saved += 1
                except Exception as e:
                    log.warning("SAVE_IMG_MP4_ERR: %s", str(e)[:80])
                continue

            # ── 图片（jpg/png/webp 等）：压缩 → phash → 去重 → 存 ──
            dst_dir = target_dir
            os.makedirs(dst_dir, exist_ok=True)

            # 先捕获原始 mtime（压缩可能替换文件）
            mtime = self._src_mtime(src_path)

            # 压缩：>500KB 归一化为 JPG，≤500KB 原样返回
            compressed = compress_image(src_path)

            # 计算 phash
            img_hash = None
            try:
                pil_img = Image.open(compressed)
                img_hash = imagehash.phash(pil_img)
                pil_img.close()
            except Exception as _he:
                log.info("SAVE_IMG_HASH_ERR: %s %s", os.path.basename(compressed), str(_he)[:60])

            # 去重检查（hash 有效时才查）
            if img_hash is not None:
                is_dup, _ = self.dedup.is_duplicate_by_hash(img_hash)
                if is_dup:
                    dedup_skipped += 1
                    log.info("SAVE_IMG_DEDUP_SKIP: hash match, %s", os.path.basename(src_path))
                    # 清理压缩产生的临时文件
                    if compressed != src_path and os.path.isfile(compressed):
                        try:
                            os.remove(compressed)
                        except Exception:
                            pass
                    continue

            # 用压缩后文件的扩展名
            actual_ext = os.path.splitext(compressed)[1] or ".jpg"
            ts = _dt.fromtimestamp(mtime).strftime("%Y%m%d_%H%M%S")
            seq = 1
            while True:
                fname = f"{ts}_{seq:03d}{actual_ext}"
                dst = os.path.join(dst_dir, fname)
                if not os.path.exists(dst):
                    break
                seq += 1

            try:
                shutil.copy2(compressed, dst)
                saved += 1
                if img_hash is not None:
                    self.dedup.record(dst, img_hash)
            except Exception as e:
                log.warning("SAVE_IMG_ERR: %s", str(e)[:80])

        # ── 嘲讽池 ──
        _mock_pool = [
            "废物老资源一直发有没有意思",
            "翻来覆去就这几张是吧",
            "能不能来点新的 存图的都看吐了",
            "典 又是这张 你硬盘里是不是就这点东西",
            "重复率这么高 你搁这刷屏呢",
            "又来？这图我都见过八回了",
            "有没有新货啊 老哥",
            "你发得不腻我看得都腻了",
            "今日存图 昨日重现",
        ]
        _all_dup_mock = [
            "全是发过的 你是复读机吗",
            "一张新的都没有 走了",
            "建议买个新硬盘 你这存量太丢人了",
            "零新图 零分 下次别叫我了",
        ]
        import random as _r

        if saved > 0:
            log.info("SAVE_IMG: saved %d to %s (dedup_skipped=%d)", saved, save_to, dedup_skipped)
            parts = [f"已存到「{save_to}」{saved}张 ✅"]
            if dedup_skipped > 0:
                parts.append(f"（{dedup_skipped}张重复已跳过）")
                parts.append("💬 " + _r.choice(_mock_pool))
            reply = " ".join(parts)
        else:
            log.info("SAVE_IMG: no images to save, gid=%s uid=%s save_to=%s", gid, uid, save_to)
            reply = "没有可存的图片"
            if dedup_skipped > 0:
                reply = f"全部是重复图片（{dedup_skipped}张已跳过）💬 {_r.choice(_all_dup_mock)}"

        log.info("SAVE_IMG_REPLY: gid=%s uid=%s reply=%s", gid, uid, reply)
        segs = []
        if at_bot:
            segs.append({"type": "at", "data": {"qq": int(uid)}})
        segs.append({"type": "text", "data": {"text": reply}})
        await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": segs})
        log.info("SAVE_IMG_DONE: gid=%s saved=%d target=%s", gid, saved, save_to)

    @staticmethod
    def _src_mtime(src_path):
        """安全获取文件修改时间。"""
        try:
            return os.path.getmtime(src_path)
        except OSError:
            return time.time()

    async def _handle_add_group(self, gid, uid):
        """管理员添加群到白名单"""
        log.info("CMD_ADD: gid=%s uid=%s", gid, uid)
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        try:
            with open(cfg_path, "r", encoding="utf-8") as _f:
                new_cfg = yaml.safe_load(_f) or {}
            groups = new_cfg.setdefault("groups", {})
            if str(gid) in groups:
                await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                    {"type": "text", "data": {"text": "该群已在白名单中 ✅"}},
                ]})
                return
            prompt = "你是一个QQ群聊机器人叫泡泡。在群里像真人一样聊天，语气轻松自然，偶尔开玩笑。回复简短（30字以内）。"
            groups[str(gid)] = {"prompt": prompt}
            with open(cfg_path, "w", encoding="utf-8") as _f:
                yaml.dump(new_cfg, _f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            prompts = getattr(self, '_group_configs', GROUP_PROMPTS)
            prompts[str(gid)] = {"prompt": prompt}
            self._reload_config()
            log.info("CMD_ADD_OK: gid=%s added to whitelist", gid)
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "text", "data": {"text": "已将该群加入白名单 ✅ 开始聊天吧~"}},
            ]})
        except Exception as e:
            log.error("CMD_ADD_ERR: %s", str(e)[:100])
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "text", "data": {"text": f"添加失败: {str(e)[:50]}"}},
            ]})

    async def _handle_cmd(self, gid, uid, text, at_bot):
        cmd = text.strip().split()[0].lower()
        parts = text.strip().split(maxsplit=1)
        cmd_args = parts[1].strip() if len(parts) > 1 else ""

        if gid in self.context:
            del self.context[gid]

        if cmd == "/clear":
            log.info("CMD_CLEAR: gid=%s uid=%s", gid, uid)
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "text", "data": {"text": "已清空上下文 ✅"}},
            ]})
            return

        # /add — 管理员将当前群加入白名单（已白名单的群里的快捷方式）
        if cmd == "/add" and str(uid) in (getattr(self, '_admin_uids', ADMIN_UIDS)):
            await self._handle_add_group(gid, uid)
            return

        # /选图 <模式> — 切换选图模式并热加载
        if cmd == "/选图" and cmd_args:
            mode_map = {
                "纯随机": "shuffle",
                "shuffle": "shuffle",
                "最新优先": "newest",
                "newest": "newest",
                "随机顺序": "random",
                "随机顺序窗": "random",
                "random": "random",
            }
            new_mode = mode_map.get(cmd_args)
            if new_mode:
                cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
                try:
                    import yaml
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg = yaml.safe_load(f) or {}
                    cfg["pick_mode"] = new_mode
                    with open(cfg_path, "w", encoding="utf-8") as f:
                        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False, indent=2, width=120)
                    self._reload_config()
                    log.info("CMD_PICK_MODE: gid=%s uid=%s mode=%s", gid, uid, new_mode)
                    await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                        {"type": "text", "data": {"text": f" 已切换选图模式为「{cmd_args}」"}},
                    ]})
                except Exception as e:
                    log.error("CMD_PICK_MODE_ERR: %s", str(e)[:80])
                    await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                        {"type": "text", "data": {"text": " 切换失败，请重试"}},
                    ]})
            else:
                await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                    {"type": "at", "data": {"qq": int(uid)}},
                    {"type": "text", "data": {"text": " 可用模式: 纯随机 / 最新优先 / 随机顺序"}},
                ]})
            return

        prompts = getattr(self, '_group_configs', GROUP_PROMPTS)
        base = prompts.get(gid, {}).get("prompt", DEFAULT_PROMPT)
        sp = f"当前QQ群号: {gid}\n\n{base}"
        user_text = text
        await self._call_and_send(gid, uid, sp, user_text, at_bot)

    # ─── 每日发图限额 ─────────────────────────────────

    def _get_daily_img_limit(self, gid):
        gcfg = (getattr(self, '_group_configs', GROUP_PROMPTS)).get(str(gid), {})
        return gcfg.get("daily_img_limit", 0)

    def _get_daily_img_used(self, gid):
        today = datetime.now().strftime("%Y-%m-%d")
        return self.daily_img_usage.get(f"{today}|{gid}", 0)

    def _incr_daily_img_usage(self, gid, count=1):
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"{today}|{gid}"
        self.daily_img_usage[key] = self.daily_img_usage.get(key, 0) + count
        self._save_daily_img_usage()

    def _save_daily_img_usage(self):
        """持久化到 JSON 文件，供 config_web 前端读取。"""
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".daily_img_usage.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.daily_img_usage, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    async def _check_daily_img_limit(self, gid, needed=1):
        limit = self._get_daily_img_limit(gid)
        if limit <= 0:
            return True
        used = self._get_daily_img_used(gid)
        ok = (used + needed) <= limit
        if not ok:
            log.info("DAILY_IMG_LIMIT: gid=%s used=%d/%d", gid, used, limit)
        return ok

    # ─── 生图 ──────────────────────────────────────────

    async def _check_gen_img_limit(self, gid, uid):
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"{today}|{gid}|{uid}"
        return self.gen_img_usage.get(key, 0) < GEN_IMG_DAILY_LIMIT

    def _incr_gen_img_usage(self, gid, uid):
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"{today}|{gid}|{uid}"
        self.gen_img_usage[key] = self.gen_img_usage.get(key, 0) + 1
        used = self.gen_img_usage[key]
        log.info("GEN_IMG_USAGE: gid=%s uid=%s today=%s used=%d/%d",
                 gid, uid, today, used, GEN_IMG_DAILY_LIMIT)

    async def _handle_gen_img(self, gid, uid, text, img_urls=None, ref=None, at_bot=False):
        if not await self._check_gen_img_limit(gid, uid):
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "at", "data": {"qq": int(uid)}},
                {"type": "text", "data": {"text": " 今天的生图次数用完了，明天再来吧~"}},
            ]})
            return

        # 先回复"正在画…"
        await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
            {"type": "at", "data": {"qq": int(uid)}},
            {"type": "text", "data": {"text": " 正在画……\U0001f3a8"}},
        ]})

        # 去掉 st 前缀，去掉 [图片] 占位符
        prompt = re.sub(r"^st\b\s*", "", text, flags=re.IGNORECASE).strip()
        prompt = re.sub(r"\s*\[图片\]\s*", " ", prompt).strip()
        prompt = re.sub(r"\s*\[图片/视频\]\s*", " ", prompt).strip()

        # ── 收集参考图：消息自带图片 + 引用/转发中的图片 ──
        os.makedirs(IMG_STORAGE, exist_ok=True)
        ref_img_urls = []

        # 1) 当前消息自带的图片（NapCat URL → 下载到本地，用文件头识别格式）
        for url in (img_urls or []):
            try:
                import urllib.request
                tmp = os.path.join(IMG_STORAGE, f"tmp_{uuid.uuid4().hex}")
                urllib.request.urlretrieve(url, tmp)
                ext = self._detect_image_ext(tmp)
                fname = f"ref_{uuid.uuid4().hex}{ext}"
                dst = os.path.join(IMG_STORAGE, fname)
                os.rename(tmp, dst)
                ref_img_urls.append(dst)  # 本地路径
                log.info("GEN_IMG_REF_DL: url=%s dst=%s", url[:50], fname)
            except Exception as e:
                log.warning("GEN_IMG_DL_ERR: %s", str(e)[:80])

        # 2) 引用/转发中缓存的图片（本地文件）
        if ref and ref.cached_paths:
            for path in ref.cached_paths:
                if os.path.isfile(path):
                    ext = os.path.splitext(path)[1].lower() or ".jpg"
                    fname = f"ref_{uuid.uuid4().hex}{ext}"
                    dst = os.path.join(IMG_STORAGE, fname)
                    shutil.copy2(path, dst)
                    ref_img_urls.append(dst)  # 本地路径

        # 最多 10 张参考图
        ref_img_urls = ref_img_urls[:10]

        if not prompt and not ref_img_urls:
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "at", "data": {"qq": int(uid)}},
                {"type": "text", "data": {"text": " 说清楚想画什么呀，或者发张参考图~"}},
            ]})
            return

        log.info("GEN_IMG_PROMPT: gid=%s uid=%s prompt=%s ref_imgs=%d",
                 gid, uid, prompt[:200], len(ref_img_urls))
        _log_gen_img(gid, uid, prompt, "prompt",
                     f"raw={text[:60]} ref_imgs={len(ref_img_urls)} model=gpt-image-2")

        # 读取生图配置（品质/分辨率）
        prompts = getattr(self, '_group_configs', GROUP_PROMPTS)
        gen_cfg = getattr(self, '_gen_img_cfg', None)
        if gen_cfg is None:
            import yaml as _y
            try:
                _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
                with open(_p, encoding="utf-8") as _f:
                    gen_cfg = _y.safe_load(_f).get("gen_img", {})
                self._gen_img_cfg = gen_cfg
            except Exception:
                gen_cfg = {}
        gsize = gen_cfg.get("size") or None
        gquality = gen_cfg.get("quality") or None
        result = await gen_image(prompt, image_paths=ref_img_urls if ref_img_urls else None,
                                     size=gsize, quality=gquality)
        if not result.ok:
            status = result.status or 502
            msg = result.error[:80] if result.error else f"HTTP {status}"
            _log_gen_img(gid, uid, prompt, "api_fail",
                         f"status={status} error={msg}")
            from gen_img import STATUS_MSGS
            hint = STATUS_MSGS.get(status, f"未知错误 ({status})")
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "at", "data": {"qq": int(uid)}},
                {"type": "text", "data": {"text": f" 生图失败 ({status}): {hint}"}},
            ]})
            return
        image_data = result.data
        _log_gen_img(gid, uid, prompt, "api_ok",
                     f"model=gpt-image-2 size=auto quality=auto resp_size={len(image_data)}bytes")

        os.makedirs(GEN_IMG_DIR, exist_ok=True)
        os.makedirs(IMG_STORAGE, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        local_filename = f"gen_{ts}_{uid}.png"
        local_path = os.path.join(GEN_IMG_DIR, local_filename)
        with open(local_path, "wb") as f:
            f.write(image_data)
        log.info("GEN_IMG_SAVED: %s (%d bytes)", local_path, len(image_data))

        tc_filename = f"gen_{ts}_{uid}_{uuid.uuid4().hex}.png"
        tc_path = os.path.join(IMG_STORAGE, tc_filename)
        shutil.copy2(local_path, tc_path)
        img_url = f"{IMG_URL}/1/{tc_filename}"

        send_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tc", "send_image.py")
        cmd_send = [sys.executable, send_script, "--group", str(gid), "--url", img_url]
        cmd_str = " ".join(cmd_send)
        log.info("GEN_IMG_CMD: %s", cmd_str)
        _log_gen_img(gid, uid, prompt, "cmd",
                     f"model=gpt-image-2 size=auto quality=auto {cmd_str}")

        async def _send_img(c=cmd_send, fn=tc_filename):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *c, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(proc.wait(), timeout=15)
                log.info("GEN_IMG_SEND_OK: gid=%s", gid)
                _log_gen_img(gid, uid, prompt, "send_ok", f"img={fn}")
            except Exception as e:
                log.error("GEN_IMG_SEND_ERR: %s", str(e)[:80])
                _log_gen_img(gid, uid, prompt, "send_fail", str(e)[:80])

        await self.send_queue.put(_send_img())

        self._incr_gen_img_usage(gid, uid)
        await self._handle_chat(gid, uid, text, at_bot)

    async def close(self):
        self._shutdown = True
        # 关闭插件
        await self._bus.emit("bot_shutdown", {})
        if hasattr(self, '_hot_reload_task'):
            self._hot_reload_task.cancel()
        await self.plugin_manager.unload_all()
        if self._send_worker_task:
            self._send_worker_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()
        await self._sf_client.aclose()
        self.img.cleanup()

    async def _send_worker(self):
        """消费发送队列，每条消息间隔 3 秒"""
        while not self._shutdown:
            try:
                coro = await self.send_queue.get()
                await coro
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("SEND_WORKER_ERR: %s", e)

    async def _enqueue_send(self, action, params):
        """通过限速队列发送群消息（3秒间隔）"""
        await self.send_queue.put(self.send(action, params))
