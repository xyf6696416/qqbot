# GreyWind x NapCat 桥接项目总结

# GreyWind x NapCat 桥接项目总结 (v6.3)

桥接脚本：`bridge.py`（版本 v6.3 — 模块化拆分 + 健壮性改进）

---

## 架构

```
QQ群 → NapCat WebSocket (18888) → bridge.py/bot.py → SiliconFlow AI → 回复
                                           ↓
                                  intent_router.py 意图路由
                                           ↓
                                  (发图/存图/识图/生图/聊天)
                                           ↓
                                  NapCat → QQ群

管理面板 ← config_web.py (7788) ← 浏览器
后台服务 ← image_server.py (7777) ← 图床
         ← image_compressor.py     ← 定时压缩
         ← image_dedup.py          ← 图片去重
```

**模块结构（v6.3）：**

```
napcat-greywind/
├── bridge.py            # 入口（进程管理 + main）
├── bot.py               # Bot 核心（连接/消息解析/队列/handler）
├── config.py            # 配置加载（从 config.yaml 读取）
├── config.yaml          # 群聊配置（群号/prompt/trigger/生图等）
├── config_web.py        # Web 配置管理面板（Flask, 端口 7788）
├── intent_router.py     # 意图路由 + 参数提取（关键词 + SF 兜底）
├── image_utils.py       # 图片压缩 + 图床管理（ImageServer）
├── image_compressor.py  # 后台图片压缩调度器（定时扫描 >500KB 图片）
├── image_dedup.py       # 图片去重模块（基于 phash，SQLite 存储）
├── gen_img.py           # AI 生图 (12AI GPT-Image-2)
├── tc/                  # 图床相关
│   ├── image_server.py  #   图片 HTTP 服务（端口 7777）
│   ├── send_image.py    #   发单张图到群（NapCat HTTP API）
│   ├── send_images_batch.py  # 批量发图
│   ├── pick_fwd_image.py     # 选图脚本
│   └── 1/               #   临时图片缓存目录
├── templates/
│   └── config_editor.html  # 配置管理面板前端
├── docs/                # 需求/设计文档
├── tests/               # 测试脚本
├── start_bridge.bat     # 启动桥接（新窗口）
├── start_config_web.bat # 启动配置面板（新窗口）
└── README.md
```

---

## 服务总览

所有服务必须同时运行才能正常工作。启动顺序：**NapCat → 图床 → 桥接 → 配置面板**

| # | 服务 | 文件/程序 | 端口 | 必需 | 自动启动 |
|---|------|-----------|------|------|---------|
| 1 | **NapCat QQ** | `NapCatWinBootMain.exe` | 18888 (WS) | ✅ | 手动 |
| 2 | **图床** | `tc/image_server.py` | 7777 | ✅ | bridge.py 自动启动 |
| 3 | **主桥接** | `bridge.py` | - | ✅ | 手动 (`start_bridge.bat`) |
| 4 | **配置面板** | `config_web.py` | 7788 | ❌ | 手动 (`start_config_web.bat`) |
| 5 | **图片压缩器** | `image_compressor.py` | 后台 | ❌ | config_web.py 自动启动 |

### 一键启动

```powershell
cd C:\Users\Administrator\Desktop\napcat-greywind

# 启动桥接（新窗口，自动启动图床、杀掉旧进程）
start_bridge.bat

# 启动配置管理面板（新窗口，自动启动图片压缩调度器）
start_config_web.bat
```

> **注意**：`bridge.py` 内含 `kill_old_bridges()`，启动时自动杀死旧 bridge 进程。\
> 如果从命令行直接 `python bridge.py` 运行，可能会被 kill 波及，推荐用 `start_bridge.bat` 或在独立终端运行。

### 访问地址

| 服务 | 地址 |
|------|------|
| 配置管理面板 | http://localhost:7788 |
| 图片服务器 | http://localhost:7777 |

---

## 核心功能

### 1. 消息收发
- 通过 NapCat WebSocket (`ws://127.0.0.1:18888`) 接收 QQ 群消息
- 调用 **SiliconFlow** API 获取 AI 回复（`Qwen/Qwen2.5-14B-Instruct` 聊天，`Qwen/Qwen3-VL-8B-Instruct` 识图）
- 群号白名单：`788327119`、`1026442086`、`1037926595`、`1047550014`

### 2. 意图路由（`intent_router.py`）
- `IntentRouter.keyword_route()` 按优先级匹配：save_img > send_img > gen_img > vision > chat
- 低置信度走 SiliconFlow Qwen2.5-7B 兜底分类
- SRC_MAP 映射发图关键词到 `桌面\转发图片\` 子文件夹
- 不指定 src 时选图脚本自动从所有文件夹选图

### 3. 发图流水线
- 流程：`pick_fwd_image.py 选图 → 复制到 tc/1/ → send_images_batch.py 批量发送`
- 选图模式：`newest`（按时间最新优先，可在 config.yaml 配置）
- 图片来源：`桌面\转发图片\`（静图/GIF/视频）
- 一批最多 6 张，批间隔消息队列 3 秒

### 4. 存图流水线
- 自动压缩 >500KB 图片（PNG/JPG/WebP 统转 JPG，阶梯降 quality）
- 自动去重（phash 相似度 ≤5，基于 SQLite 全局库）
- GIF → `桌面\转发图片\GIF\<分类>\`（不压缩）
- MP4 → `桌面\转发图片\视频\<分类>\`
- 静图 → `桌面\转发图片\<分类>\`

### 5. 识图流水线（用户发图时）
1. **SiliconFlow** `Qwen/Qwen3-VL-8B-Instruct`：原始客观描述
2. **AI 回复**：润色为群聊风格回复

### 6. AI 生图（`gen_img.py`）
- 触发词：`/生图`、`st <prompt>`
- API：12AI `gpt-image-2`
- 支持参考图（消息图片 + 引用转发图片，最多 10 张）
- 日限 99 次/用户，保存到 `~/Desktop/AI生成/`
- 支持 size/quality 配置（config.yaml → gen_img）

### 7. 群聊上下文
- 每群缓存最近 200 条消息（本地 deque）
- `/clear` 清空上下文
- @机器人 或 命中 trigger_keywords → 回复

---

## 配置管理面板

访问 http://localhost:7788 可在线管理：

- **配置编辑**：群聊 prompt、触发词、gen_img 等
- **图片压缩**：查看状态、手动触发扫描、设置调度间隔
- **一键重命名**：按修改时间重命名图片文件
- **图片去重**：全量扫描 → 审核重复组 → 删除/保留
- **每日用量**：查看各群发图量
- **服务状态**：NapCat/图床/桥接运行状态一览

---

## 进程关系

```
NapCatWinBootMain.exe          ← QQ 框架启动器
    └─ QQ.exe                  ← QQ NT 主进程（端口 18888 WS）

