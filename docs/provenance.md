# 模块来源表（provenance）

> 本表是 SPEC §14（AGPL 合规规约；AGPL 是一类强互惠的开源许可证，衍生作品须继续以同
> 许可证开源）要求的来源标注：virtuallover/ 下每个模块
> 必须标注 own / port-of-own / clean-room-of-X 三种来源之一，**无标注不得合入**。
>
> 三种标注的含义：
> - **own** —— 本项目原创：从零撰写，不存在被模仿的上游实现；
> - **port-of-own** —— 从本项目自有代码搬入改造（plugins/vl_mood_engine、vl_diary、
>   vl_eval 与 workbench/ 均为本项目原创，曾以插件/工作台目录随本仓库开源；v2 并入后
>   已整目录退役删除，历史形态见 git 历史，搬入不引入任何第三方著作权）；
> - **clean-room-of-X** —— 清洁室重实现：行为规格先行（SPEC 对应章节，含对现网行为的
>   黑盒实测），实现阶段**未阅读、未复制 X 的任何源码**；X 仅作为行为规格的事实来源。
>   其中对 AGPL 上游（AstrBot、astrbot_plugin_mnemosyne、astrbot_plugin_proactive_chat）
>   只允许"运行期黑盒观察 + 配置/数据库实测"；对开放规范（OneBot v11）直接以规范文本为准。
>
> 敏感数据规约（本表随附）：QQ 号、token、API key 一律不出现在代码/测试/注释中，
> 真值仅由 virtuallover/tools/config_migrate.py 从 local/ 读取。

## virtuallover/（按 SPEC §2 目录逐模块）

