# 桥接插件系统 — 需求分析报告

> 日期：2026-06-15
> 参考系统：AstrBot (Star 插件系统)
> 目标：为 GreyWind × NapCat 桥接引入插件化架构

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [AstrBot 参考分析](#2-astrbot-参考分析)
3. [当前架构分析](#3-当前架构分析)
4. [功能需求](#4-功能需求)
5. [架构设计](#5-架构设计)
6. [事件系统设计](#6-事件系统设计)
7. [插件生命周期](#7-插件生命周期)
8. [插件 API 设计](#8-插件-api-设计)
9. [目录结构](#9-目录结构)
10. [与现有系统集成](#10-与现有系统集成)
11. [实施计划](#11-实施计划)

---

## 1. 背景与目标

### 1.1 为什么需要插件系统

当前桥接的所有功能（聊天、识图、发图、存图、生图、GIF、定时色图）都硬编码在 `Bot` 类中。随着功能增多，代码库膨胀到 2000+ 行单一文件，导致：

| 问题 | 表现 |
|------|------|
| **耦合度高** | 所有 handler 方法都在 Bot 类内部，新增功能必须修改核心代码 |
| **扩展困难** | 没有标准扩展点，用户无法添加自定义逻辑 |
| **维护成本高** | 单一文件接近 2000 行，新功能容易引入回归 |
| **复用性差** | 功能代码与框架代码混在一起，难以独立发布/复用 |

### 1.2 目标

1. 建立 **标准化插件框架**，允许第三方以模块化方式扩展桥接功能
2. 将现有部分功能 **逐步迁移为内置插件**，验证插件系统的完整性
3. 保持 **向后兼容**，现有 `config.yaml` 配置和行为不受影响
4. 实现 **热加载**，开发插件无需重启桥接
5. 提供 **轻量级 API**，插件开发者只需要了解少量概念即可上手

---

## 2. AstrBot 参考分析

### 2.1 AstrBot Star 系统概述

AstrBot（github.com/AstrBotDevs/AstrBot）是一个开源的 Python 机器人框架，其插件系统称为 **"Star" 系统**，有以下关键设计：

### 2.2 值得借鉴的设计

| 设计 | AstrBot 实现 | 参考价值 |
|------|-------------|---------|
| **基类 + 自动注册** | `Star` 基类 + `__init_subclass__` 自动发现 | ⭐⭐⭐ 核心机制 |
| **装饰器注册 handler** | `@filter.command()` / `@filter.regex()` 等 | ⭐⭐⭐ 开发者体验好 |
| **metadata.yaml** | 插件名称/作者/版本/描述等元数据声明 | ⭐⭐⭐ 标准化 |
| **Context 依赖注入** | `Context` 对象提供 LLM/DB/配置等访问 | ⭐⭐⭐ 解耦关键 |
| **生命周期钩子** | `initialize()` / `terminate()` | ⭐⭐⭐ 资源管理 |
| **事件驱动** | 消息经过 EventBus → 匹配 handler → 执行 | ⭐⭐⭐ 扩展性基础 |
| **热加载** | `watchfiles` 监控文件变化 → 重新加载插件 | ⭐⭐ 开发者体验 |

### 2.3 需简化的设计

| 设计 | 说明 | 处理方式 |
|------|------|---------|
| 进程隔离 (v4) | supervisor-worker 模型 | 本桥接单进程即可，暂不引入 |
| LLM Tool 系统 | `@filter.llm_tool` 注册 LLM 可调用工具 | 第一阶段不实现，后续可选 |
| i18n 国际化 | 插件多语言支持 | 当前不必要 |
| 配置 UI schema | `_conf_schema.json` | 第一阶段简单键值存储即可 |

---

## 3. 当前架构分析

### 3.1 消息处理流程

```
NapCat WS (18888)
    │
    ▼
Bot._handle_group(data)
    │  ┌─ 解析消息（文本/图片/转发/引用/At）
    │  ├─ 权限检查（admin_uids / forbidden_ops）
    │  ├─ 关键词触发检测
    │  ├─ 缓存消息到 context deque
    │  └─ 放入 group_queue
    │
    ▼
Bot._process_queue_item(item)
    │  ├─ 命令处理（/clear /add /选图）
    │  ├─ 色图直发 / GIF / 文件夹直发
    │  ├─ 意图路由（IntentRouter.keyword_route）
    │  └─ 分发到具体 handler
    │      ├─ _handle_chat()       → SiliconFlow Chat
    │      ├─ _handle_vision()     → SiliconFlow VL + Chat
    │      ├─ _handle_send_img()   → pick_fwd_image + 批量发送
    │      ├─ _handle_save_img()   → 压缩 + phash + 存盘
    │      ├─ _handle_gen_img()    → 12AI GPT-Image-2 + 发送
    │      └─ _handle_cmd()        → 管理员命令
    │
    ▼
Bot._enqueue_send() / send() → WS → NapCat → QQ群
```

### 3.2 关键集成点

插件需要能够 Hook 到以下节点：

```
节点 A: 消息到达（_handle_group 入口）
    └─ on_message(event) — 所有群消息的原始事件

节点 B: 消息解析完成后（text/img_urls/ref 就绪）
    └─ on_message_parsed(event) — 解析完成，即将路由

节点 C: 意图路由后（intent+params 就绪）
    └─ on_before_handler(intent, params) — 可以拦截/修改路由

节点 D: Handler 执行中
    └─ on_<intent>(event) — 特定意图的处理（插件可覆盖/扩展）

节点 E: Handler 执行后
    └─ on_after_handler(intent, result) — 回复已发送

节点 F: 启动/关闭
    └─ on_bot_start() / on_bot_shutdown()
```

### 3.3 可迁移为内置插件的现有功能

这些功能适合在第二阶段提取为内置插件，验证系统完整性：

| 现有功能 | 对应模块位置 | 作为插件的好处 |
|---------|-------------|---------------|
| 定时色图 | `Bot._se_tu_scheduler` | 可以按需启用/禁用 |
| GIF 处理 | `Bot._se_tu_send_gif` | 独立维护 |
| 存图去重 | `Bot._handle_save_img` 中的 dedup 逻辑 | 可被其他插件复用 |
| 每日限额 | `Bot._check_daily_img_limit` | 可被其他插件复用 |
| 命令系统 | `Bot._handle_cmd` 中的 /clear /add /选图 | 可扩展 |
| 嘲讽池 | `Bot._handle_save_img` 中的 _mock_pool | 独立插件 |

---

## 4. 功能需求

### 4.1 功能性需求

| ID | 需求 | 优先级 | 说明 |
|----|------|--------|------|
| F1 | 插件发现与加载 | P0 | 扫描 `mod/plugins/` 目录，自动发现并加载插件 |
| F2 | 插件元数据 | P0 | 每个插件声明 name/author/version/desc |
| F3 | 消息事件钩子 | P0 | 插件能接收和处理群消息事件 |
| F4 | 发送消息 API | P0 | 插件能通过桥接向群发送消息 |
| F5 | 生命周期管理 | P0 | initialize/terminate 钩子 |
| F6 | 热加载 | P1 | 修改插件文件后自动重载 |
| F7 | 插件配置存储 | P1 | 插件自己的持久化键值存储 |
| F8 | 事件优先级 | P1 | 多个插件可处理同一事件，按优先级执行 |
| F9 | 可禁用/启用 | P1 | 通过配置开关单个插件 |
| F10 | 现有功能迁移 | P2 | 将部分现有功能提取为内置插件 |
| F11 | Web 面板管理 | P2 | 配置面板可查看/管理插件 |
| F12 | 插件依赖管理 | P3 | `requirements.txt` 自动安装 |

### 4.2 非功能性需求

| ID | 需求 | 说明 |
|----|------|------|
| N1 | 低侵入 | 现有功能不受影响，不改动现有 config.yaml 结构 |
| N2 | 异常隔离 | 单个插件崩溃不影响桥接核心和其他插件 |
| N3 | 性能 | 插件调度开销 < 1ms/事件（无 IO）|
| N4 | 易用性 | 开发一个简单插件不超过 10 行代码 |
| N5 | 文档 | 提供示例插件和开发文档 |

---

## 5. 架构设计

### 5.1 整体架构

```
napcat-greywind/
├── mod/                              ← NEW: 插件系统根目录
│   ├── __init__.py
│   ├── plugin_base.py                ← 插件基类定义
│   ├── plugin_manager.py             ← 插件管理器（生命周期全权负责）
│   ├── event_bus.py                  ← 事件总线
│   └── plugins/                      ← 第三方插件存放目录
│       ├── __init__.py
│       └── example_plugin/           ← 示例插件
│           ├── main.py
│           └── metadata.yaml
│
├── bot.py            ← 微调: 在关键节点触发事件总线
├── bridge.py         ← 不变
├── config.py         ← 微调: 添加 mod 相关配置项
├── config_web.py     ← 增强: 添加插件管理路由
│
└── (其余现有文件不变)
```

### 5.2 核心组件职责

| 组件 | 文件 | 职责 |
|------|------|------|
| `PluginBase` | `mod/plugin_base.py` | 所有插件的基类，定义生命周期和 API |
| `PluginManager` | `mod/plugin_manager.py` | 扫描、加载、卸载、热重载插件 |
| `EventBus` | `mod/event_bus.py` | 事件发布-订阅，按优先级调度 handler |
| `BotContext` | `mod/plugin_base.py` 或独立文件 | 注入给插件的上下文（访问 bot 能力） |

### 5.3 数据流

```
群消息 → Bot._handle_group()
    │
    ▼
[EventBus.emit("message", MessageEvent)]
    │  ├─ 插件 A handler 处理（优先级 10）
    │  └─ 插件 B handler 处理（优先级 20）
    │
    ▼  （如果插件未消费事件）
Bot._handle_group() 继续执行原有逻辑
    │
    ▼
[EventBus.emit("intent_resolved", IntentEvent)]
    │  └─ 插件可修改/拦截意图
    │
    ▼
Bot 执行 intent handler
    │
    ▼
[EventBus.emit("reply", ReplyEvent)]
    │  └─ 插件可修改回复内容
    │
    ▼
发送到群
```

---

## 6. 事件系统设计

### 6.1 事件类型

```python
class EventType(enum.Enum):
    # 生命周期事件
    BOT_START = "bot_start"           # 桥接启动完成
    BOT_SHUTDOWN = "bot_shutdown"     # 桥接关闭前
    
    # 消息事件
    MESSAGE = "message"               # 收到群消息（原始数据）
    MESSAGE_PARSED = "message_parsed" # 消息已解析（text/img_urls 就绪）
    
    # 路由事件
    INTENT_RESOLVED = "intent_resolved"  # 意图路由完成
    BEFORE_HANDLER = "before_handler"    # handler 执行前
    AFTER_HANDLER = "after_handler"      # handler 执行后
    
    # 特定意图事件（可以直接监听具体意图）
    ON_CHAT = "on_chat"
    ON_VISION = "on_vision"
    ON_SEND_IMG = "on_send_img"
    ON_SAVE_IMG = "on_save_img"
    ON_GEN_IMG = "on_gen_img"
    ON_CMD = "on_cmd"
    
    # 插件生命周期
    PLUGIN_LOADED = "plugin_loaded"
    PLUGIN_UNLOADED = "plugin_unloaded"
    PLUGIN_ERROR = "plugin_error"
```

### 6.2 事件总线实现

```python
# mod/event_bus.py 核心设计
class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[HandlerEntry]] = {}
        # _handlers = {
        #     "message": [
        #         HandlerEntry(handler_fn, priority=10, plugin_name="xxx"),
        #         HandlerEntry(handler_fn, priority=20, plugin_name="yyy"),
        #     ]
        # }
    
    def on(self, event_type: str, handler, priority: int = 100):
        """注册事件监听（用于装饰器）"""
    
    def emit(self, event_type: str, event, *, cancellable=False) -> EventResult:
        """发布事件，按优先级调用 handler"""
    
    def remove_plugin_handlers(self, plugin_name: str):
        """移除某插件的所有 handler（卸载时调用）"""
```

### 6.3 事件优先级

- 优先级 **数字越小越先执行**
- 默认优先级：`100`
- 系统内置 handler：`0-50`（预留）
- 高优先级插件：`51-100`
- 普通插件：`100-200`
- 低优先级插件：`201-999`
- 任意 handler 可以 `consume()` 事件阻止后续 handler 执行

### 6.4 事件对象

```python
@dataclass
class MessageEvent:
    """原始消息事件"""
    group_id: str
    user_id: str
    raw_data: dict
    raw_message: str
    message: list           # [{"type": "text", "data": {"text": "..."}}, ...]

@dataclass
class ParsedMessageEvent:
    """解析后的消息事件"""
    group_id: str
    user_id: str
    text: str
    img_urls: list[str]
    at_bot: bool
    reply_to: str | None
    ref_context: RefContext
    
@dataclass 
class IntentEvent:
    """意图路由事件"""
    group_id: str
    user_id: str
    text: str
    intent: str              # chat / vision / save_img / send_img / gen_img
    confidence: str
    params: dict
    cancelled: bool = False  # 插件设为 True 可拦截此次处理
```

---

## 7. 插件生命周期

### 7.1 生命周期阶段

```
  [发现]
    │  PluginManager.scan_plugins()
    │  扫描 mod/plugins/ 下所有子目录
    │  检查 metadata.yaml 是否存在
    ▼
  [加载]
    │  PluginManager.load_plugin()
    │  importlib.import_module() 导入 main.py
    │  __init_subclass__ 自动注册 → 生成 PluginMeta
    ▼
  [注册]
    │  PluginManager.register_plugin()
    │  解析 metadata.yaml → 合并到 PluginMeta
    │  注册装饰器收集的 handler → EventBus
    ▼
  [初始化]
    │  plugin.initialize()
    │  （异步）建立连接、启动定时器、加载资源
    ▼
  [运行]
    │  plugin.is_active = True
    │  handler 响应事件
    ▼
  [终止]
    │  plugin.terminate()
    │  取消定时器、关闭连接、清理资源
    │  EventBus 移除该插件的所有 handler
    │  sys.modules 清理
```

### 7.2 状态机

```
        ┌──────────┐
        │  DISCOVERED  │
        └─────┬────┘
              │ load
        ┌─────▼────┐
        │  LOADED    │ ← 导入成功，__init_subclass__ 已触发
        └─────┬────┘
              │ register
        ┌─────▼────┐
        │ REGISTERED│ ← handlers 已注册到 EventBus
        └─────┬────┘
              │ initialize()
        ┌─────▼────┐
        │  ACTIVE   │ ← 正常运行
        └─────┬────┘
              │ terminate()
        ┌─────▼────┐
        │  STOPPED  │ ← 已卸载
        └──────────┘
```

### 7.3 热重载机制

```
1. 检测到文件变化（inotify/watchfiles 或定期扫描 mtime）
2. 调用当前插件实例的 terminate()
3. EventBus 移除该插件的所有 handler
4. 从 sys.modules 中弹出相关模块缓存
5. 清理 PluginManager._registry 中的记录
6. 重新 import，触发 __init_subclass__
7. 重新注册 handler 到 EventBus
8. 调用新实例的 initialize()
```

---

## 8. 插件 API 设计

### 8.1 插件基类

```python
# mod/plugin_base.py

class PluginBase(metaclass=PluginMeta):
    """所有插件必须继承自此类。"""
    
    def __init__(self, context: PluginContext):
        self.context = context
        self.log = logging.getLogger(f"plugin.{self.name}")
    
    @property
    def name(self) -> str:
        """插件名，由 PluginMeta 自动填充"""
        return self._plugin_meta.name
    
    # ── 生命周期钩子 ──
    
    async def initialize(self):
        """异步初始化。启动时调用。"""
        pass
    
    async def terminate(self):
        """异步清理。卸载/重载/关闭时调用。"""
        pass
    
    # ── 事件监听注册 ──
    # （通过装饰器在类定义时收集，由 PluginMeta 处理）
```

### 8.2 装饰器 API

```python
# 插件开发示例
from mod.plugin_base import PluginBase, on_message, on_intent, on_command
from mod.context import PluginContext

class GreetingPlugin(PluginBase):
    """打招呼插件"""
    
    async def initialize(self):
        self.log.info("问候插件已启动")
        self.greeted_users = set()
    
    @on_message(priority=50)
    async def greet_new_user(self, event: MessageEvent):
        """用户第一次说话时打招呼"""
        if event.user_id not in self.greeted_users:
            self.greeted_users.add(event.user_id)
            await self.context.send_group_msg(
                event.group_id,
                f"欢迎 {event.user_id} 来到本群~"
            )
    
    @on_intent("chat", priority=200)
    async def custom_chat(self, event: IntentEvent):
        """在 AI 回复前添加自定义逻辑"""
        if "天气" in event.text:
            # 处理天气查询，不交给 AI
            await self.context.send_group_msg(
                event.group_id, 
                "今天天气不错~"
            )
            event.cancelled = True  # 阻止后续 handler
    
    async def terminate(self):
        self.greeted_users.clear()
        self.log.info("问候插件已停止")
```

### 8.3 拓展装饰器

```python
# 支持的装饰器（一期）
@on_message(priority=100)           # 监听所有群消息
@on_intent("chat", priority=100)    # 监听特定意图
@on_command("hello")                 # 监听 /hello 命令
@on_regex(r"^hi", priority=100)     # 正则匹配消息

# 支持的装饰器（二期）
@on_llm_request                     # 拦截 LLM 请求
@on_llm_response                    # 修改 LLM 回复
@cron("0 */30 * * * *")            # 定时任务（每30分钟）
```

### 8.4 PluginContext API

```python
class PluginContext:
    """注入给插件的上下文对象"""
    
    # ── 发送消息 ──
    async def send_group_msg(self, group_id: str, text: str):
        """向群发送文本消息"""
    
    async def send_group_images(self, group_id: str, image_paths: list[str]):
        """向群发送图片"""
    
    # ── 获取信息 ──
    def get_group_config(self, group_id: str) -> dict:
        """获取群配置"""
    
    def get_bot_config(self) -> dict:
        """获取完整配置"""
    
    # ── 持久化存储 ──
    async def kv_get(self, key: str, default=None):
        """获取插件自己的持久化键值"""
    
    async def kv_put(self, key: str, value):
        """设置插件自己的持久化键值"""
    
    # ── 访问共享服务 ──
    async def llm_chat(self, prompt: str, system_prompt: str = "") -> str:
        """调用主 LLM API"""
    
    async def call_siliconflow(self, messages: list) -> str:
        """直接调用 SiliconFlow API"""
```

---

## 9. 目录结构

### 9.1 `mod/` 目录布局

```
mod/                                    ← 插件系统根
├── __init__.py                         # 导出 PluginBase, EventBus 等
├── plugin_base.py                      # 插件基类 PluginBase + PluginMeta
├── plugin_manager.py                   # PluginManager（生命周期全权负责）
├── event_bus.py                        # EventBus（事件发布订阅）
├── context.py                          # PluginContext（依赖注入）
├── constants.py                        # 事件类型枚举、常量
│
├── plugins/                            # 第三方插件存放目录
│   ├── __init__.py
│   │
│   ├── builtin_se_tu/                  # （二期）内置定时色图插件
│   │   ├── main.py
│   │   └── metadata.yaml
│   │
│   ├── builtin_commands/               # （二期）内置 /clear /add 等命令
│   │   ├── main.py
│   │   └── metadata.yaml
│   │
│   └── example_hello/                  # 示例插件
│       ├── main.py
│       └── metadata.yaml
│
└── examples/                           # 示例代码
    └── simple_plugin.py
```

### 9.2 单个插件目录结构

```
mod/plugins/my_plugin/
├── main.py              # 入口文件：定义插件类（必须）
├── metadata.yaml        # 元数据声明（必须）
├── requirements.txt     # Python 依赖（可选）
└── data/                # 插件私有数据（可选）
    └── ...
```

### 9.3 `metadata.yaml` 格式

```yaml
name: my_plugin              # 插件标识符（Python 合法的模块名）
display_name: 我的插件        # 显示名称（可选，默认同 name）
author: 作者名
version: 1.0.0
description: 这是描述文字
priority: 100                # 默认事件优先级（可选）
enabled: true                # 默认启用（可选）
bridge_version: ">=1.0.0"    # 桥接版本约束（可选）
```

---

## 10. 与现有系统集成

### 10.1 `bot.py` 修改点

```python
# bot.py 中的修改（最小侵入）

class Bot:
    def __init__(self):
        # ... 现有代码 ...
        
        # NEW: 插件系统初始化
        self.plugin_manager = PluginManager(self._get_context())
    
    async def run(self):
        # ... 现有代码 ...
        
        # NEW: 加载插件
        await self.plugin_manager.load_all()
        EventBus.emit("bot_start", {})
    
    async def _handle_group(self, data):
        # ... 现有解析代码 ...
        
        # NEW: 事件钩子 - 消息到达
        event = MessageEvent(gid, uid, data, ...)
        if EventBus.emit("message", event, cancellable=True).consumed:
            return  # 插件已处理，跳过默认流程
        
        # ... 继续现有流程 ...
    
    async def _process_queue_item(self, item):
        # 路由后、handler 前
        intent_event = IntentEvent(gid, uid, text, intent, params)
        if EventBus.emit("intent_resolved", intent_event, cancellable=True).consumed:
            return
        
        # 原有 dispatch 逻辑...
    
    async def close(self):
        EventBus.emit("bot_shutdown", {})
        await self.plugin_manager.unload_all()
        # ... 现有清理代码 ...
```

### 10.2 `config.py` 修改点

```python
# config.py 新增配置项
MOD_ENABLED = cfg.get("mod", {}).get("enabled", True)
MOD_PLUGINS_DIR = os.path.join(BASE_DIR, "mod", "plugins")
MOD_AUTO_RELOAD = cfg.get("mod", {}).get("auto_reload", True)
```

### 10.3 `config.yaml` 修改点

```yaml
# 新增 mod 插件系统配置
mod:
  enabled: true            # 是否启用插件系统
  auto_reload: true        # 是否启用热加载
  plugins:                 # 插件级配置
    greeting_plugin:
      enabled: true
      custom_greeting: "你好呀~"
```

### 10.4 `config_web.py` 修改点

新增 API 路由：
- `GET /api/plugins` — 插件列表及状态
- `POST /api/plugins/<name>/toggle` — 启用/禁用插件
- `GET /api/plugins/<name>/config` — 插件配置
- `POST /api/plugins/<name>/config` — 更新插件配置

前端 `config_editor.html` 新增「插件管理」选项卡。

---

## 11. 实施计划

### 第一阶段：基础框架（优先级 P0）

| 步骤 | 内容 | 预计代码量 |
|------|------|-----------|
| 1 | 搭建 `mod/` 目录结构，创建 `__init__.py` | ~10 行 |
| 2 | 实现 `PluginBase` 基类 + `PluginMeta` metaclass | ~100 行 |
| 3 | 实现 `EventBus` 事件总线 | ~80 行 |
| 4 | 实现 `PluginManager`（扫描、加载、注册） | ~150 行 |
| 5 | 实现 `PluginContext`（send_msg, kv 存储） | ~80 行 |
| 6 | 实现 `on_message` / `on_intent` 装饰器 | ~50 行 |
| 7 | `bot.py` 集成：在关键节点插入 EventBus.emit() | ~30 行 |
| 8 | `config.py` / `config.yaml` 添加 mod 配置 | ~10 行 |
| 9 | 开发 `example_hello` 示例插件 | ~20 行 |
| 10 | 测试：插件加载 + 消息事件 + 发送回复 | — |

**第一阶段完成标志**：能在群里发消息触发示例插件回复。

### 第二阶段：完善功能（优先级 P1）

| 步骤 | 内容 |
|------|------|
| 1 | 实现热加载（watchfiles 或定期扫描） |
| 2 | 实现插件级持久化 KV 存储（JSON 文件） |
| 3 | 实现 `on_command` / `on_regex` 装饰器 |
| 4 | `config_web.py` 添加插件管理页面 |
| 5 | 插件错误隔离（try-except 包裹每个 handler） |
| 6 | 将 `_se_tu_scheduler` 提取为内置插件验证系统 |

### 第三阶段：扩展与迁移（优先级 P2）

| 步骤 | 内容 |
|------|------|
| 1 | 将 `/clear` `/add` `/选图` 等命令提取为内置插件 |
| 2 | 支持 `requirements.txt` 依赖自动安装 |
| 3 | 插件市场机制（从 GitHub 下载插件） |
| 4 | `@cron()` 定时任务装饰器 |
| 5 | `@on_llm_request` / `@on_llm_response` 拦截器 |

---

## 附录 A：最小示例插件

```python
"""mod/plugins/example_hello/main.py - 最小插件示例"""

from mod.plugin_base import PluginBase, on_message
from mod.context import PluginContext

class HelloPlugin(PluginBase):
    """当群友发"hello"时回复"""
    
    @on_message(priority=100)
    async def on_hello(self, event):
        text = event.text.strip().lower()
        if text == "hello":
            await self.context.send_group_msg(
                event.group_id,
                f"Hello! 我是 {self.name} 插件 v1.0"
            )
            event.consume()  # 已处理
```

```yaml
# mod/plugins/example_hello/metadata.yaml
name: hello_world
author: GreyWind
version: 1.0.0
description: 一个简单的示例插件，回复 hello
enabled: true
```

---

## 附录 B：参考来源

- [AstrBot GitHub](https://github.com/AstrBotDevs/AstrBot) — 开源 Python 机器人框架
- [AstrBot Plugin System (Stars) — DeepWiki](https://deepwiki.com/AstrBotDevs/AstrBot/7-plugin-system-(stars))
- [AstrBot Star Base Class & Lifecycle — DeepWiki](https://deepwiki.com/AstrBotDevs/AstrBot/7.1-plugin-architecture-and-lifecycle)
- [AstrBot Plugin Loading & Registration — DeepWiki](https://deepwiki.com/AstrBotDevs/AstrBot/7.2-plugin-loading-and-registration)
- [AstrBot Hot Reload — DeepWiki](https://deepwiki.com/AstrBotDevs/AstrBot/7.6-hot-reload-and-development)
- [AstrBot Plugin Development Guide (中文)](https://zread.ai/AstrBotDevs/AstrBot/15-plugin-development-guide)
- [AstrBot Context API — DeepWiki](https://deepwiki.com/AstrBotDevs/AstrBot/2.4-context-api-for-plugins)
- [AstrBot Wiki — Simple Star Guide](https://github.com/AstrBotDevs/AstrBot/wiki/en-dev-star-guides-simple)
