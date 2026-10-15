# Virtual-lover 全面优化规格 v1（我整合十参谋 R1 终裁）

> 依据：84 条侦察/搜寻事实 + 十参谋 R1（多参谋亲测复测互相修正）+ 我的终裁。
> 「十参谋」「XX 参谋」是起草期的内部说法，指按方向并行审查产出的报告（未随仓库发布）；
> 文中 O1-O14 是本轮性能任务的内部任务编号，R1-R5 是其后各修缮轮的轮次编号。
> 本规格是本轮实施的唯一依据；与本轮提案草稿（`.tmp_perf_proposal.md`，未随仓库发布）
> 冲突处以本文为准。

## 0. 终裁记录（分歧处理）

| 争点 | 分歧 | 终裁 |
|---|---|---|
| debounce 语义 | 固定窗口（延迟/并发参谋）vs 冷热两段 vs 投机启动（产品） | **立即起跑 + 打断地平线**（产品方案逻辑碾压：零窗口税、连发自动合并、任意间隔覆盖）；打断期默认 1.5s 可配；距上条消息 >2min 视为冷启动（不启用打断期） |
| interrupt 实现 | 硬取消 vs supersede-discard | **generation-id 丢弃守卫**（三家收敛；不做传输层 cancel；chat timeout 120s→30s 压住被弃线程） |
| "73 项死对象" | 我提案的口径 | 修正为 ~19 项真死 + ~100 项活副本（须选择性迁移 ~35-45 项后才能删 workbench） |
| xdist 进默认 | 融合参谋反对 vs 三家实测 4.3-5.8× | **进默认门禁**（三跑一致有实测支撑；bridge_port per-worker 化同批修） |
| 验收口径 | "13 倍" | 降为脚注（复测 9-13× 波动）；对外统一 E2E 口径：首段 p50 6.9s→目标带 1.2-2.5s |
| 流式 | 我提案的 P0 | **降级条件启动**（Phase 1 实测首段仍 >2.5s 才做，且带四条硬规则） |

## 1. P0-a 现存 bug 修复（先于一切，并发参谋实锤）

**MemoryService 丢更新竞态**（复现：总结在途期间 on_message ×2 被静默丢弃）：
- `_summarize` 在 await chat 返回后、atomic() 窗口内**重读** state，只清除"本次已总结的前缀"（按进入时 buffer 快照长度切片）；
- 或 per-session asyncio.Lock（对齐 mood.py 锁模式）。取 Lock 方案（简单可证）。
- 回归测试：慢 chat_fn 期间并发 on_message ×2，断言总结完成后 pending==2。

## 2. P0-b 延迟改造（O1+O3+O4+O2a）

### O1 thinking 双实例
- vl.toml `[chat] thinking = "disabled"`（默认；可选 enabled/auto；后台任务独立键 `bg_thinking = "enabled"`）。
- 装配：ctx.chat_client（thinking=disabled，主回复+proactive）与 ctx.chat_client_bg（enabled，日记+记忆总结）；旧配置无字段也必须获得关思考默认。
- 参数走既有 extra_body 通道；fallback 客户端不继承 ark 专属字段。
- 回归冒烟固化：非流式 has_reasoning=False（scripts/bench/probe_ark_thinking.py 断言）。

### O3 立即起跑 + 打断地平线（状态机 IDLE/RUNNING/DRIP）
- 消息到达：**立刻**发起 LLM 轮（检索+装配+chat），零窗口税。
- 打断期（默认 1.5s，可配 [0,4]）：期内新消息到达 → generation-id 丢弃当前轮（结果与历史均不落库，仅计费）→ 合并最新上下文立即重跑；距上条 >2min（冷）不启用打断期。
- 分段发送中（DRIP）不可打断：已生成段必须发完（允许压缩段间隔收尾），随后走新一轮回应最新。
- 逐条语义不可合并（合并前逐条执行）：命令路由（旁路直答）、is_crisis 危机检测、mood.handle_message、proactive.on_user_message（_msg_seq 逐条递增）、history user 行（append 到消息到达时刻）。
- 合并只作用于 LLM 轮；合并文本拼接时对定界符做保留标记中和（`<资料引用开始/结束>`、`<Mnemosyne>`、`<system_reminder>`、`【当前内心状态` 复用 neutralize 手法）。
- chat timeout 120s→30s；superseded 计数日志。
- 埋点：history 补 created_at（现全 NULL），1-2 周后据实复核窗口值。