| 模块 | 来源标注 | 依据与说明 |
|---|---|---|
| `SPEC.md` | own | 主控起草 + 十参谋（协议/LLM/记忆/数据/灵魂/安全/部署/许可证/测试/产品）审查定稿 |
| `__init__.py` | own | 包声明 |
| `__main__.py` | own | 入口装配 + 守护运行 + 事件流/调度接线（对话主管线、EVENT_SCHEDULED 驱动 proactive/diary、--smoke 就绪探测），SPEC §1/§2/§8 定义 |
| `config.py` | own | 读 local/config/vl.toml（stdlib tomllib）+ 启动校验 + 自带 TOML 序列化器（tomllib 只读不写，dump_config 供迁移脚本与 token 回写复用），SPEC §11.1 产物的加载/校验端；[memes]/[console] 节与 storage.memory_db 为集成期增补 |
| `events.py` | own | asyncio 事件总线，SPEC §2 定义 |
| `bridge/onebot_server.py` | clean-room-of-botuniverse/onebot-11 | SPEC §3 九条契约；唯一参考为 OneBot v11 开放规范文本 + NapCat 运行期黑盒观察（SPEC §14） |
| `bridge/textutil.py` | clean-room-of-botuniverse/onebot-11 | CQ 码文本格式出自 OneBot v11 规范；只做单向降级，不写 CQ 构造器 |
| `brain/__init__.py` | own | 包声明 |
| `brain/llm.py` | clean-room-of-AstrBot | SPEC §4：chat/embedding 双客户端行为对齐旧栈实测（重试/超时/think 剥离/双形态 content）；未读 AstrBot 源码 |
| `brain/persona.py` | clean-room-of-AstrBot | SPEC §5：装配管道顺序与 user_prompt prepend 均为旧行为黑盒实测结论；truncate（0.82 丢最旧 1 turn，不做摘要）也在此模块 |
| `brain/history.py` | clean-room-of-AstrBot | SPEC §6：会话存储与双写镜像；SQLite schema 为本项目自定设计 |
| `brain/sender.py` | clean-room-of-AstrBot | SPEC §5 发送侧行为：分段（阈值/正则参数实测自 proactive_chat 配置）、log 分布间隔、10 条/30s stall 限流；未读其源码 |
| `brain/pipeline.py` | own | O3/O4 性能轮引入（b477774）、R2/R3 持续扩展：消息管线——同步入队 → per-session 串行通道 → per-user 打断地平线（IDLE/RUNNING/DRIP）→ 发送 FIFO 统一收编主回复/表情/主动/日记；characterization 契约固化于 tests/_o34_contract.py |
| `brain/router.py` | own | 五轮修缮 R2 核心：LLM 主备智能降级路由——瞬态故障（5xx/超时）per-request fallback、计费/鉴权失败（401/403）连续 2 轮黏性本地保命、30min 半开探针只提示不自动切回（计费裁决留人）；本地 llama-server adopt-or-start（绝不杀外部实例）+ 4096 token 预算截断。行为规格为本项目自定（状态机分寸以 voice/lifecycle 为金样板），无上游；tests/test_brain_router.py + test_brain_health.py |
| `memory/store.py` | clean-room-of-astrbot_plugin_mnemosyne | SPEC §7.1-7.3：schema/检索/注入格式来自现网数据库与行为实测；总结 prompt **自行撰写**（SPEC §7.2 禁抄原文，pre-commit 探针门禁把关） |
| `memory/backfill.py` | clean-room-of-astrbot_plugin_mnemosyne | SPEC §7.4：分窗总结→embedding→入库，幂等重跑 |
| `memory/benchmark.py` | **port-of-own**（`plugins/vl_eval/benchmark.py`） | SPEC §8.5/§7.4：逐行并入、除 docstring 外零逻辑改动；/eval memory 经 `__main__._build_eval_runner` 以本模块 + 进程内向量库装配；其 22 项纯逻辑测试随 O11① 迁至 tests/test_memory_benchmark.py（import 改新家，断言原样） |
| `memory/service.py` | own | SPEC §7.2 写入触发服务的实现本体：计数达阈值触发后台总结（prompt 自行撰写，探针门禁把关）、失败计数/缓冲持久化待重试、/remember 直通；R3 记忆质量三补丁——人设锚（summary system 按 persona_id 参数化，防「恋人性别」漂移）、弃权出口（无内容窗口可答「不值得记住」清缓冲不入库）、入库查重（近邻 cos>0.85 且关键词重叠高跳过）；tests/test_memory_quality.py |
| `soul/textclean.py` | own | 五轮修缮 R3：共享文本清洗原语——`clean_proactive_text`（主动消息出口）与 `clean_tts_text`（语音合成前）单一来源；纯函数、零 IO、只剥除不改写正文语义（不剥句末标点，作 TTS 韵脚锚）；tests/test_voice_polish.py 等 |
| `soul/mood.py` | **port-of-own**（`plugins/vl_mood_engine` 的 engine.py+storage.py） | SPEC §8.1：原样并入、零逻辑改动；命令面等价（/mood[/stats\|/reset]） |
| `soul/diary.py` | **port-of-own**（`plugins/vl_diary`） | SPEC §8.2：writer 并入；LLM 调用改走 brain.llm；注入防护原样保留 |
| `soul/proactive.py` | clean-room-of-astrbot_plugin_proactive_chat | SPEC §8.3：8 条主动行为全部有现网行号级黑盒证据；未读其源码 |
| `soul/memes.py` | clean-room-of-astrbot_plugin_meme_manager | SPEC §8.4 + SPEC §14：meme_manager 为 **MIT** 许可，仍**不搬代码**只实现行为（&&tag&& 匹配/白名单/残缺清洗）；如将来搬代码须保留其版权行 |
| `commands.py` | own（/eval 段 **port-of-own** `plugins/vl_eval`） | SPEC §8.5：/mood /diary 命令面对应 port 模块；/eval memory 评测面来自 vl_eval 并入；R2 增补 /brain [status\|local\|cloud] 管理员大脑降级观测与手动切换 |
| `net.py` | own | 五轮修缮 R2 集成修复：smart_urlopen 本机端点感知——回环/私网/链路本地（RFC1918+127/8+link-local）目标禁代理直连（实证：系统代理把发往本地大脑 127.0.0.1:8002 的请求劫持回 404），公网保持系统默认（云端用户可能依赖代理出网，不替人做主）；共享单例 opener |
| `voice/gptsovits.py` | clean-room-of-GSVI 推理包（gsv_tts_selfhost） | SPEC §9：GET /tts 契约来自其运行期 HTTP 黑盒观察（参数名/别名/超时实测） |
| `voice/manager.py` | own | VOICE_SPEC §2：语音管线门面（每用户有序队列后台 worker、一段一语音、dual_output 双轨、trigger_probability 整体抽样、waiting/degraded 降级）；消费 voice.engine 的 synthesize(text, mood_snapshot) 接缝；文字路径零改动、绝不 await 合成；R3-2c 起首段出帧后语音即并行入队、发送失败由 drop_queued 收走排队作业 |
| `voice/engine.py` | own | VOICE_SPEC §1 引擎栈组合体（dfd1888 引入）：组合 gsvi/lifecycle/cache/mapping 并持 /tts 全局单飞锁（串行化权威实现点），挂 ctx.voice_engine、转发控制台契约到 voice_manager 门面 |
| `voice/gsvi.py` | own | VOICE_SPEC §1：GSVI HTTP 客户端 v2（自 SPEC §9 客户端改造）——GET /tts 契约沿实测裁定（别名成对/3 次/60s/RIFF 校验）；character_list 只拉一次缓存（服务端每调重写 character_info.json，禁轮询）；探活只用 TCP connect |
| `voice/lifecycle.py` | own | VOICE_SPEC §1：GSVI 进程 adopt-first 状态机（收编优先，绝不杀外部实例）——30s 周期探测、连续 2 次判死、10min 内 3 次死亡熔断 degraded、5min 冷却后半开重试；五轮修缮 R2 的 service.py 大脑看护 即此参数面的脚本侧移植 |
| `voice/mapping.py` | own | VOICE_SPEC §1：情绪→音色注册情绪映射（纯 7 态表 + 回退链：映射值∉注册表→配置默认→default，每跳 degraded_emotion 告警——GSVI 对未注册情绪静默忽略，必须客户端拦）；R3 按听感实测重排默认表（常态 calm 配「聊天」，疯批只留给生气） |
| `voice/cache.py` | own | VOICE_SPEC §1：语音产物磁盘缓存——键=sha1(排序后全部请求参数)，文本归一化与 GSVI 服务端逐字同链（键的等价类=服务端行为等价类）；双上限 LRU（500 条/128MB，内存 OrderedDict+落盘 mtime 双序） |
| `console/` | **port-of-own**（`workbench/` v2）+ 删减 | SPEC §10：整体移植；**删除 AstrBot 代理段**（engine.py 的 ab_* 全部与 console.json 的 astrbot_password 托管）；保留 NapCat QR 代理（nc_*）与三重门（会话认证 + X-Requested-With CSRF + 127.0.0.1 绑定）；/api/status 改进程内探测（core.vl_status）；新增 napcat 指向切换/回退动作（调 tools/config_migrate 公共改写函数）；前端仅 abCheckStatus 一处降级补丁 |
| `console/config_page.py` | own | 五轮修缮 R1：控制台配置页逻辑层（GET/PUT /api/config/{chat,embedding,persona}）——load 校验→内存改→dump 序列化→.bak-<时间戳> 备份→tmp+os.replace 原子替换，任一步失败写盘前中止；api_key 只回显 `{"len": N}`、PUT 缺省=保持现值（拒空串清空）；无热加载故响应明示 need_restart；tests/test_console_config_page.py |
| `console/recent.py` | own | 五轮修缮 R2：最近事件环形缓冲——进程内 logging.Handler（ERROR/WARNING+关键 INFO 里程碑，deque 50 条、异常栈拍平单行 240 字截断）+ vl.log 尾部同构解析兜底（跨重启），/api/recent-errors；修「ark 欠费 403 她哑 8 小时、控制台却全绿」的可见性缺口；tests/test_recent_errors.py |
| `console/voice.py` | own | VOICE_SPEC §3（dfd1888 引入、R1/R3 扩展）：语音页后端逻辑层——状态卡（lifecycle 优先，冷启 15s 禁裸探针上卡）/试听/启停/映射表读写/base_url 一键修正；语音本体一律走 voice_manager（绝不自建缓存或第二把合成锁），严禁向用户可控地址代理请求；R1 起其 fix_base_url 是全部配置页写入纪律的金样板 |
| `tools/config_migrate.py` | own | SPEC §11.1：读旧配置生成 vl.toml，报告只记密钥长度；集成期新增公共改写函数 switch_napcat_target / rollback_napcat_target / set_bridge_token（console 的指向切换/回退动作复用） |
| `tools/history_migrate.py` | own | SPEC §11.2：data_v4.db → history.db，120 条守恒断言、注入脚手架清理 |
| `tools/milvus_check.py` | own | SPEC §11.3：空库证明脚本（≤20 行），删除三容器的前置条件存档；O10 收尾：docstring 改 override 取证流程（三件套 profile 临时拉起取证后停回） |
| `tools/init_config.py` | own | PERF_SPEC §4-O12：全新安装初始化（--init-config）——生成可启动的 vl.toml 模板（必填占位齐备 + ensure_bridge_token 复用落盘），解决新装无旧栈可迁的鸡生蛋；默认人格为原创自写 |
| `tools/setup_writeback.py` | own | PERF_SPEC §7.1-7 ①：setup.bat v2 交互收集值（API Key/QQ 号）经环境变量写回 vl.toml 三处同步；Key 不回显、QQ 打码；R1 扩为全新安装闭环——provider/embedding 预设写回 + napcat 反向 WS 代写/改写（ensure_napcat_reverse_ws，缺则按 QQ 代写 onebot11_<QQ>.json，有则 switch_napcat_target 改写）；tests/test_setup_writeback.py |
| `tools/memory_clean.py` | own | 五轮修缮 R3：记忆存量性别漂移修正（演练只打印 diff，--apply 备份后写库，幂等可复跑）；仓库根 tools/memory_clean.py 为薄启动器 |
| `tests/` | own | SPEC §12：契约九条 + 行为断言 + import 卫生双门禁（含 test_smoke 冒烟固化、test_console_port 移植适配）；O11⑤ 起 workbench 活断言迁入（test_auth 17 / NapCatProxy 9 / Compose+Backup 10 / AuthGate 矩阵，见 test_console_workbench.py） |
| 仓库根 `conftest.py` | own（**已退役**，O11③） | 统一测试入口的收集协调（importlib 模式 + 逐目录 sys.path/顶层 conftest 绑定）只服务于 plugins/*/tests 与 workbench/tests 的同名收集冲突——两者整删后实测安全删除（686 passed 口径验证），conftest 唯一入口为 virtuallover/tests/conftest.py |

## 仓库其它自有面（port-of-own 的搬入源头，随本仓库 MIT 开源）

| 目录 | 来源标注 | 说明 |
|---|---|---|
| `plugins/vl_mood_engine` | own（**已退役**，O11②） | 情绪与好感度引擎（156 项单元测试的主体（v1 插件历史口径；v2 现量见 README））；v2 并入 soul/mood.py；目录整删，历史形态见 git |
| `plugins/vl_diary` | own（**已退役**，O11②） | 她的日记；v2 并入 soul/diary.py；目录整删 |
| `plugins/vl_eval` | own（**已退役**，O11②） | 记忆评测；v2 并入 commands.py /eval 段与 memory/benchmark.py；目录整删 |
| `workbench/` | own（**已退役**，O11⑤） | 管理控制台 v1（本地只读工作台）；v2 整体移植为 console/（删 AstrBot 代理段）；活断言迁 virtuallover/tests 后整删——删除前提（--rollback-napcat CLI + runbook docs/回退.md 演练）均已满足 |
| `requirements-dev.txt` | own | O11④/§7.2：测试依赖与运行依赖分账（pytest/pytest-xdist 版本锁定 + 门禁入口注释），setup .venv 安装 |
| `docker-compose.yml` / `setup.bat` / `一键*.bat` / `scripts/` | own | 编排与运维脚本。五轮修缮期守护扩展：`scripts/service.py` 扩为整程序服务编排 + 双看护（vl 10s 巡检拉起；llama-server(8002) 大脑看护 状态机——30s 探测/连续 2 次判死重拉/10min 内 3 次死亡熔断/5min 冷却半开；重启退避按「最近死亡时刻」窗口、连续存活 5min 清零；pid 击杀一律过命令行核验门防 pid 复用误杀；napcat 启动 Docker 未就绪 3 次×30s 重试）；`scripts/brain_switch.py` 本地/云端大脑一键切换（ensure_gguf 转换量化、chat_probe 真对话验证、restart_vl 看护在岗时绝不双拉）；tests/test_service_guard.py 等 |
| `tools/`（仓库根：`memory_clean.py` / `history_clean.py`） | own | 五轮修缮 R3 双清理启动器（薄封装、零业务逻辑）：memory_clean 记忆性别漂移修正（实现本体在 virtuallover/tools/）；history_clean 历史 bug 产物清理（主动消息毒循环行/`(?:` 碎片行/function-call 泄漏行——仅 role=assistant 参与匹配、默认 dry-run、删除前整库备份、幂等可复跑） |
| `docs/`（本文件除外） | own | 部署详解、调校笔记、语音、FAQ、命令速查 |
| `deploy/` | own | 人格模板、插件清单、脱敏配置模板 |

## 非代码资产与上游边界

| 项 | 标注 | 说明 |
|---|---|---|
| NapCat | 外部器官（仅引用官方镜像） | **自定义限制许可（非开源，禁止商业用途）**；本项目不分发其代码 |
| AstrBot | legacy 回退组镜像（AGPL-3.0） | v1 编排时代的大脑；v2 已由本表所列自有模块整体替代 |
| Milvus / etcd / MinIO | legacy 回退组镜像 | Apache-2.0 / AGPL-3.0；v2 记忆为内嵌向量库，不再依赖 |
| GPT-SoVITS / GSVI | 外部器官（MIT） | 宿主机可选进程；voice/ 只按其 HTTP 契约调用 |

## 维护规约

1. 新增任何模块前先在本表登记来源标注，`无标注不得合入`（SPEC §14）。
2. `clean-room-of-X` 模块在实现期间禁止阅读 X 的源码/容器内文件；行为问题一律回到
   SPEC 对应章节或运行期黑盒观察取证。
3. pre-commit 门禁（`.githooks/pre-commit`）自动执行：virtuallover/ 无
   `from astrbot|import astrbot` 引用、无 SPEC §12 探针句命中。探针词表存于
   `local/agpl-probes.txt`（本机 gitignored，不入版本库——AGPL 上游 prompt 文案
   写入 git 跟踪文件即构成复制分发），只做本机门禁；tracked 的
   `.githooks/probe-patterns.txt` 仅作机制说明与兜底。
