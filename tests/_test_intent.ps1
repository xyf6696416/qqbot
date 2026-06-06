$key = "YOUR_SILICONFLOW_API_KEY"

Write-Host "=== 意图分类测试 (SF Qwen2.5-7B-Instruct) ==="
Write-Host ""

# 测试1: 发图 - 明确
$body1 = @"
{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"system","content":"你是一个QQ群聊消息的意图分类器。根据用户消息输出意图类别，只输出一个词：send_img(发图), vision(识图), chat(普通聊天), save_img(存图)。"},{"role":"user","content":"发张萝莉图看看"}],"max_tokens":10,"temperature":0.1}
"@

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$r1 = Invoke-RestMethod -Uri "https://api.siliconflow.cn/v1/chat/completions" -Method Post -Body $body1 -ContentType "application/json" -Headers @{Authorization="Bearer $key"} -TimeoutSec 15 -Proxy "http://127.0.0.1:7890"
$sw.Stop()
Write-Host "测试1 (发张萝莉图): $($r1.choices[0].message.content)  [${($sw.ElapsedMilliseconds)}ms]"

# 测试2: 识图
$body2 = @"
{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"system","content":"你是一个QQ群聊消息的意图分类器。根据用户消息输出意图类别，只输出一个词：send_img(发图), vision(识图), chat(普通聊天), save_img(存图)。"},{"role":"user","content":"这张图里是啥"}],"max_tokens":10,"temperature":0.1}
"@
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$r2 = Invoke-RestMethod -Uri "https://api.siliconflow.cn/v1/chat/completions" -Method Post -Body $body2 -ContentType "application/json" -Headers @{Authorization="Bearer $key"} -TimeoutSec 15 -Proxy "http://127.0.0.1:7890"
$sw.Stop()
Write-Host "测试2 (这张图里是啥): $($r2.choices[0].message.content)  [${($sw.ElapsedMilliseconds)}ms]"

# 测试3: 普通对话
$body3 = @"
{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"system","content":"你是一个QQ群聊消息的意图分类器。根据用户消息输出意图类别，只输出一个词：send_img(发图), vision(识图), chat(普通聊天), save_img(存图)。"},{"role":"user","content":"今天天气不错啊"}],"max_tokens":10,"temperature":0.1}
"@
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$r3 = Invoke-RestMethod -Uri "https://api.siliconflow.cn/v1/chat/completions" -Method Post -Body $body3 -ContentType "application/json" -Headers @{Authorization="Bearer $key"} -TimeoutSec 15 -Proxy "http://127.0.0.1:7890"
$sw.Stop()
Write-Host "测试3 (今天天气不错): $($r3.choices[0].message.content)  [${($sw.ElapsedMilliseconds)}ms]"

# 测试4: 模糊情况 - 好康
$body4 = @"
{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"system","content":"你是一个QQ群聊消息的意图分类器。根据用户消息输出意图类别，只输出一个词：send_img(发图), vision(识图), chat(普通聊天), save_img(存图)。"},{"role":"user","content":"来点好康的"}],"max_tokens":10,"temperature":0.1}
"@
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$r4 = Invoke-RestMethod -Uri "https://api.siliconflow.cn/v1/chat/completions" -Method Post -Body $body4 -ContentType "application/json" -Headers @{Authorization="Bearer $key"} -TimeoutSec 15 -Proxy "http://127.0.0.1:7890"
$sw.Stop()
Write-Host "测试4 (来点好康的): $($r4.choices[0].message.content)  [${($sw.ElapsedMilliseconds)}ms]"

# 测试5: JSON 结构化输出 + 参数提取
$body5 = @"
{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"system","content":"分析用户消息，提取意图和参数。只输出JSON：{\"intent\":\"chat|send_img|vision|save_img\",\"count\":1,\"src\":\"文件夹名或null\"}"},{"role":"user","content":"发两张脚的色图"}],"max_tokens":60,"temperature":0.1}
"@
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$r5 = Invoke-RestMethod -Uri "https://api.siliconflow.cn/v1/chat/completions" -Method Post -Body $body5 -ContentType "application/json" -Headers @{Authorization="Bearer $key"} -TimeoutSec 15 -Proxy "http://127.0.0.1:7890"
$sw.Stop()
Write-Host "测试5 (发两张脚的色图 含参数): $($r5.choices[0].message.content)  [${($sw.ElapsedMilliseconds)}ms]"

# 测试6: 表情包 - 不要重定向
$body6 = @"
{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"system","content":"你是一个QQ群聊消息的意图分类器。根据用户消息输出意图类别，只输出一个词：send_img(发图), vision(识图), chat(普通聊天), save_img(存图), forward_msg(合并转发)。"},{"role":"user","content":"这消息别删啊"}],"max_tokens":10,"temperature":0.1}
"@
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$r6 = Invoke-RestMethod -Uri "https://api.siliconflow.cn/v1/chat/completions" -Method Post -Body $body6 -ContentType "application/json" -Headers @{Authorization="Bearer $key"} -TimeoutSec 15 -Proxy "http://127.0.0.1:7890"
$sw.Stop()
Write-Host "测试6 (这消息别删啊): $($r6.choices[0].message.content)  [${($sw.ElapsedMilliseconds)}ms]"

# 测试7: 存图
$body7 = @"
{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"system","content":"你是一个QQ群聊消息的意图分类器。根据用户消息输出意图类别，只输出一个词：send_img(发图), vision(识图), chat(普通聊天), save_img(存图)。"},{"role":"user","content":"这张图帮我存起来"}],"max_tokens":10,"temperature":0.1}
"@
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$r7 = Invoke-RestMethod -Uri "https://api.siliconflow.cn/v1/chat/completions" -Method Post -Body $body7 -ContentType "application/json" -Headers @{Authorization="Bearer $key"} -TimeoutSec 15 -Proxy "http://127.0.0.1:7890"
$sw.Stop()
Write-Host "测试7 (这张图帮我存起来): $($r7.choices[0].message.content)  [${($sw.ElapsedMilliseconds)}ms]"

# 测试8: 边缘 - 包含发但其实是聊天
$body8 = @"
{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"system","content":"你是一个QQ群聊消息的意图分类器。根据用户消息输出意图类别，只输出一个词：send_img(发图), vision(识图), chat(普通聊天), save_img(存图)。"},{"role":"user","content":"我去发个消息给老板"}],"max_tokens":10,"temperature":0.1}
"@
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$r8 = Invoke-RestMethod -Uri "https://api.siliconflow.cn/v1/chat/completions" -Method Post -Body $body8 -ContentType "application/json" -Headers @{Authorization="Bearer $key"} -TimeoutSec 15 -Proxy "http://127.0.0.1:7890"
$sw.Stop()
Write-Host "测试8 (我去发个消息给老板): $($r8.choices[0].message.content)  [${($sw.ElapsedMilliseconds)}ms]"

Write-Host ""
Write-Host "=== 测试完成 ==="