### O4 worker 解耦（per-user 双队列）
- 桥 `_event_worker` 只做帧路由 → 按 user_id 分发 per-user 管线队列（复用 voice/manager.py 懒创建 dict 模式）；reply 回查（≤10s）随事件进管线；EventBus.emit 语义不动。
- per-user 发送 FIFO 统一收编全部文字发送方（主回复段/meme 图/proactive 文字/diary 文字）；语音维持独立队列（文字先行语义）；两队列共用全局 RateLimiter（单例，无 await 间隙）。
- 发送失败短路本回复剩余段（修掉只告警不抛的现状）；interrupt 丢弃时清空该回复未发段。
- 调度长活后台化：diary/proactive 的 LLM 生成 create_task + 复入保护（proactive 已有 _generating；diary 补复入门）；tick 处理时长 p95 ≤1s。
- 保序：同会话 history 写入与 memory on_message 走 per-session 串行通道；回归测试并发压测后 user 序单调、计数守恒。

### O2a 分段阈值（产品新裁决）
- words_count_threshold 100→30（或 \n\n 强切），让 1-3 句回复真正变 2-3 条连发；段间间隔维持 1.5-3.5s 分布（≥1.2s 下限）；首段加 0.8-2.2s 拟人抖动（目标带 1.2-2.5s，不是越小越好）。

### O2 流式（条件启动，默认不做）
- 触发条件：Phase 1 上线实测首段 p50 仍 >2.5s。
- 若做，四条硬规则：flush 只在分段正则完整终止符边界且无未闭合 &&；已 flush 任一段后断流禁整体重发；stream_options.include_usage 先实测 ark 支持；流式独立降级路径。只消费 delta.content。

## 3. P1 成本与正确性

### O5 上下文（32k + O(n)）
- 改 **[context].max_context_tokens**（不是 [chat] 键）默认 32768；128k/256k 保留逃生档（配置注释写场景）。
- O(n) truncate：单遍 turn 前缀和取最长满足预算后缀（方案已验证：8000 条 236.9ms vs 现 300,703ms）；assemble 与 truncate 共享一次估算；补单测（大历史小预算/首条 assistant/耗尽告警分支）。
- 记忆兜底三补丁同批：top_k 2→3、检索相似度下限（低于阈值不注入）、查询并入最近 1-2 条历史。
- 文档口径：窗口外细节以摘要形态存活（不宣传无损）。

### O6 隐式缓存三件套（显式 Context API 砍）
- chat 请求加 `prompt_cache_key = session_id`（实测第 3 次命中 90%）。
- **mood 片段移出 system 前缀**拼到 user 消息侧（资料块语义不变）——这是命中开关。
- 日志落 usage cached_tokens（验收：连发第 2 条起 cached>0）。
- interrupt 重答复用同一 cache_key。

### O7 embedding LRU
- 实例级（随 EmbeddingClient 重建，禁模块级全局）；键=(model, style, dim, text)；内存上限 64MB（8KB/条）；失败不缓存；in-flight 同文本单飞。

### O8 记忆库
- store.py：WAL + busy_timeout(5000) + synchronous=NORMAL + check_same_thread=False + 实例锁（对齐 history 模式；history 补 synchronous=NORMAL）。
- 检索矩阵会话级缓存：失效点=remember 执行时与 delete_session（非 commit 时）；失效与写同锁；"重建不原地改"纪律；万条命中 p50 ≤10ms 进基准测试。
- 量化/mmap/分块全盘否决（实测负优化）；O9 to_thread 主体否决（pragma 后收益趋零），仅万条 miss 路径可选。
- 备份链路同批修：backup.bat 复制前 wal_checkpoint(TRUNCATE) + 备份后行数校验（WAL 化后必须）。

