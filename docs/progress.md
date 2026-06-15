# 开发进度记录

> 更新日期：2026-06-15
> 当前版本：v6.4-dev（插件系统阶段二完成）

---

## 版本里程碑

| 版本 | 日期 | 状态 | 说明 |
|------|------|------|------|
| v6.0 | 06-02 | ✅ | 初始桥接可用 |
| v6.1 | 06-03 | ✅ | 意图路由 + 多意图处理器 |
| v6.2 | 06-04 | ✅ | 群级 session + 选图修复 |
| v6.3 | 06-05 | ✅ | 模块化拆分（config/bot/intent_router/image_utils/gen_img） |
| v6.4-dev | 06-15 | 🛠️ | 插件系统（阶段一/二完成） |

---

## 已完成功能

### 基础桥接（v6.3）

- [x] NapCat WebSocket 连接管理
- [x] 多群白名单 + 独立 prompt
- [x] 硅基流动 AI 聊天/识图（Qwen2.5 系列）
- [x] 发图流水线（pick_fwd_image → 缓存 → 批量发送）
- [x] 存图流水线（压缩 → phash 去重 → 分类归档）
- [x] AI 生图（12AI GPT-Image-2，支持参考图）
- [x] 群上下文缓存（200条/deque）
- [x] 发送限速队列（3秒间隔）
- [x] GIF/视频分类处理
- [x] Web 配置面板（Flask, :7788）
- [x] 图片定时压缩调度器
- [x] 图片去重全量扫描（phash + SQLite）
- [x] 热加载配置（`.reload_config.flag`）

### 插件系统 — 阶段一：基础框架（2026-06-15）

- [x] `mod/` 目录结构搭建（constants / event_bus / context / plugin_base / plugin_manager）
- [x] `PluginBase` 基类 + `__init_subclass__` 自动注册
- [x] `EventBus` 事件总线（优先级调度 / cancel / 异常隔离）
- [x] `PluginContext` 依赖注入（发消息 / 读配置 / KV 存储）
- [x] `PluginManager` 生命周期（扫描 → 导入 → 注册 handler → initialize → 运行 → terminate）
- [x] 装饰器 API: `@on_message` / `@on_intent` / `@on_command` / `@on_regex`
- [x] bot.py 集成：`message` / `message_parsed` / `intent_resolved` 三个事件钩子
- [x] 示例插件 `example_hello`
- [x] 异常隔离（单个 handler 崩溃不影响其他）

### 插件系统 — 阶段二：热加载 + Web 管理（2026-06-15）

- [x] mtime 跟踪 + 定时扫描（5秒间隔，`_hot_reload_loop`）
- [x] 新增/删除目录自动加载/卸载
- [x] 跨进程命令标记文件（`.plugin_cmd.json`）
- [x] Web API: `GET /api/plugins`
- [x] Web API: `POST /api/plugins/<name>/reload`
- [x] Web API: `POST /api/plugins/<name>/toggle`
- [x] Web API: `POST /api/plugins/info`
- [x] Web API: `POST /api/plugins/reload-all`
- [x] 前端：「插件管理」选项卡（列表/详情/重载/启用禁用）

### 重构清理

- [x] SF 意图兜底移除（低置信度直接走 chat）
- [x] 清理所有 SF fallback 遗留文档引用

---

## 架构总览（当前）

```
QQ群 → NapCat WS (18888) → bridge.py/bot.py → SiliconFlow AI → 回复
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

插件系统 (mod/)
├── PluginManager         ← 生命周期管理
├── EventBus              ← 事件中枢
│   ├── "message"           → _handle_group 入口
│   ├── "message_parsed"    → 解析完成后
│   ├── "intent_resolved"   → 路由后、handler 前
│   ├── "bot_start"         → 启动时
│   └── "bot_shutdown"      → 关闭时
└── plugins/
    └── example_hello/      ← 示例插件
```

### 模块依赖关系

```
bridge.py
  └→ bot.py
       ├→ config.py
       ├→ intent_router.py
       ├→ image_utils.py
       ├→ image_dedup.py
       ├→ gen_img.py
       └→ mod/ (插件系统)
            ├→ plugin_manager.py
            │    ├→ event_bus.py
            │    ├→ plugin_base.py
            │    └→ context.py
            └→ plugins/*/main.py

config_web.py (独立进程)
  ├→ config.yaml
  ├→ image_dedup.py
  ├→ image_compressor.py
  └→ .plugin_cmd.json (跨进程通信)
```

---

## 模块文件清单

