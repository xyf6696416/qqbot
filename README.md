# GreyWind x NapCat 桥接项目总结

桥接脚本：`bridge.py`（版本 v6.3 — 模块化拆分 + 健壮性改进）
运行方式：`python bridge.py`

---

## 架构

```
QQ群 → NapCat WebSocket (18888) → bridge.py → OpenClaw API → AI回复
                                           ↓
                                  bot.py 处理返回（发图/存图/识图/生图）
                                           ↓
                                  NapCat → QQ群
```

**模块结构（v6.3）：**

```
napcat-greywind/
├── bridge.py          # 入口（进程管理 + main）
├── config.py          # 配置加载
├── intent_router.py   # 意图路由 + 参数提取
├── image_utils.py     # 图片压缩 + 图床管理
├── gen_img.py         # AI 生图 (12AI GPT-Image-2)
├── bot.py             # Bot 核心（连接/消息解析/队列/handler）
├── config.yaml        # 群聊配置
├── tc/                # 图床相关
│   ├── image_server.py
│   ├── send_image.py
│   └── send_images_batch.py
├── tests/             # 测试脚本
└── README.md
```

- **NapCat**：`ncp/` 目录
- **图床**：`tc/1/`（HTTP 7777）
- **选图脚本**：`tc/pick_fwd_image.py`

---

## 核心功能

### 1. 消息收发
- 通过 NapCat WebSocket（127.0.0.1:18888）接收 QQ 群消息
- 转发到 OpenClaw API（127.0.0.1:18789）获取 AI 回复
- 群号白名单：在 `config.yaml` 中配置

### 2. 意图路由（`intent_router.py`）
- `IntentRouter.keyword_route()` 按优先级匹配：save_img > send_img > gen_img > vision > chat
- 低置信度走 SiliconFlow Qwen2.5-7B 兜底分类
- SRC_MAP 映射发图关键词到 `~/Desktop/转发图片/` 子文件夹
- 不指定 src 时随机选一个子文件夹

### 3. 发图流水线
- 流程：`pick_fwd_image.py 选图 → 复制到 tc/1/ → send_images_batch.py 批量发送`
- 选图脚本：`tc/pick_fwd_image.py`（带去重状态 `.pick_fwd_state.json`）
- 图片来源：`~/Desktop/转发图片/`
- 一批最多 6 张，批次间隔 5s

### 4. 存图流水线
- 自动压缩 >500KB 图片（PNG→JPG）
- 复制到 `~/Desktop/转发图片/<分类>/`
- 不指定分类默认存到「其他」

### 5. 识图流水线（用户发图时）
1. **SiliconFlow** `Qwen/Qwen3-VL-8B-Instruct`：原始客观描述（100 字内）
2. **OpenClaw**：润色为群聊风格回复

### 6. AI 生图（`gen_img.py`）
- 触发词：生成/画一个/画只/画条/帮我画/AI画/画出来
- 默认去掉触发词直接作为 prompt，用户说「润色/扩写」才调 agent
- API：12AI `gpt-image-2`（密钥从 `~/Desktop/key.txt` 懒加载）
- 日限 99 次/用户，保存到 `~/Desktop/AI生成/`
- 生图直发群

### 7. 群聊上下文
- 每群缓存最近 200 条消息（本地 deque）
- **OC session key**：`v3_group_{gid}`（按群共享）
- `/` 开头命令透传给 OpenClaw，不加 `[QQ:uid]` 前缀

### 8. 群聊触发器
- @机器人 → 回复
- 命中 `trigger_keywords`（在 `config.yaml` 中配置）→ 回复

---

## 群聊配置

### 白名单
- 只在 `config.yaml` → `groups` 里配置的群生效
- 每个群可独立配置 prompt

### auto-clear 机制
- 在 `config.yaml` → `auto_clear_groups` 中配置
- 每次回复后自动删 OC session（不保留连续对话）

### 权限系统
- `admin_uids`：在 `config.yaml` 中配置（超级管理员）
- `forbidden_ops`：非管理员禁止的操作（删/删除/重置/清空记录等）

### 生图日限
- `config.yaml` → `gen_img.daily_limit`（默认 99）

---

## 进程关系

```
NapCatWinBootMain.exe          ← 启动器
    └─ QQ.exe                  ← QQ NT 主进程（端口 18888 WS）
              
bridge.py                      ← 桥接入口
    ├─ bot.py                  ← Bot 核心
    ├─ image_server.py         ← 图床（HTTP 7777）
    └─ send_images_batch.py    ← 批量发图

frpc.exe                       ← frp 客户端（外网穿透）
mihomo.exe                     ← 代理（HTTP 7890）
```

---

## 运维指南

### 重启桥接
```powershell
cd <项目目录>
# bridge.py 启动时会自动 kill 旧进程
python bridge.py
```

### 加群
1. `config.yaml` → `groups` 加一行群号 + prompt
2. 如需 auto-clear → 加到 `auto_clear_groups`
3. 重启桥接

### 检查运行
```powershell
Get-Process python
netstat -ano | Select-String "18888.*ESTAB"
```

---

## 修改记录

### 2026-06-05 (v6.3) — 模块化拆分 + 健壮性
- **模块拆分**：bridge.py 拆分为 config / intent_router / image_utils / gen_img / bot
- **Bug 修复**：close 方法缩进错误、重复 @staticmethod、控制台 UTF-8 编码
- **httpx 连接复用**：OC 和 SF 各自复用 AsyncClient，减少 TCP 握手
- **OC 5xx 重试**：遇到 500 错误自动重试 1 次
- **图片压缩异步化**：compress_image 用 asyncio.to_thread 包装，不再阻塞事件循环
- **tc/1/ 自动清理**：启动时删除 >24h 的临时文件
- **优雅关闭**：run 循环检查 _shutdown 标志，close 时关闭所有客户端
- **冗余清理**：删除 tc/gen_img_and_send.py（已合并到 gen_img.py），测试文件移入 tests/

### 2026-06-04 (v6.2)
- **session key 改为群级**：`v3_group_{gid}`（之前 `v3_group_{gid}_user_{uid}` 各人独立）
- **system prompt 加群号**：开头 `当前QQ群号: {gid}`，修复发图发错群
- **选图脚本修正**：改用 `pick_fwd_image.py`（搜 `~/Desktop/转发图片/`）
- **缺省 src**：不指定 src 时随机选一个子文件夹
- **生图 prompt 逻辑**：默认去掉触发词直接当 prompt

### 2026-06-03 (v6.1)
- 引用的单图路由、图片压缩、识图修复、auto-clear 修复、/ 命令触发修复
- 意图路由（IntentRouter 类）、提示词拆分、处理器拆分
- SF 兜底、SSH 修复

### 2026-06-02
- 转发图片仅 @时保存、嵌套转发检测、文件名格式修改