### O2b proactive 人格补丁（质量参谋新裁决，先行合入）
- proactive 的 LLM 调用补 system=persona+mood 快照（现只发裸 user 消息——"必须符合人格设定"却没人格在场）。

## 4. P2 深度融合收尾

### O10 milvus 停机（融合参谋已实操）
- 已完成：三件套 Exited、空库证明存档 backup/milvus_check_20261011205548.json（未随仓库发布）、回退演练 17s-4min。
- 收尾：受控 `docker compose up -d` 重建 napcat（消化 pending recreate，QQ 未登录零成本窗口）；compose 头注释更新；milvus_check.py 注释改 override 取证流程；回退 SOP 成文 docs/回退.md（三件套回退 `--profile legacy up -d etcd minio milvus` 勿整组；整组才含 astrbot；docker stop -t 60 优雅）。

### O11 双份退役（七步链，每步全绿独立提交）
① virtuallover/tests 补 test_memory_benchmark.py（22 项改 import）→ ② 删 plugins/ 整目录 → ③ 删/简化根 conftest.py（实测安全）→ ④ 门禁入口改 `python -m pytest virtuallover/tests -q -n auto`（requirements.txt/SPEC.md/provenance.md 三处）→ ⑤ workbench 活断言选择性迁移（~35-45 项：test_auth 18、NapCatProxy 9、Compose/Backup 10、AuthGate 矩阵）后整删 workbench/ → ⑥ 改 3 个 bat（一键启动:111 删 workbench 启动、setup.bat、一键停止口径）→ ⑦ 改 4 处文档（README×2/部署详解/provenance）。
- workbench/ 删除**不早于回退 runbook 落地**（它是 vl 进程死后唯一幸存控制台 napcat_rollback 按钮——运维参谋裁决：窗口内保留？融合裁决已含 runbook 手动路径。终裁：runbook 成文并演练后即可删，不绑死 14 天）。

### O12 setup.bat v2（前置：init-config）
- 先实现 `python -m virtuallover --init-config`（或 deploy/vl.toml.example + ensure_bridge_token）：全新 VL_ROOT 空目录 init 后 --smoke 就绪（解决鸡生蛋：config_migrate 需要旧栈，新装无源可迁）。
- setup.bat 八条任务书（融合参谋定稿）：分流识别（老用户走 migrate/新装走 init）→ Docker 检查 → napcat up → .venv → vl.toml init → 起 vl → /api/status 探测 → 收尾只留三件事（控制台填 Key、QQ 扫码、可选语音）。删全部 v1 叙述（6185 等待/反向 WS 指引/面板密码）。
- scripts/update.bat 重写为 vl 更新+进程重启流。

### O13 xdist + 基线
- 默认门禁 `python -m pytest virtuallover/tests -q -n auto`（实测 23s）；bridge_port per-worker 化（worker_id 派生端口）恢复 3 条绑定测试真执行；test_gptsovits 假 handler 捕 ConnectionError；**任何非零退出码判败**（xdist 坏路径 exit 5 陷阱）。

### O14 冻结
- local/astrbot_data 放 FROZEN 标记（日期+"回退合并源，勿动"）；窗口内留在备份清单（增量≈0）；窗口关闭后先删数据再动备份脚本；pre-migration 快照与空库证明入冻结保留清单。

## 5. 验收指标（D9 定标，双层口径）

| 指标 | 现状 | 目标 |
|---|---|---|
| 单条首段（生产日志口径 assemble→首 send） | p50 3.17s / p90 7.3s（另一口径 p50 6.9s） | p50 ≤1.8s（O1-only 阶段 ≤2.5s）、p95 ≤4s、下限 ≥0.8s（拟人抖动） |
| 连发（0.5s×3） | 末条 22.7s | 末条→合并回复首段 ≤3s；首条→她说完所有段 ≤12s |
| 记忆竞态 | 丢更新（实锤） | 零丢失（回归测试） |
| 万条检索 | 174ms 阻塞 | 缓存命中 p50 ≤10ms |
| 内存 | 三件套 322MiB | docker stats 口径归零（已达成） |
| 测试 | 137s 串行 | ≤30s（xdist，结果与串行一致） |
| 质量门槛 | — | n≥50 盲评胜率 ≥45% + 机械 linter 100% + 回复长度 p10 ≥10 字 |
| 评测资产 | tmp/ 临时的 | 情景电池+linter+盲评骨架入库 scripts/ |