```
napcat-greywind/
│
├── bridge.py                 # 入口（进程管理 + main）
├── bot.py                    # Bot 核心（WS 连接/消息路由/handler）
├── config.py                 # 配置加载（从 config.yaml）
├── config.yaml               # YAML 配置
├── config_web.py             # Web 配置面板（Flask, :7788）
├── intent_router.py          # 意图路由 + 参数提取
├── image_utils.py            # 图片压缩 + 图床管理
├── image_compressor.py       # 定时压缩调度器
├── image_dedup.py            # 图片去重（phash + SQLite）
├── gen_img.py                # AI 生图（12AI GPT-Image-2）
├── .gitignore
├── .reload_config.flag       # 热加载标记
├── .plugin_cmd.json          # 插件命令标记（运行时）
│
├── mod/                      # ← 插件系统（新增）
│   ├── __init__.py           # 公共 API 导出
│   ├── constants.py          # EventType 枚举 + 优先级常量
│   ├── event_bus.py          # 事件总线（发布/订阅）
│   ├── context.py            # PluginContext 依赖注入
│   ├── plugin_base.py        # PluginBase + 装饰器 + 事件对象
│   ├── plugin_manager.py     # PluginManager 生命周期
│   └── plugins/
│       ├── __init__.py
│       └── example_hello/    # 示例插件
│           ├── main.py
│           └── metadata.yaml
│
├── tc/                       # 图床工具集
│   ├── image_server.py       # HTTP 图床服务（:7777）
│   ├── send_image.py         # 发单张图
│   ├── send_images_batch.py  # 批量发图
│   ├── pick_fwd_image.py     # 选图脚本
│   └── 1/                    # 临时图片缓存
│
├── templates/
│   └── config_editor.html    # Web 面板前端
├── docs/                     # 需求/设计文档
│   ├── plugin_system_requirements.md    # 插件系统需求分析
│   ├── 意图路由关键词表.md              # 关键词匹配规则
│   ├── 意图路由+引用消息需求分析.md      # 旧需求文档
│   └── progress.md                     # ← 本文件
├── tests/                    # 测试脚本
├── start_bridge.bat          # 桥接启动脚本
└── start_config_web.bat      # 配置面板启动脚本
```

---

## 插件开发快速参考

### 最小插件（2 个文件）

```yaml
# mod/plugins/my_plugin/metadata.yaml
name: my_plugin
author: 我
version: 1.0.0
description: 描述
enabled: true
```

```python
# mod/plugins/my_plugin/main.py
from mod import PluginBase, on_message

class MyPlugin(PluginBase):
    @on_message()
    async def handle(self, event):
        await self.context.send_group_msg(event.group_id, "你好！")
        event.consume()
```

### 可用装饰器

| 装饰器 | 用途 |
|--------|------|
| `@on_message(priority=100)` | 监听所有群原始消息 |
| `@on_intent("chat", priority=100)` | 监听特定意图 |
| `@on_command("ping")` | 监听 `/ping` 命令 |
| `@on_regex(r"^hello")` | 正则匹配消息 |

### PluginContext API

| 方法 | 用途 |
|------|------|
| `send_group_msg(gid, text)` | 发文本消息 |
| `send_group_custom(gid, segments)` | 发自定义消息段 |
| `get_group_config(gid)` | 读群配置 |
| `get_bot_config()` | 读 config.yaml |
| `kv_get(key, default)` | 读持久化键值 |
| `kv_put(key, value)` | 写持久化键值 |
| `.log` | 插件专属 logger |

### EventBus 事件

| 事件 | 触发时机 | 可拦截 |
|------|---------|--------|
| `message` | 群消息到达 | ✅ |
| `message_parsed` | 消息解析完成 | ✅ |
| `intent_resolved` | 意图路由后 | ✅ |
| `bot_start` | 桥接启动 | ❌ |
| `bot_shutdown` | 桥接关闭 | ❌ |

---

## 待开发（下一阶段）

### 阶段三：功能扩展（P2）

- [ ] `@cron("0 */30 * * *")` 定时任务装饰器
  - [ ] 插件实现 `async def scheduled_task(self):` 自动定时触发
  - [ ] 基于 asyncio 的轻量 cron 调度器
- [ ] `@on_llm_request` / `@on_llm_response` LLM 拦截器
  - [ ] 在调用 SiliconFlow 前/后插入插件逻辑
  - [ ] 可修改 prompt、过滤回复、注入上下文
- [ ] `/clear` `/add` `/选图` 提取为内置插件
  - [ ] `builtin_commands/` 插件
  - [ ] `builtin_se_tu/` 插件（定时色图）
  - [ ] `builtin_mock/` 插件（存图嘲讽池）
- [ ] `requirements.txt` 插件依赖自动安装

### 阶段四：高级特性（P3）

- [ ] 插件市场（从 GitHub 下载/更新插件）
- [ ] 插件配置 UI schema（`_conf_schema.json` → Web 面板自动渲染）
- [ ] 插件性能监控（handler 耗时统计）
- [ ] 进程级隔离（worker 进程运行插件）

---

## 已知问题

| 问题 | 说明 | 优先级 |
|------|------|--------|
| bot.py 仍 ~2000 行 | 插件系统已完成，但现有功能尚未迁移为内置插件 | P2 |
| 热加载 mtime 精度 | Windows 下 FAT32/NTFS mtime 精度为 100ns，但部分编辑器的"保存"可能不更新 mtime | P3 |
| KV 存储 JSON 文件 | 高频写入有竞争风险，当前单线程 asyncio 下安全 | P3 |
| Web 面板插件状态 | config_web 和 bridge 在不同进程，插件"已加载/未加载"状态只能异步反映 | P2 |

---

## Git 历史

```
626d94d cleanup: 清除所有 SF 意图兜底遗留引用
8fd326a refactor: 移除 SF 意图兜底，低置信度直接走 chat
1ba7182 feat: 插件热加载 + Web 面板管理
b785018 feat: 插件系统第一阶段实现
4c3b6e8 Merge backup branch into main
8b23192 feat: 插件系统需求分析 + 图片去重模块 + 启动脚本
c32d4ed backup: 完整项目当前状态
451c341 Initial commit: GreyWind x NapCat 桥接 (v6.3)
f0dddbb Initial commit
```
