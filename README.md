# Virtual-lover

[![CI](https://github.com/m0shy/virtual-lover/actions/workflows/ci.yml/badge.svg)](https://github.com/m0shy/virtual-lover/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[简体中文](README.md) · [English](README.en.md)

[English](README.en.md) | 中文

一套开箱即用的**私人 AI QQ 伴侣系统**：她住在你的 QQ 好友列表里，有自己的人格和说话方式，像真人一样分段连发消息、会主动来找你聊天、记得你说过的话（长期记忆）、会发表情包，还能用你自己训练的音色发语音。

```
                她 = 1 个进程 + 2 个器官

   你的 QQ ⇄ [器官① 协议端容器 vl-heart]（QQ 协议端，WebUI 扫码）
                     ⇅ 反向 WS（127.0.0.1:6199，Bearer token）
   ┌─────────────────── 宿主机：你的 Windows ───────────────────┐
   │ virtuallover/ —— Python ≥3.11 单进程（她的大脑）           │
   │   bridge   OneBot v11 反向 WS 服务端                        │
   │   brain    人格装配 · LLM 双客户端 · 智能降级 · 会话与上下文 │
   │   memory   内嵌向量记忆（numpy 余弦检索，无需向量库容器）     │
   │   soul     情绪引擎 · 日记 · 主动消息 · 表情包               │
   │   console  管理控制台（127.0.0.1:8787，唯一管理入口）        │
   └────────────────────────────────────────────────────────────┘
                     ⇅ HTTP（可选语音）
        [器官② 语音引擎进程 voice-engine]（127.0.0.1:5000，用你训练的音色）
```

旧版（v1）以 AstrBot 容器为大脑、Milvus/etcd/minio 三容器为记忆库；**v2 已整体替换为上图的独立实现**，旧的五容器编排保留在 `docker-compose.yml` 的 `legacy` 组里，仅供 14 天回退窗口（本机 AstrBot 镜像已清除，整组拉起会失败，见 docs/回退.md R6 声明）使用（`docker compose --profile legacy up -d`）。

全部跑在你自己的电脑上；聊天数据、记忆、密钥只落在本地 `local/` 目录（不会进入 git，也不会上传到本项目任何服务器——聊天内容会发送给你自己选择的大模型 / Embedding 服务商，这是使用云端 API 的固有形态，介意者请接本地模型）。

## 自研核心：三个原创模块（v1 以插件形态实现，v2 已并入新栈）

这个仓库不只是部署编排——它包含三个为本项目原创开发、带完整单元测试的核心模块（v1 时代以 AstrBot 插件形态活在 `plugins/`，v2 起并入 `virtuallover/`：`soul/mood.py`、`soul/diary.py` 与 `commands.py` 的 /eval 命令面；v1 插件目录与测试已随 v2 验收退役删除（O11），历史形态见 git，来源标注见 [provenance](docs/provenance.md)）：

| 模块 | 是什么 | 亮点 |
|---|---|---|
| **vl_mood_engine 情绪与好感度引擎** | 事件驱动状态机：好感度/心情/连续互动天数随你们的对话涨落——甜言与夸奖升温、冷淡与负面词降温、彻夜沉默会让她"想念"——并实时注入 LLM system prompt 影响语气 | v2 收集 122 项（test_mood.py）单元测试覆盖全部规则分支（含否定窗口/防刷分上限/时间衰减/注入防护等回归测试）；纯逻辑层与框架胶水层严格分离；状态按用户隔离、原子写持久化 |
| **vl_diary 她的日记** | 每晚定时把当天的对话写成她的第一人称日记，私聊发给你并存档 | 独立 LLM 请求不污染会话上下文；后处理链清洗模型输出格式 |
| **vl_eval 记忆评测** | 聊天里发 `/eval memory` 即可对长期记忆做 recall@1/@3 评测并出报告 | RAG 效果可量化——聊天里一条命令即可复现的 recall 报告 |

## 这也不是又一篇部署教程

除自研插件外，仓库还沉淀了三样教程给不了的东西：

1. **一份真实运行了几个月、经过脱敏的调校配置**（[cmd_config.example.json](deploy/cmd_config.example.json)）——限流防风控、真人感分段回复、「只有主人能聊」的白名单策略、语音容灾接线，每个参数的设计依据见[调校笔记](docs/调校笔记.md)
2. **跨四个项目的接线知识**（v1 编排时代沉淀，v2 迁移时即由此取证）——协议端配置持久化对抗匿名卷丢失、容器健康门控、容器访问宿主机语音服务的正确写法，这些坑官方文档各自为政，没人给全景
3. **防手滑的安全边界**——个人数据结构性隔离 + 提交前自动门禁钩子（密钥扫描/import 卫生/上游探针句三道）+ 端口默认仅本机

## 功能特性

- 💬 **QQ 原生对话** — QQ 协议端接入，私聊免唤醒词，短句分段连发像真人
- 🧠 **跨会话长期记忆** — 对话自动总结存入内嵌向量库（随新栈进程常驻，无需向量库容器），聊天时智能检索注入
- 🛡️ **云端故障不停摆**（智能降级，可选）— 云端 Key 欠费/鉴权失效这类不可重试故障连续两轮即自动黏性切到本地大脑（任意 GGUF 模型）保命，30 分钟半开探针探到云端恢复就提醒你切回（绝不擅自替你扣钱）；聊天里 `/brain` 一句话看大脑状态
- ⚡ **本地大脑一键换**（可选）— 控制台「引擎与人格」页一键切到本机 llama.cpp 跑的本地模型（任意 GGUF 均可，作者测试用 Qwen2.5-7B；对话数据不出门）；`一键启动.bat` 按配置自动带起，看护 30 秒探活、判死自动重拉、熔断冷却防雪崩
- 🔔 **主动消息** — 你不理她，她会在随机时刻主动来找你（可设免打扰时段）
- 💗 **情绪与好感度**（自研）— 她记得你们的关系温度：好感随互动涨落、心情随言语起伏，语气会跟着变
- 📖 **她的日记**（自研）— 每晚她把这一天写成第一人称日记发给你
- 📊 **记忆评测**（自研）— `/eval memory` 一键测出长期记忆的 recall@k
- 🖥️ **管理控制台**（自研）— 唯一管理入口：随她的进程内嵌运行（同起同停），登录后可填 API Key、改人格与 Embedding、QQ 扫码、启停备份；一个页面也看服务健康/最近事件（报错不再"全绿假象"）/她的内心状态/日记/记忆搜索/记忆评测报告（127.0.0.1:8787）
- 🎭 **自由人格** — 按[模板](deploy/persona-template.txt)或用 AI 生成器创建任何人格
- 🖼️ **表情包** — `&&标签&&` 自动配图，历史标签白名单校验、残缺标记自动清洗
- ✨ **灵魂质量门**（自研）— 主动消息质量门 + 出口统一文本清洗：正则碎片、emoji 乱码这类"念不出口/发不出手"的内容到不了你眼前；记忆入库自动查重、人设锚定防"他/她"记串
- 🗣️ **带情绪的语音**（自研）— 她的声音带着情绪——夸她时是傻白甜，惹她生气是疯批；情绪→音色情绪的映射表在控制台可改、可逐条试听，同一句话重复问秒回（语音缓存），语音后端由框架自动拉起自愈（首次开口约 15 秒自动就绪）
- 🔒 **数据全本地** — 一切落在 `local/` 目录，备份搬家一条命令

## 一条消息的生命周期（v2 新栈）

```mermaid
sequenceDiagram
    participant Q as 你的QQ
    participant N as 协议端容器(器官①)
    participant V as virtuallover(单进程大脑)
    participant L as LLM API
    participant T as 语音引擎(voice-engine·器官②·可选)
    Q->>N: 私聊一句话
    N->>V: 反向WS推送(OneBot v11, 127.0.0.1:6199, Bearer token)
    V->>V: 白名单门禁/限流检查
    V->>V: 内嵌向量库检索相关记忆(top-k)
    V->>L: 人格system + 记忆prepend + 消息
    L-->>V: 回复文本
    V->>V: 按标点切分,随机1.5~3.5s间隔排队
    V->>T: TTS合成(可选,失败回退纯文字)
    V->>V: &&表情标签&&匹配(白名单校验)
    V->>N: 构造数组段逐条发送(文字/语音/表情)
    N-->>Q: 像真人一样连发
    V->>V: 每轮写入历史库; 计数达阈值后台总结存入长期记忆
```

## 系统架构（v2）

| 组件 | 访问 | 作用 |
|---|---|---|
| virtuallover 新栈 | 宿主机进程（`python -m virtuallover`，`.venv` 隔离） | 大脑：OneBot 桥、人格装配、LLM、内嵌记忆、情绪/日记/主动消息/表情包 |
| 控制台 | [127.0.0.1:8787](http://127.0.0.1:8787) | 唯一管理入口：登录后可填 API Key、改人格、QQ 扫码、启停备份；也一页看服务健康/内心状态/日记/评测报告 |
| 协议端（容器 vl-heart） | 127.0.0.1:61990（由控制台代管，无需打开；中性端口，宿主侧 61990→容器 6099） | 器官① QQ 协议端：消息出入口 |
| 语音引擎 voice-engine（可选，上游 GPT-SoVITS 推理包） | 宿主机 :5000 | 器官② 语音合成推理 |
| legacy 组（AstrBot + Milvus/etcd/minio 三容器） | 仅回退模式运行 | v1 编排整体保留在 compose `legacy` profile，供 14 天回退窗口 |

默认所有端口仅本机可访问（127.0.0.1 绑定），6199 反向 WS 只接受本机协议端反连——公共 Wi-Fi 环境可放心运行。语音引擎 voice-engine 由框架经自有 wrapper 拉起，只绑 `127.0.0.1:5000` 回环（详见 [语音指南](docs/语音.md) 第 5 节）；你手动直接运行上游脚本则仍监听 `0.0.0.0` 且默认无鉴权，公共网络环境建议交给框架拉起或在控制台停掉语音后端。详见 [部署详解](docs/部署详解.md) 的「网络暴露模型」。

## 快速开始（v2 · 三步）

### 前置要求

1. **Windows 10/11 64 位** + [Docker Desktop](https://www.docker.com/products/docker-desktop/)（Linux/Mac 见[部署详解](docs/部署详解.md)）
2. 一个**QQ 小号**当机器人（切勿用大号，有风控风险）
3. 一个大模型 API Key——安装时选服务商（火山方舟豆包 / DeepSeek / 任意 OpenAI 兼容），向导按选择自动写端点与模型预设。长期记忆另需一个 Embedding 模型（可选：没有她也能聊，只是记不住说过的话；方舟 Key 可直接用，其他家的按提示填）

### 安装（3 步）

1. **双击 `setup.bat`** — 向导交互两分钟：粘贴 Key、选服务商、填机器人 QQ 号与你的常用 QQ 号（主人号可选，回车跳过＝小号兼当主人；Embedding 单独询问，可跳过＝记忆暂闭）。向导自动完成 Docker 检查、协议端容器启动、`.venv` 与依赖安装、配置生成与写回（会话/门禁按主人号写，机器人号只管扫码）、**协议端反向 WS 自动接线**（无需手工配置 WebUI），最后拉起她并等待控制台就绪
2. **控制台扫码** — 打开控制台 [http://127.0.0.1:8787](http://127.0.0.1:8787)（密码见 `local/console.json`），在「QQ 登录」页用**小号**扫码；Key、人格、embedding 都能在控制台配置页核对修改
3. **说句话** — 用常用号加小号为好友，发条消息——她就活了

日常启停用 `一键启动.bat` / `一键停止.bat`；想让她在本地显卡上离线说话，在控制台「引擎与人格」页点「切到本地大脑」（任意 GGUF 模型均可，作者测试用 Qwen2.5-7B）。语音训练与接入见 [语音指南](docs/语音.md)。默认人格是「小语」，可在控制台「引擎与人格」页或 `local/config/vl.toml` 的 `[persona]` 段替换（模板见 [persona-template.txt](deploy/persona-template.txt)）。想手工放一份配置再微调：复制 [vl.toml.example](deploy/config-samples/vl.toml.example) 为 `local/config/vl.toml`（与配置树逐键对拍，见 `virtuallover/tests/test_config_example.py`）。

### 日常使用

| 双击脚本 | 作用 |
|---|---|
| `一键启动.bat` | 启动全部（语音后端由新栈进程 auto_start 自动拉起，不再弹窗询问） |
| `一键停止.bat` | 停止全部（数据保留） |
| `一键状态.bat` | 容器/服务/QQ 登录一屏体检 |
| `scripts/backup.bat` | 备份聊天、新栈数据（vl_data）、登录数据（升级前必跑） |
| `scripts/update.bat` | 安全升级（自动先备份，带回滚指引） |
| `scripts/check-secrets.bat` | 发布/提交前扫描密钥泄漏 |
| `http://127.0.0.1:8787` | 控制台（唯一管理入口）：状态一览与全部管理操作 |

聊天中可用命令见[速查表](docs/聊天命令速查.md)；问题先查 [FAQ](docs/常见问题.md)。

### 开机自启（可选）

注册 Windows 计划任务：登录即自动拉起全家（协议端 + 本地大脑(如已配置) + vl + 看护）。以下命令在 cmd 里执行（当前用户登录触发，无需管理员；`<你的仓库目录>` 换成你的仓库根，路径含空格时内部路径用 `\"` 转义）：

```bat
:: 注册（ONLOGON 调 service.py start；输出落 local/vl_data/logs/autostart.log）
schtasks /Create /F /TN "VirtualLover自启" /SC ONLOGON /TR "cmd /c cd /d <你的仓库目录> && .venv\Scripts\python.exe scripts\service.py start >> local\vl_data\logs\autostart.log 2>&1"

:: 撤销
schtasks /Delete /F /TN "VirtualLover自启"
```

- 未跑过 `setup.bat`（无 `.venv`）时，把命令里的 `.venv\Scripts\python.exe` 换成你机器上的 `python.exe` 全路径；
- 登录后会短暂弹出控制台窗口，`start` 跑完自动关闭；Docker 未就绪时协议端容器启动会自动重试（3 次 × 间隔 30s）；
- `service.py start` 自带看护：vl 崩溃自动重启、本地大脑(8002) 判死自动重拉（详见 [部署详解](docs/部署详解.md)）。

> 📐 想知道这些参数为什么这么调（限流、随机分段、白名单策略的完整设计依据）？看[调校笔记](docs/调校笔记.md)。

## 目录结构

```
├── setup.bat / 一键启动·停止·状态.bat    安装与日常（本地大脑切换在控制台「引擎与人格」页）
├── virtuallover/                        ★ v2 新栈：她的全部（桥/大脑/记忆/灵魂/控制台，含单元测试）
├── docker-compose.yml                   协议端容器（vl-heart）常驻编排 + AstrBot/Milvus legacy 回退组（最小端口暴露）
├── deploy/                              人格模板、插件清单、脱敏配置模板与示例
├── docs/                                部署详解、语音、FAQ、命令速查、回退 SOP、模块来源表（provenance）
├── scripts/                             备份、升级、密钥自查、整程序服务编排（service.py）
├── tools/                               历史库/记忆库清理启动器（R3，薄封装包内实现）
└── local/                               ★ 你的全部私人数据（git 忽略）
```

> v1 形态的 `plugins/`（自研插件）与 `workbench/`（v1 控制台）已随 v2 验收退役删除
> （功能分别并入 `virtuallover/soul|memory` 与 `virtuallover/console`，见 provenance）。

## 参与开发

克隆后请启用提交前密钥扫描钩子（一次性，防止误提交密钥）：

```bash
git config core.hooksPath .githooks
```

推送/发布前自查两件事：① 运行 `scripts/check-secrets.bat`（扫描暂存区密钥，含 GBK 脚本的中文模式）；② 运行 `git status --ignored` 确认没有新的未跟踪文件混入（个人数据都在被忽略的 `local/`）。

## 免责声明

- NapCat 为非官方 QQ 协议实现，存在账号风控风险：**务必使用小号**，频率适度，风险自担
- 语音克隆请仅使用你有权使用的音色，训练产物请勿公开分发
- **v1 与 v2 的来源声明**：v1 基于 AstrBot 编排（仅引用其官方镜像，未修改、未分发其代码）；v2 为**独立重实现**——以行为规格先行 + 运行期黑盒观察的 clean-room 方式自行撰写全部代码，未复制 AstrBot 或其 AGPL 插件的任何源码，逐模块来源标注见 [provenance](docs/provenance.md)；各上游组件著作权归其作者（见下）
- **本项目仅供个人学习与单人陪伴场景使用**，严禁用于情感诈骗、冒充真人、诱导转账、批量养号等任何违法或伤害他人的用途，使用者自行承担全部法律责任；人格模板中「不自称 AI」的设定仅用于伴侣拟真体验，不构成向第三方隐瞒 AI 身份的授权

## 致谢与开源组件

v1 时代本仓库以编排形态使用下列上游项目（仅引用官方镜像，未修改、未分发其代码）；v2 为独立重实现、未复制其代码（逐模块来源标注见 [docs/provenance.md](docs/provenance.md)）。各组件著作权与许可归其作者：

| 组件 | 许可证 | 备注 |
|---|---|---|
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | AGPL-3.0 | v1 大脑；v2 起由自研新栈替代，仅保留于 compose legacy 回退组 |
| [NapCat](https://github.com/NapNeko/NapCatQQ) | **自定义限制许可（非开源，禁止商业用途）** | QQ 协议端（v1/v2 均为器官①）；本项目仅引用其官方镜像 |
| [Milvus](https://github.com/milvus-io/milvus) / [etcd](https://github.com/etcd-io/etcd) | Apache-2.0 | v1 向量数据库 / 元数据；v2 记忆改为内嵌向量库，仅 legacy 回退组使用 |
| [MinIO](https://github.com/minio/minio) | AGPL-3.0 | v1 对象存储；仅 legacy 回退组使用 |
| [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) / GSVI 推理包 | MIT | 语音合成（器官②，MIT；v2 的 `voice/` 仅按其 HTTP 契约调用） |
| 各插件 | 见各自仓库 | 作者清单见 [plugin-list.md](deploy/plugin-list.md)；v2 对其行为均为 clean-room 重实现（见 provenance） |

> ⚠️ 特别提醒：NapCat 的许可证**明确禁止商业用途**，请勿将本方案用于商业场景。

## License

[MIT](LICENSE)（本仓库的编排、脚本与文档；上述各组件遵循其各自的开源协议）