## 6. 沉淀入库

- 收编已完成：probe_ark_thinking.py / quality_eval.py（情景电池，协议冻结）/ perf_probe.py 均在 scripts/bench/；R5 再收编 key_consumer_check.py（全键消费复查）。
- README/简历数字统一 E2E 口径；一条消息的生命周期时序图改版。

## 7. R2 修订（30 条裁决，与正文冲突处以本节为准）

### 7.1 high 级修订（7 条）

1. **抖动改并行语义**（O2a）：首段发送时刻 = max(LLM 完成, 消息到达+抖动)——抖动不叠加延迟、只保拟人下限。§5 行1 改为**含抖动口径 p50 ≤2.5s、p95 ≤4s、下限 ≥0.8s**；连发末条→首段 ≤3.5s、首条→说完 ≤12s。O2 流式触发条件用同一口径。
2. **打断丢弃语义改写**（O3）：被弃轮只丢弃 assistant 结果与成对历史写；**user 行已在到达时刻落库不回滚**；重跑轮只补 assistant 行。mirror 镜像合并说明按新行语义编写（user 先行、assistant 后补、弃轮无 assistant 行）。
   **R5 增补（被弃轮重试止损）**：结果侧「不做传输层 cancel」维持不变，但重试侧补守卫——chat 的每次重试/fallback 切换前回调 `should_retry`（pipeline 传「代仍存活」谓词），被弃轮失败一次即抛、不再耗 1s/2s/4s 退避与 fallback 额度；被弃轮的失败不计 `llm_fail_count`、只记 info（不是用户可感知失败）。动机：本地大脑多 slot 并发下，僵尸重试把 llama-server KV 占满会让**当前代**也挤不进（实测 burst 三连发全静默，vl.log 2026-10-13 起）。
3. **存量 vl.toml 归一化**：load_config 对 timeout/max_context_tokens/words_count_threshold 做归一化（对齐 _normalize_tts 手法，clamp+warning），启动日志打印三项生效值；删除 [chat].max_context_tokens 死键。
4. **timeout 只降主客户端**：30s 仅 ctx.chat_client（主回复+proactive）；**ctx.chat_client_bg 保持 120s**（后台思考实测 11.5s+，日记生产 prompt 更大）；vl.toml 拆 timeout/timeout_main 或构造期覆写。
5. **首段时点定义**：拟人抖动之后的首个 send_private_msg 出帧（E2E 体感口径），全 spec 统一。
6. **验收判定器**：scripts/bench/ 收编时修 perf_probe 轮次归因 bug（逐轮复位）；新增判定器输出带日期 JSON + --check 退出码对照 §5；日志补 message_id 关联（现无任何日志能把 send 归到消息）。**R5 增补**：数据缺失（NaN，如 burst 轮零出帧）≠ 不达标——指标记 `pass:null`（skipped）并带 note，顶层判「无 False 即过」；修前 `NaN<=target` 恒 False，把「未采集到」误报成「超标」。
7. **setup.bat v2 闭环**：控制台无配置写端点/vl 重启/桥状态——①交互收集 API Key+QQ 号写回 vl.toml（不等控制台）；②收尾判定= /api/status 200 **且**日志见 NapCat 反连；③QQ 扫码明指 NapCat WebUI 6099（不是 vl 控制台）。

### 7.2 重要修订（medium 摘要）

