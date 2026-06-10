---
name: config-web-rename-feature
description: 配置前端文件压缩页添加了一键重命名按钮
metadata:
  type: project
---

## 一键重命名功能

在配置前端 `图片压缩` 页添加了「一键重命名」按钮。

**后端:** `config_web.py` — `POST /api/compressor/rename`
- 读取 `image_compressor.scan_dirs` 配置
- 递归收集目录下所有图片文件（jpg/png/webp/bmp/gif/tiff）
- 按文件修改时间 mtime 排序
- 重命名为 `YYYYMMDD_HHmmss_NNN.ext` 格式（日期+时分秒+3位序号）
- 已是该格式的文件自动跳过（幂等）

**前端:** `templates/config_editor.html`
- 压缩配置卡片底部添加了「🔄 一键重命名」按钮
- 点击弹出 confirm 确认框
- 执行时按钮禁用并显示「⏳ 重命名中...」
- 完成后 toast 显示结果（成功数/跳过数/失败数）
- 新增 `.btn-warning` 样式（暗金色）和 `.toast.warning` 样式
