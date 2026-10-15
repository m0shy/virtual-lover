# 插件清单

全部通过 **AstrBot 面板 → 插件 → 插件市场/安装** 或聊天命令 `/plugin install <仓库地址>` 安装。
装完在面板「插件配置」页改配置（不用改 json 文件）。

| 插件 | 仓库 | 用途 | 建议 |
|---|---|---|---|
| astrbot_plugin_mnemosyne | https://github.com/lxfight/astrbot_plugin_mnemosyne | 长期记忆：对话自动总结存入 Milvus，聊天时检索注入 | ★必装（本项目的记忆核心，需 Milvus 已启动 + 面板已配 Embedding 模型）装完先聊两句再执行 `/memory init`。**与 AstrBot v4.28.x 搭配需两处本地 patch**（上游 v2.1.x 尚未适配）：① 插件 `requirements.txt` 里 `google-genai>=1.11.0,<2.0.0` 删掉 `<2.0.0`；② `core/initialization.py` 里 `astrbot_config["provider_settings"]["max_context_length"]` 改为 `.get("max_context_length", settings.get("max_context_tokens", -1))` 式的兼容读取。不 patch 的症状：记忆插件静默加载失败/日志报「配置检查失败: 'max_context_length'」。上游 v2.3.0+ 已改 chromadb 架构，与本项目的 Milvus 编排暂不匹配，建议先用 v2.1.x |
| astrbot_plugin_proactive_chat | https://github.com/DBJD-CR/astrbot_plugin_proactive_chat | 主动消息：会话静默后按随机间隔主动找你说话（免打扰时段、TTS、分段发送） | 推荐装；配置见 `config-samples/proactive_chat.example.json`，目标会话用 `/sid` 查 |
| astrbot_plugin_meme_manager | https://github.com/anka-afk/astrbot_plugin_meme_manager | 表情包管理：`&&标签&&` 自动配图，WebUI 管理图库 | 推荐装；先 `表情管理 开启管理后台` 再导入你自己的表情 |
| astrbot_plugin_parser | https://github.com/Zhalslar/astrbot_plugin_parser | 万能链接解析：B站/抖音/微博/小红书等 12 平台链接转视频图集直发 | 可选；解析 B 站高清可再扫码登录 |
| astrbot_plugin_apis | https://github.com/Zhalslar/astrbot_plugin_apis | API 聚合：160+ 免费 API 按关键词触发（天气/文案/图片…） | 可选；依赖 [api-aggregator](https://github.com/Zhalslar/api-aggregator) 管理面板（端口 4141） |
| astrbot_plugin_lzpersona | https://github.com/idiotsj/astrbot_plugin_lzpersona | AI 人格生成器：自然语言描述直接生成/优化人格提示词 | 可选；配合 `deploy/persona-template.txt` 使用更佳 |

## 记忆插件（mnemosyne）上手

1. 面板「服务提供商」先配好一个 **Embedding** 模型（SiliconFlow 的 `BAAI/bge-m3` 免费额度即可，或火山方舟/OpenAI 兼容均可）
2. 安装插件 → 配置保持默认（地址 `milvus-standalone:19530`，示例见 `config-samples/mnemosyne.example.json`）
3. 和机器人聊 10~20 句后，管理员发 `/memory init`
4. 之后她会自动记住你的偏好与聊过的事；`/memory list` 查看

## 主动消息（proactive_chat）上手

⚠️ 先记住两个最常见的坑：

- **时序**：目标会话 ID 要等你和机器人**聊过天之后**才拿得到——装插件时这一栏先空着，第一次聊天后发 `/sid`，再回面板填写
- **格式**：`/sid` 返回形如 `aiocqhttp:FriendMessage:你的QQ号`——**前缀是「平台 ID」不是人格名**（即 AstrBot 面板-机器人 里 aiocqhttp 适配器的 ID）。没改过适配器 ID 的直接**整串照抄 /sid 的结果**即可；只有你在面板把适配器 ID 改成了别的名字，前缀才需要同步改。直接抄别人的示例前缀（如人格名），主动消息会静默失效、连报错都没有

1. 按上述规则在面板插件配置填「目标会话」（完整示例见 `config-samples/proactive_chat.example.json`）
2. 调度区间默认 30~120 分钟随机，免打扰 23:00–7:00；改完配置需「重载插件」