- **DRIP 起点**=首段实际发出（入 FIFO 不算）；打断只在 RUNNING（首段未发出）生效，超窗到达一律"发完再新一轮"；"清空未发段"限定被弃轮（首段未发出的轮）。
- **P0-a 锁边界**：只包无 LLM await 的状态读改写（on_message 的 load/append/save + _summarize 入口快照与 atomic 前重读）；**禁止跨 await chat 持锁**；O4 落地后锁永久保留（_summarize 在通道外跑，正交）。
- **O6 配套清单**：persona.py docstring 装配顺序条款、SPEC.md §5、test_persona 断言（mood 在末条 user 侧且包资料块）三处同步改；验收改"连发第 3 条起 cached>0 或 burst 内出现"；usage 日志带 prompt_cache_key 关联。
- **O11④ 与 O13 同一提交**：含 -n auto 且 3 条绑定测试 passed 非 skipped；**新建 requirements-dev.txt**（pytest/pytest-xdist 版本锁定，setup .venv 安装）；门禁处为 requirements.txt + SPEC.md 两处改 + provenance.md 新增条目。
- **回退键补齐**：[chat] interrupt_window_seconds（默认 1.5，0=关=现行为，[0,4] 校验）；[reply.segmented] first_gap（"0.8-2.2"，空=关抖动）；[mood] fragment_position（system|user，默认 user，system=旧行为回退）；**O3+O4 捆绑无配置回退键——独立成 commit，回退=revert 整个提交**。
- **埋点改毫秒**：created_at 提毫秒精度（或结构化 [perf] 日志行）——秒级坍缩测不出 0.5s 连发间隔；迁移旧行 NULL 保留。
- **O1 回归拆两层**：离线单测（extra_body 断言：主 off/bg on/fallback 不继承）+ scripts/bench/smoke_ark_thinking.py --check 模式（无 key 独立退出码 skip）。
- **评测协议冻结**：臂A=产线(thinking off)/臂B=on 基线；胜率=wins/(n−tie)；judge 单模型 3 次多数表决；linter 硬失败集=[句号/动作描写/超长句]（正向项仅报告）；p10=盲评同批 strip 后长度；**<45% 阻断 O1 默认并触发人工复核**。
- **napcat_rollback 归属修正**：在 vl console（随 vl 同生死），workbench 无此功能；workbench 删除前提=config_migrate 增 --rollback-napcat CLI 子命令 + runbook 演练通过。
- **语音轨与打断**：interrupt 丢弃/短路时该回复语音一律不入队（enqueue_reply 只在回复完整发完后调用——现架构已如此，写明防回归）。
- **上线窗口节**：合批表（P0-a 一批 / O1+O2b 一批 / O3+O4 一批 / P1 一批 / P2 整洁批）；每批一次 vl 重启，选无人对话时段；批后验证= /api/status 200 + NapCat 反连日志 + 测试消息全链路；README 写明"vl 重启期间消息会丢，属预期"。
- **测试一致口径**：§5 行6"非 skip 集合一致"；串行对照统一 VL_TEST_PORT；现状数字标注统计范围与日期。
- test_gptsovits 修复验收=全量 xdist 下 stderr 无 BrokenPipe 栈 + exit 0。

### 7.3 characterization 先行清单（先红后绿，随实现同 PR）

O3：①到达即起跑（假时钟断言零窗口）②打断期新消息→旧轮不发送不写 assistant+superseded+1+重跑 ③>2min 冷启动不启用 ④DRIP 段间不可打断 ⑤逐条语义五件（命令/is_crisis/mood/_msg_seq/user 行到达时刻）⑥四定界符中和 ⑦timeout 30s（主）/120s（bg）。
O4：①per-user FIFO 保序+计数守恒 ②回查 ≤10s 不阻塞他 user ③发送失败短路剩余段 ④interrupt 清空被弃轮未发段 ⑤EventBus 特性测试（快照迭代/异常屏障/EVENT_ERROR 转投）锚定"语义不动"。
P0-a：慢 chat_fn 期间 on_message×2 → pending==2（已定）。
打断端到端：连发 3 条 → 恰 1 回复、user=3/assistant=1、superseded=2、命令与 mood 各执行 3 次。