bridge.py                      ← 桥接入口（杀掉旧实例后启动）
    ├─ bot.py                  ← Bot 核心（WS 连接/消息路由）
    ├─ image_server.py         ← 图床 HTTP 服务（7777）
    │   └─ tc/1/               ← 临时图片目录
    ├─ send_images_batch.py    ← 批量发图到群
    ├─ send_image.py           ← 单张发图到群
    └─ pick_fwd_image.py       ← 选图脚本

config_web.py                  ← 配置管理面板（Flask, 7788）
    ├─ image_compressor.py     ← 后台定时压缩调度器
    └─ image_dedup.py          ← 图片去重（phash + SQLite）

frpc.exe                       ← frp 客户端（外网穿透）
mihomo.exe                     ← 代理（HTTP 7890）
```

---

## 群聊配置

### 白名单
- 只在 `config.yaml` → `groups` 里配置的群生效
- 每个群可独立配置 prompt
- 管理员发 `/add` 可在线添加当前群到白名单

### 权限系统
- `admin_uids`：`653020384`（超级管理员）
- `forbidden_ops`：非管理员禁止的操作（删/删除/重置/清空记录等）

### 生图日限
- `config.yaml` → `gen_img.daily_limit`（默认 99）

---

## 运维指南

### 启动全部服务

```powershell
# 1. 确保 NapCat 已启动（桌面 NapCat 快捷方式）

# 2. 启动桥接（新窗口，包含图床）
cd C:\Users\Administrator\Desktop\napcat-greywind
.\start_bridge.bat

# 3. 启动配置面板（新窗口，包含压缩调度器）
.\start_config_web.bat
```

### 重启桥接
```powershell
# bridge.py 启动时会自动 kill 旧进程
cd C:\Users\Administrator\Desktop\napcat-greywind
python bridge.py
```
或在配置面板点击「重启桥接」按钮。

### 加群
1. 配置面板 → YAML 编辑 或 直接编辑 `config.yaml`
2. `groups` 加一行 `群号: { prompt: "..." }`
3. 保存后自动热加载（写 `.reload_config.flag`），无需重启

### 检查运行
```powershell
# 查看所有 Python 进程
tasklist | findstr python

# 查看端口
netstat -ano | findstr 18888    # NapCat WS
netstat -ano | findstr 7777     # 图床
netstat -ano | findstr 7788     # 配置面板
```

### 查看日志
- 桥接日志：控制台 stdout（或 `bridge_test.log`）
- 生图日志：`~/Desktop/AI生成/gen_img.log`
- 配置面板日志：控制台 stdout

---

## 修改记录

### 2026-06-05 (v6.3) — 模块化拆分 + 健壮性
- **模块拆分**：bridge.py 拆分为 config / intent_router / image_utils / gen_img / bot
- **Bug 修复**：close 方法缩进错误（之前不在 Bot 类内）、重复 @staticmethod、控制台 UTF-8 编码
- **httpx 连接复用**：OC 和 SF 各自复用 AsyncClient，减少 TCP 握手
- **OC 5xx 重试**：遇到 500 错误自动重试 1 次
- **图片压缩异步化**：compress_image 用 asyncio.to_thread 包装，不再阻塞事件循环
- **tc/1/ 自动清理**：启动时删除 >24h 的临时文件
- **优雅关闭**：run 循环检查 _shutdown 标志，close 时关闭所有客户端
- **冗余清理**：删除 tc/gen_img_and_send.py（已合并到 gen_img.py），测试文件移入 tests/

### 2026-06-04 (v6.2)
- **session key 改为群级**：`v3_group_{gid}`（之前 `v3_group_{gid}_user_{uid}` 各人独立）
- **system prompt 加群号**：开头 `当前QQ群号: {gid}`，修复发图发错群
- **选图脚本修正**：改用 `pick_fwd_image.py`（搜 `桌面\转发图片\`）
- **缺省 src**：不指定 src 时随机选一个子文件夹
- **生图 prompt 逻辑**：默认去掉触发词直接当 prompt
- **auto_clear 调整**：`1037926595` 移出 auto_clear 名单

### 2026-06-03 (v6.1)
- 引用的单图路由、图片压缩、识图修复、auto-clear 修复、/ 命令触发修复
- 意图路由（IntentRouter 类）、提示词拆分、处理器拆分
- SF 兜底、SSH 修复

### 2026-06-02
- 转发图片仅 @时保存、嵌套转发检测、文件名格式修改
