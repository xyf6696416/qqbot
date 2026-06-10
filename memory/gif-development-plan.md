---
name: gif-development-plan
description: GIF 发图功能分离的开发规划，已实现 gif xx 触发发图
metadata: 
  node_type: memory
  type: project
  originSessionId: 012414f4-f494-43da-ad38-63870dd8cd24
---

## GIF 功能开发

### 已完成
- ✅ `tc/pick_fwd_image.py` 新增 `--gif` 参数
- ✅ `bot.py` 发图默认排除 GIF（静图发图不变）
- ✅ `bot.py` 存图和 `/生图` 参考图下载改用文件头识别格式
- ✅ `bot.py` `_se_tu_send_gif()` 方法：GIF 顺序窗口选图 + 防重复
- ✅ `bot.py` `gif xx` 触发：`gif <分类名>` 从 `桌面\转发图片\GIF\<分类名>\` 发 3 个 GIF
- ✅ `gif 列表` 列出可用 GIF 分类
- ✅ 选图策略：按文件名排序 → 筛选未发送 → 随机起始位取连续 3 张 → 标记已发送
- ✅ 防重复机制：复用 `.pick_fwd_state.json`，key 格式 `src_gif_<分类名>`
- ✅ 全部发完后自动重置

### 触发方式
- `gif 列表` → 显示可用 GIF 分类及数量
- `gif zmd` → 从 `GIF/zmd/` 发 3 张
- `gif 萝莉` → 从 `GIF/萝莉/` 发 3 张
- 使用 10 秒冷却防刷屏

**Why:** GIF 文件不需要压缩（保持动画），与静态图分开管理。顺序窗口模式保证按时间顺序浏览，随机起点增加变化。

**How to apply:** 在 QQ 群发 `gif zmd` 即可触发。
