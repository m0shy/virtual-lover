# virtuallover/voice v2 语音框架规格（我整合四参谋定稿）

> 依据：我的提案 + 四参谋（语音引擎/管线整合/质量测试/产品控制台）审查 + 我的终裁。
> 「四参谋」「XX 参谋」是起草期的内部说法，指按方向并行审查产出的报告（未随仓库发布）；
> 文中 R 编号是内部修缮轮次编号（定义见 PERF_SPEC）。
> 所有行为参数均出自实测（行号级证据见参谋报告，未随仓库发布）。

## 0. 终裁记录（分歧处理）

| 争点 | 分歧 | 终裁 |
|---|---|---|
| 默认音色 | 现网 Hutao 单情绪→联动哑火 vs 不擅自换声音身份 | **默认切 Huohua**（7 情绪承载联动；Huohua 是用户自备的多情绪音色；控制台一键切回 Hutao；报告向用户明示声音变化） |
| 情绪映射结构 | 两档门控 / 基础+强档两列 / 纯 7 态 | **纯 7 态表**（简单可维护；intensity 仅经衰减语义参与：<0.15→calm） |
| sad 映射 | 慢速病娇 / 平静的 / 平常的 | 慢速病娇（多数；表可配置控制台可改） |
| chunking | 独立模块 / 保险丝 / 砍 | **>120 字保险丝**（仅无标点长文触发；主路径=sender 既有分段一段一语音） |
| edge_tts 兜底 | 留实验位 / 砍 | **砍出 v1**（四参谋一致） |
| 就绪探测 | TCP connect / character_list | **TCP connect 1s 间隔**（character_list 每调写盘 character_info.json，禁轮询；就绪后拉一次注册表缓存） |

## 1. 模块与契约

```
virtuallover/voice/
├── manager.py     # VoiceManager：synthesize(text, mood_snapshot)->音频|None；全局 asyncio.Lock 串行
├── gsvi.py        # 改造：+character_list()、探测、注册表缓存
├── lifecycle.py   # 状态机 not_started→starting→ready→suspected_down→restarting|degraded
├── cache.py       # 键=sha1(排序后全部请求参数)；归一化 text（……→。、\n→。\n）
└── mapping.py     # mood 有效状态→emotion（客户端校验，回退链打点）
```

- **生命周期**：adopt-first（端口应答即收编 externally_owned，绝不杀外部实例）；auto_start 拉起
  Popen argv 固定 `[<GSVI>/runtime/python.exe, Inference/src/tts_backend.py]`，cwd=GSVI 根
  （错 cwd 会静默降级 classic 模式）、CREATE_NO_WINDOW、stdout/stderr→local/vl_data/logs/voice-engine.log
  （轮转）；就绪预算 300s（冷启可能 1-2 分钟，15s 是热启）；崩溃检测 30s 周期、连续 2 次判死、
  10 分钟 3 次熔断 degraded；冷却 ≥5 分钟；cleanup 终止自有进程；PID 落
  local/vl_data/voice/voice-engine.pid，杀前命令行核验（防误杀）。
- **情绪映射默认表**（vl.toml 可改）：happy→傻白甜、excited→疯批、angry→疯批、sad→慢速病娇、
  lonely→平常的、shy→聊天、calm→平静的、fallback→聊天。输入用 MoodEngine 有效状态
  （τ=4h 衰减+<0.15→calm，与文字注入同源——给 mood.py 加结构化 getter，禁止解析注入字符串）。
  回退链：映射值∉注册表→配置默认情绪→"default"，每跳打点 degraded_emotion。
- **缓存**：键含 character|emotion|text_language|format|归一化 text；条数 500 + 字节 128MB 双上限
  LRU；写盘 tmp+os.replace 原子；读回 RIFF 魔数校验坏条目当 miss；启动扫描重建索引；
  目录 local/vl_data/voice_cache（**从两处备份清单排除**：console/control.py BACKUP_SOURCE_DIRS
  粒度调整 + scripts/backup.bat 排除——可再生数据不进备份）。
- **串行化**：全局单飞锁——任一时刻至多一个 in-flight /tts（聊天与试听共用）；契约测试断言
  请求时间窗零重叠。

## 2. 管线接入

- 挂点：sender 分段后一段一语音；文字路径零改动、绝不 await 合成；语音走每用户有序
  asyncio.Queue 后台 worker，段间间隔复用 sample_gap，发送前过共享限流（10 条/30s）。
