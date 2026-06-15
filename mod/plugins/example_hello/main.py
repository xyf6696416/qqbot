"""
示例插件 — Hello World
在群里回复 "hello" 时打招呼
"""

from mod.plugin_base import PluginBase
from mod import on_message


class HelloPlugin(PluginBase):
    """一个简单的示例插件"""

    async def initialize(self):
        self.log.info("HelloPlugin 已启动!")
        self.reply_count = 0

    @on_message(priority=100)
    async def handle_hello(self, event):
        text = (event.raw_message or "").strip().lower()
        if text == "hello":
            self.reply_count += 1
            await self.context.send_group_msg(
                event.group_id,
                f"Hello! 我是 {self.name} 插件~ (已回复 {self.reply_count} 次) 🤖"
            )
            event.consume()

    async def terminate(self):
        self.log.info("HelloPlugin 已停止，共回复 %d 次", self.reply_count)
