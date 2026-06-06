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
import time
import unicodedata
import uuid
from collections import deque
from datetime import datetime

import httpx
from aiohttp import ClientSession, WSMsgType

from config import (
    NAPCAT_WS, OC_API, OC_TOKEN, AGENT_ID, OC_ENABLED,
    TRIGGER_KW, ADMIN_UIDS, FORBIDDEN_OPS,
    GROUP_PROMPTS, DEFAULT_PROMPT, AUTO_CLEAR_GROUPS, AUTO_SE_TU_GROUPS,
    SF_KEY, SF_MODEL, SF_CHAT_MODEL, IMG_PORT, IMG_URL, IMG_STORAGE,
    MAX_CONTEXT, MAX_IMAGE_SIZE, GEN_IMG_DIR, GEN_IMG_DAILY_LIMIT,
)
from intent_router import IntentRouter, RefContext
from image_utils import compress_image, ImageServer
from gen_img import gen_image, _log_gen_img, _get_gen_img_key

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
        self._oc_client = httpx.AsyncClient(timeout=180, proxy=None)
        self._sf_client = httpx.AsyncClient(timeout=60)
        # 优雅关闭标志
        self._shutdown = False
        # 启动时清理 tc/1/ 旧文件（>24h）
        self._cleanup_tc_files()
        # 发送队列（每条消息间隔 3 秒，防封）
        self.send_queue = asyncio.Queue()
        self._send_worker_task = None

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
        return str(uid) in ADMIN_UIDS

    def _has_forbidden_op(self, text):
        for kw in FORBIDDEN_OPS:
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
                        fwd_img_urls.append(url_m.group(1))
            elif isinstance(raw_content, list):
                for seg in raw_content:
                    st2 = seg.get("type", "")
                    sd2 = seg.get("data", {})
                    if st2 == "text":
                        msg_parts.append(sd2.get("text", ""))
                    elif st2 == "image":
                        msg_parts.append("[图片]")
                        u2 = sd2.get("url", "")
                        if u2 and fwd_img_urls is not None:
                            fwd_img_urls.append(u2)
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

    def _cache_fwd_images(self, urls):
        if not urls:
            return []
        cached = []
        img_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tc", "1")
        os.makedirs(img_dir, exist_ok=True)
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        for idx, url in enumerate(urls, 1):
            try:
                import urllib.request
                ext = ".jpg"
                for e in [".png", ".gif", ".webp", ".bmp", ".jpeg"]:
                    if e in url.lower():
                        ext = e
                        break
                fname = f"fwd_{now}_{idx}{ext}"
                dst = os.path.join(img_dir, fname)
                urllib.request.urlretrieve(url, dst)
                cached.append(dst)
            except Exception as e:
                log.warning("CACHE_IMG_ERR: url=%s err=%s", url[:50], str(e)[:100])
        return cached

    def _extract_imgs(self, segs):
        urls = []
        for s in segs:
            if s.get("type") == "image":
                u = s.get("data", {}).get("url", "")
                if u:
                    urls.append(u)
        return urls

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

    # ─── OpenClaw / SiliconFlow 调用 ────────────────────

    async def _call_oc(self, system_prompt, context_str, user_text, session_key, retry=True):
        msgs = [{"role": "system", "content": system_prompt}]
        if context_str:
            msgs.append({"role": "system", "content": f"【近期群消息上下文】\n{context_str}"})
        tagged = f"[qq_bridge] {user_text}" if not user_text.startswith("[qq_bridge]") else user_text
        msgs.append({"role": "user", "content": tagged})
        payload = {
            "model": f"openclaw/{AGENT_ID}",
            "messages": msgs,
            "user": session_key,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OC_TOKEN}",
        }
        try:
            r = await self._oc_client.post(OC_API, json=payload, headers=headers)
            if r.status_code >= 500 and retry:
                log.warning("OC 5xx retry: status=%d", r.status_code)
                await asyncio.sleep(2)
                r = await self._oc_client.post(OC_API, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.error("OC err: %s", str(e)[:300])
            return None

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
        if str(gid) not in GROUP_PROMPTS:
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
                                cached = self._cache_fwd_images(fwd_imgs)
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
                    q_img_cached = self._cache_fwd_images(q_imgs)
                    _all_cached_paths.extend(q_img_cached)
                quoted_forward = None
                for seg in q_msg:
                    if seg.get("type") == "forward":
                        quoted_forward = seg.get("data", {}).get("id", "")
                        break
                if quoted_forward:
                    log.info("QUOTED_FORWARD: id=%s", quoted_forward)
                    qf = await self.req("get_forward_msg", {"id": quoted_forward}, timeout=15)
                    if qf and qf.get("status") == "ok":
                        nodes = qf.get("data", {}).get("messages", [])
                        if nodes:
                            qf_imgs = []
                            qf_text = await self._parse_forward_nodes(nodes, qf_imgs)
                            if qf_imgs:
                                qf_cached = self._cache_fwd_images(qf_imgs)
                                _all_cached_paths.extend(qf_cached)
                            text = (
                                f"[引用合并转发]\n{qf_text}\n"
                                f"(转发中包含{len(qf_imgs)}张图片，已缓存到图床)\n"
                                f"[用户回复] {text}"
                            )
                    else:
                        text = "[引用合并转发(展开失败)] " + text
                else:
                    text = "[引用消息] " + text

        text = text.strip()
        log.info("PARSE_RESULT: reply=%s at_bot=%s text_len=%d text_start=%s",
                 reply, at_bot, len(text), text[:80] if text else "(empty)")

        # 涩图直发
        stripped = text.strip()
        if stripped in ("色图", "涩图", "ɫͼ", "ɬͼ"):
            log.info("SE_TU_DIRECT: gid=%s", gid)
            reply = True

        # 色图列表
        if not reply and stripped == "色图列表":
            log.info("SE_TU_LIST: gid=%s", gid)
            reply = True

        # 直接喊文件夹名
        if not reply:
            matched_src = self._match_direct_src(stripped)
            if matched_src:
                log.info("DIRECT_SRC_NAME: gid=%s text=%s matched=%s", gid, stripped, matched_src)
                reply = True

        if not reply:
            for kw in TRIGGER_KW:
                if kw in text:
                    reply = True
                    log.info("TRIGGERED_BY_KEYWORD: kw=%s", kw)
                    break

        # st 生图触发（行首 st + 非英文字母，兼容中文无空格）
        if not reply:
            if re.search(r"^st(?![a-zA-Z])", stripped, re.IGNORECASE):
                reply = True
                log.info("TRIGGERED_BY_ST: gid=%s text=%s", gid, stripped[:50])

        if text.startswith("/") and reply:
            log.info("CMD_PASSTHROUGH: gid=%s cmd=%s", gid, text.split()[0])

        self._cache_msg(gid, uid, text or "[图片]")

        if not reply or not text:
            return

        log.info("[%s] Trigger: %s", gid, text[:60])

        # 权限检查
        if not self._is_admin(uid):
            for kw in FORBIDDEN_OPS:
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

        # / 命令
        if text.startswith("/"):
            await self._handle_cmd(gid, uid, text, at_bot)
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
            lines = ["📁 可用色图列表："]
            for name, aliases in self._get_available_srcs():
                if aliases:
                    lines.append(f"  {name}（{'、'.join(aliases)}）")
                else:
                    lines.append(f"  {name}")
            log.info("SE_TU_LIST_REPLY: gid=%s srcs=%d", gid, len(lines) - 1)
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "text", "data": {"text": "\n".join(lines)}},
            ]})
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

            # Auto-clear
            if gid in AUTO_CLEAR_GROUPS:
                self._clear_oc_session(gid, uid)

    # ─── 涩图直发 ──────────────────────────────────────

    @staticmethod
    def _match_direct_src(text):
        """精确匹配：文本是否直接命中转发图片子文件夹名或 SRC_MAP 别名"""
        if not text:
            return None
        # NFKC 归一化：处理 "穂"(U+7A42) vs "穗"(U+7A57) 等异体字差异
        norm = lambda s: unicodedata.normalize("NFKC", s)
        n_text = norm(text)
        # 1) SRC_MAP 别名匹配
        for folder, keywords in IntentRouter.SRC_MAP.items():
            if text in keywords or n_text in [norm(k) for k in keywords]:
                return folder
        # 2) 实际文件夹名精确匹配（NFKC 归一化后比较）
        fwd_dir = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片")
        try:
            for d in os.listdir(fwd_dir):
                if os.path.isdir(os.path.join(fwd_dir, d)) and n_text == norm(d):
                    return d
        except OSError:
            pass
        return None

    @staticmethod
    def _get_available_srcs():
        """返回所有可直接喊的文件夹名及别名，用于色图列表展示"""
        srcs = []
        # 1) SRC_MAP
        for folder, keywords in IntentRouter.SRC_MAP.items():
            aliases = [kw for kw in keywords if kw != folder]
            srcs.append((folder, aliases))
        # 2) 实际文件夹（不在 SRC_MAP 中的）
        fwd_dir = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片")
        try:
            existing = set(IntentRouter.SRC_MAP.keys())
            for d in sorted(os.listdir(fwd_dir)):
                if os.path.isdir(os.path.join(fwd_dir, d)) and d not in existing:
                    srcs.append((d, []))
        except OSError:
            pass
        return srcs

    async def _se_tu_send(self, gid, uid, src=None):
        """
        发送图片到群。
        src=None → 全文件夹盲抽；src=<文件夹名> → 指定文件夹选图
        """
        import shutil
        tc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tc", "1")
        os.makedirs(tc_dir, exist_ok=True)
        pick_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tc", "pick_fwd_image.py")
        batch_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tc", "send_images_batch.py")

        cmd = [sys.executable, pick_script, "10"]
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

        BATCH_SIZE = 6
        for i in range(0, len(paths), BATCH_SIZE):
            batch = paths[i:i + BATCH_SIZE]
            urls = []
            for img_path in batch:
                compressed = await asyncio.to_thread(compress_image, img_path)
                ext = os.path.splitext(compressed)[1].lower()
                fname = f"{uuid.uuid4().hex}{ext}"
                dst = os.path.join(tc_dir, fname)
                shutil.copy2(compressed, dst)
                urls.append(f"http://127.0.0.1:{IMG_PORT}/1/{fname}")
            url_str = ",".join(urls)
            cmd_send = [sys.executable, batch_script, "--group", str(gid), "--urls", url_str]

            async def _send_batch(c=cmd_send, b_len=len(batch)):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *c, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL)
                    await asyncio.wait_for(proc.wait(), timeout=15)
                    log.info("SE_TU_SEND_OK: gid=%s batch=%d", gid, b_len)
                except Exception as e2:
                    log.error("SE_TU_SEND_ERR: %s", str(e2)[:80])

            await self.send_queue.put(_send_batch())

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
            try:
                log.info("AUTO_SE_TU: gid=%s", gid)
                await self._se_tu_send(gid, "0")
            except Exception as e:
                log.error("AUTO_SE_TU_ERR: gid=%s %s", gid, str(e)[:100])
            await asyncio.sleep(1800)

    # ─── 意图路由 ──────────────────────────────────────

    async def _route_intent(self, text, has_image=False, has_ref_image=False, has_ref_forward=False):
        result = IntentRouter.keyword_route(text, has_image, has_ref_image, has_ref_forward)

        if result["intent"] == "gen_img":
            sf_intent = await self._sf_route(text, has_image, has_ref_image, has_ref_forward)
            if sf_intent == "chat":
                return {"intent": "chat", "confidence": "sf", "params": {}}
            return {"intent": "gen_img", "confidence": "sf", "params": result["params"]}

        if result["confidence"] == "high":
            return result

        if result["confidence"] == "low":
            sf_intent = await self._sf_route(text, has_image, has_ref_image, has_ref_forward)
            if sf_intent:
                return {"intent": sf_intent, "confidence": "sf", "params": result["params"]}

        return result

    async def _sf_route(self, text, has_image, has_ref_image, has_ref_forward):
        prompt = (
            "你是QQ群聊消息的意图分类器。"
            "只输出一个词： vision save_img send_img gen_img chat\n"
            f"消息属性：含图片={has_image} 引用图片={has_ref_image} 引用转发={has_ref_forward}"
        )
        try:
            r = await self._sf_client.post(
                "https://api.siliconflow.cn/v1/chat/completions",
                json={
                    "model": "Qwen/Qwen2.5-7B-Instruct",
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": text},
                    ],
                    "max_tokens": 5,
                    "temperature": 0.1,
                }, headers={"Authorization": f"Bearer {SF_KEY}"})
            result = r.json()["choices"][0]["message"]["content"].strip()
            if result in ("vision", "save_img", "send_img", "gen_img", "chat"):
                log.info("SF_ROUTE: %s -> %s", text[:30], result)
                return result
        except Exception as e:
            log.warning("SF_ROUTE_ERR: %s", str(e)[:100])
        return None

    # ─── 意图处理器 ────────────────────────────────────

    def _build_system_prompt(self, gid, intent, extra=""):
        base = GROUP_PROMPTS.get(gid, {}).get("prompt", DEFAULT_PROMPT)
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
        if OC_ENABLED:
            session_key = f"v3_group_{gid}"
            reply = await self._call_oc(system_prompt, ctx, user_text, session_key)
        else:
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
        count = params.get("count") or random.randint(4, 10)
        src = params.get("src")
        tc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tc", "1")
        pick_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tc", "pick_fwd_image.py")

        os.makedirs(tc_dir, exist_ok=True)

        if not src:
            pass  # 由 pick_fwd_image.py 扫描所有子文件夹合并选图

        images_to_send = []
        cmd = [sys.executable, pick_script, str(count)]
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
                        *c, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    await asyncio.wait_for(proc.communicate(), timeout=15)
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
        log.info("SEND_IMG_DONE: gid=%s images=%d", gid, len(images_to_send))

    async def _handle_save_img(self, gid, uid, text, ref, at_bot):
        fwd_dir = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片")
        tc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tc", "1")
        os.makedirs(fwd_dir, exist_ok=True)

        save_to = IntentRouter.extract_save_to(text) or "其他"
        target_dir = os.path.join(fwd_dir, save_to)
        os.makedirs(target_dir, exist_ok=True)

        saved = 0
        cached = ref.cached_paths or []
        for src_path in cached:
            if not os.path.isfile(src_path):
                continue
            compressed = await asyncio.to_thread(compress_image, src_path)
            dst = os.path.join(target_dir, os.path.basename(compressed))
            try:
                shutil.copy2(compressed, dst)
                saved += 1
            except Exception as e:
                log.warning("SAVE_IMG_ERR: %s", str(e)[:80])

        if saved > 0:
            log.info("SAVE_IMG: saved %d to %s", saved, target_dir)
            reply = f"已存到「{save_to}」{saved}张 ✅"
        else:
            log.info("SAVE_IMG: no images to save, gid=%s uid=%s save_to=%s", gid, uid, save_to)
            reply = "没有可存的图片"

        log.info("SAVE_IMG_REPLY: gid=%s uid=%s reply=%s", gid, uid, reply)
        segs = []
        if at_bot:
            segs.append({"type": "at", "data": {"qq": int(uid)}})
        segs.append({"type": "text", "data": {"text": reply}})
        await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": segs})
        log.info("SAVE_IMG_DONE: gid=%s saved=%d target=%s", gid, saved, save_to)

    async def _handle_cmd(self, gid, uid, text, at_bot):
        cmd = text.strip().split()[0].lower()

        if gid in self.context:
            del self.context[gid]
        self._clear_oc_session(gid, uid)

        if cmd == "/clear":
            log.info("CMD_CLEAR: gid=%s uid=%s", gid, uid)
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "text", "data": {"text": "已清空上下文 ✅"}},
            ]})
            return

        base = GROUP_PROMPTS.get(gid, {}).get("prompt", DEFAULT_PROMPT)
        sp = f"当前QQ群号: {gid}\n\n{base}"
        user_text = text
        await self._call_and_send(gid, uid, sp, user_text, at_bot)

    def _clear_oc_session(self, gid, uid=None):
        sk = f"agent:paopao:openai-user:v3_group_{gid}"
        ss = os.path.join(
            os.path.expanduser("~"), ".openclaw", "agents", "paopao", "sessions", "sessions.json")
        try:
            with open(ss, "r", encoding="utf-8") as f:
                store = json.load(f)
            entry = store.get(sk)
            if entry:
                sf = entry.get("sessionFile", "")
                if sf and os.path.isfile(sf):
                    os.remove(sf)
                tf = sf.replace(".jsonl", ".trajectory.jsonl") if sf else ""
                if tf and os.path.isfile(tf):
                    os.remove(tf)
                del store[sk]
                with open(ss, "w", encoding="utf-8") as f:
                    json.dump(store, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("CLEAR_SESSION_ERR: %s", str(e)[:100])

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

        # 1) 当前消息自带的图片（NapCat URL → 下载到本地）
        for url in (img_urls or []):
            try:
                ext = ".jpg"
                for e in [".png", ".gif", ".webp", ".bmp", ".jpeg"]:
                    if e in url.lower():
                        ext = e
                        break
                fname = f"ref_{uuid.uuid4().hex}{ext}"
                dst = os.path.join(IMG_STORAGE, fname)
                import urllib.request
                urllib.request.urlretrieve(url, dst)
                ref_img_urls.append(dst)  # 本地绝对路径
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
                    ref_img_urls.append(dst)  # 本地绝对路径

        # 最多 3 张参考图
        ref_img_urls = ref_img_urls[:3]

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

        image_data = await gen_image(prompt, image_urls=ref_img_urls if ref_img_urls else None)
        if not image_data:
            _log_gen_img(gid, uid, prompt, "api_fail",
                         "model=gpt-image-2 size=auto quality=auto")
            await self._enqueue_send("send_group_msg", {"group_id": int(gid), "message": [
                {"type": "at", "data": {"qq": int(uid)}},
                {"type": "text", "data": {"text": " 画图失败了，可能是 API 出问题了\U0001f605"}},
            ]})
            return
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
        if self._send_worker_task:
            self._send_worker_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()
        await self._oc_client.aclose()
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