- dual_output 双轨语义逐字还原：主回复 always_send_text=true（文字必发+语音附加）；
  proactive false（成功只语音、失败回退文字，对齐 proactive.py 现有实现）。
- trigger_probability 按回复整体抽样一次（种子 rng，DEBUG 记录）。
- mood 快照取 persona 装配时刻同一有效状态（一次快照贯穿文字注入与语音映射）。
- 引擎 waiting/down 期间：该条降级纯文字，后台继续等；degraded 冷却期跳过重试直接 None。

## 3. 控制台语音页（第 7 页）

- 状态卡：lifecycle 状态优先（非裸 HTTP 探针——冷启动 15s 内 HTTP 必误报）；当前音色+注册情绪
  chips；缓存条数/容量；PID+运行时长。
- 试听卡：文本 ≤200 字 + 情绪下拉（**只列 /character_list 注册值**）+ 合成播放
  （POST /api/voice/preview → base64 JSON，产物 ≤8MB，Accept-Ranges: none；共用缓存与串行锁）。
- 映射表卡：7 行下拉（限注册情绪，未命中标黄"联动不会生效"）+ 行内试听 + 结构化安全写
  （校验+.bak+原子替换+热加载，参照 napcat_switch 模式）。
- 启停卡：owned 才可停；adopted 显示"由外部窗口启动"；GSVI 日志路径+最近错误行。
- 横幅：base_url 非 127.0.0.1 告警+一键修正（host.docker.internal 残留必炸）；单情绪音色明示。
- 安全：三重门全继承（会话+CSRF+127.0.0.1）；严禁任意 URL 代理。

## 4. 测试（并入单一 pytest 入口，基线不回退）

- 前置：**修测试基线隔离**（6199 端口参数化/占坑检测——在跑进程占口曾致 5 failed）。
- fake GSVI 契约：三怪癖保真（string 参数 .lower()；未注册情绪静默 200+wav——反向断言非法
  情绪永不出网；未注册角色 400 JSON）+ OK/HANG/503/NON_WAV/CONN_RESET 模式 + 请求记录。
- 串行化断言（最高优先）：fake 记录起止时间，并发调用零重叠。
- 缓存：命中单请求字节相等、键含情绪、LRU 注入时钟、双上限淘汰、坏魔数当 miss。
- lifecycle 状态机（fake spawner+假钟）：全迁移+熔断+只杀自有 PID+cmdline 核验。
- 降级两型（拒连秒败 + HANG 3×60s）：断言文字先于语音完成、用户侧零异常、恢复自动回语音；
  dual_output 两口径。
- 控制台三重门（401/403/413）。
- 真实 E2E 手工验收单：冷启就绪时长、20 句 p50/p95、缓存命中时延、7 态参数日志各就位、
  **真 NapCat 收 wav record 在 QQ 客户端确认可播**（无证据不得默认能播）、data_v4.db 真实
  历史回放命中率（诚实报告，预期个位数%）。

## 5. 部署与文档

- 一键启动.bat：删 choice 语音问句与 start GSVI-TTS 窗口（auto_start 框架唯一所有）；
  文案"加载 1-2 分钟"改"首次开口约 15 秒自动就绪"（冷启 2 分钟内）。
- 一键停止.bat：增 GSVI 停止段（pid+命令行核验），删"请直接关闭那个窗口"。
- README 卖点升级："她的声音带着情绪——夸她时是傻白甜，惹她生气是疯批；映射表控制台可改可试听"。
- docs/语音.md §3 重写（旧 host.docker.internal 指引是把用户带进死路的文档债）+ §5 补
  "auto_start 常驻+0.0.0.0 无鉴权"防火墙警示（启动日志也告警一句）。
- vl.toml：tts.character 默认 Huohua；base_url 归一化 127.0.0.1:5000。

## 6. "比老框架好"的验收指标（留档）

1. 可用率：冷启动后首条语音自动送达（老栈手动启动≈常年 0）
2. 情绪多样性：实际使用 emotion 种类（老栈恒 1）
3. 重复文本延迟：缓存命中 <100ms（老栈 2-9s 且每次不同演绎）
4. 首字延迟：引擎 HANG 时文字回复仍 <1s
5. 自愈：kill GSVI 后 ≤60s 自动恢复（老栈死了就一直死）
6. 进程卫生：一键停止后残留 0（老栈靠手关窗口）
