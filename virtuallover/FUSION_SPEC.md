# Virtual-lover 深度融合规格（R6-R10 新五轮，我整合六参谋审计定稿）

> R6-R10 是本项目的内部修缮轮次编号；「六参谋」「XX 参谋」是起草期的内部说法，
> 指按六个方向并行审查产出的报告（未随仓库发布）。表中「提案」指我起草的提案。

## 0. 终裁记录（六参谋分歧处理）

| 争点 | 分歧 | 终裁 |
|---|---|---|
| GSVI 安全方案 | 反代壳（我提案）vs 上游补丁（网络参谋） | **上游补丁**：tts_backend.py host=127.0.0.1 + HTTPBasicAuth（两行改动消灭 high，零新增活动件） |
| 6099 处置 | disableWebUI（我提案）vs 保留+中性端口（网络+安全） | **保留+中性端口**：127.0.0.1:61990:6099（disableWebUI 会杀死控制台代理的上游） |
| llama.cpp 重编译 | 做（我提案）vs 砍（许可证+安全） | **砍**：--api-key+文件改名已够，重编译维护负担不值 |
| 对话回放器 | 做（功能）vs 降级候选（安全） | **做**（P1，安全参谋降级理由是工时非安全，我裁决纳入本轮） |
| legacy 数据删除 | 现在删（我提案）vs 等到期（部署） | **等到期**（10-25，v2 观察期未满+我提案自相矛盾） |
| provenance.md | 中性化（我提案）vs 保留（许可证） | **保留不动**（AGPL 承重墙） |

## 1. 品牌词表（锁词，R6-R10 全轮统一）

| 现名 | 新名 | 类型 |
|---|---|---|
| napcat（容器/服务键） | vl-heart / heart | Docker |
| virtual-lover-bot-net（网络） | vl-nerve | Docker |
| GPT-SoVITS-Inference/（目录） | voice-engine/ | 文件系统 |
| qwen2.5-7b-instruct-Q4_K_M.gguf | local-brain-q4.gguf | 文件系统 |
| gsvi.log / gsvi.pid | voice-engine.log / voice-engine.pid | 日志 |
| llama_server_8002.log/.pid | brain-local.log / brain-local.pid | 日志 |
| 控制台文案 'NapCat' | '协议端' | UI |
| 控制台文案 'GSVI' | '语音引擎' | UI |
| 控制台文案 'ark'/'火山' | '云端' | UI |
| /api/engine/nc/ | /api/engine/gw/ | API 路由 |
| tts_backend.py（命令行特征） | voice_engine_entry.py（wrapper） | 进程 |
| llama-server（进程标记） | local-brain | 进程标记 |
| embedding style 'ark_multimodal' | 'multimodal_single' | 配置 |
| 不改：Virtual-lover/virtuallover/vl- | 自有品牌层 | — |
| 不改：local/napcat_data、local/napcat_config | 上游格式+登录态高风险 | — |
| 不改：docker image 字段 | 法律红线（NapCat 限制许可） | — |

## 2. R6 品牌与部署中性化

1. compose：容器名 napcat→vl-heart、服务键 napcat→heart、网络→vl-nerve；4 处 docker compose restart napcat 同步（control.py/config_migrate.py/setup_writeback.py/setup.bat）
2. GSVI 目录改名 GPT-SoVITS-Inference/→voice-engine/（.gitignore **先行**补 voice-engine/——数 GB 目录防提交是一票否决项）；vl.toml gsvi_root + config.py 默认值 + lifecycle argv/cwd/_CMDLINE_MARKER + docs/语音.md 同步
3. GGUF 改名（存量迁移小步：旧名在→自动改名认领+vl.toml 回写）；brain_switch GGUF_Q4/F16 常量同批
4. 控制台文案中性化（index.html+app.py AB_GONE_MESSAGE+/api/engine/nc/→gw/）
5. 日志/pid 文件名中性化（voice/lifecycle.py 默认值+engine.py+console/voice.py+scripts/）
6. .gitignore 补 *.gguf + voice-engine/
7. LICENSES/THIRD-PARTY.md（我已写好）入库 + README 指路
8. docs/回退.md 修正（AstrBot 镜像已清，档 B 实质退役）
9. style 取值 ark_multimodal→multimodal_single（内部映射，llm.py 两处）

## 3. R7 网络安全收口

1. GSVI 上游补丁：tts_backend.py:224 host=_config.get("tts_host","127.0.0.1")；config.json 落 tts_host=127.0.0.1；enable_auth=true+强口令（vl.toml [tts] 单源，lifecycle 注入）；voice/gsvi.py 加 Authorization Basic 头
2. 6099 中性端口：compose ports 改 127.0.0.1:61990:6099；engine.py DEFAULT_NC_BASE 可配（vl.toml [console] nc_base）
3. llama-server：--api-key（setup 时随机生成写 vl.toml fallback_api_key）；switch_toml_local 弃常量 'local'；三处探测改 /health 或带 Bearer；LLAMA_MARKER 同步换
4. 安全修复：_vl_pids 路径边界匹配（normcase+startswith(repo+sep)，禁裸子串）+前缀同名回归测试；_normalize_tts clamp retries≥1；onebot_server _download_to 换 smart_urlopen（代理修复最后缺口）；router spawn host clamp

## 4. R8 语音引擎融合

1. GSVI wrapper：vl 自有 entry 脚本（import tts_backend 的 Flask app，app.run(host='127.0.0.1')）；lifecycle argv 换 wrapper；_CMDLINE_MARKER 同步；service.py gsvi_stop marker 同步
2. 训练包 GPT-SoVITS-v2pro-20250604/ 移出仓库根（或单独补 .gitignore 行）
3. gsvi_boot.log 死文件直删
4. docs/语音.md 用户指令改写（voice-engine 命名+下载指路+LICENSE 保留声明）

## 5. R9 功能融合

1. 撤三个大脑 bat（本地大脑启动/关闭/切回云端）：控制台已有等价按钮；引擎页补'只停本地引擎'（brain_stop）ghost 按钮；根目录 6 bat→2 bat（一键启动/停止）；docs 同步清理引用
2. 控制台 7→5 页：引擎+人格合并为'引擎与人格'；记忆与日记常驻卡片（消灭 placeListCards DOM 搬家 hack）
3. 对话回放器：GET /api/history/list?date=&page=（≤200/页，三重门继承，只读）+'记忆与日记'页 timeline 卡
4. 纪念日/连续天数仪式感：认识第一天锚点（session 最早 created_at 落盘）+里程碑 proactive 触发（默认关）
5. 晚安语音配方：docs/语音.md 补文档（proactive tts 配方，不做独立功能）

## 6. R10 终验与发布

1. 逆向复验（对照预研 18 痕迹）：netstat 非回环清零（5000→127.0.0.1）、进程表/端口表无上游名、仓库自检+git check-ignore
2. 全链路 E2E：扫码重登→语音→大脑切换→一键启停循环
3. 发布物硬门：git ls-files | grep -Ei '\.(gguf|exe|dll|zip|pth|ckpt)$' 零命中
4. 组件可见可接受豁免清单：napcat 镜像 layers/GGUF 元数据/OneBot 协议/ark SNI/onebot11_*.json
5. 五轮总结报告

## 7. 硬性纪律

- 改名必须单提交+门禁（grep 清零+pytest 全绿+E2E）
- .gitignore 先行（voice-engine/ 与 *.gguf 在任何文件移动前落地）
- 法律文档层显式排除中性化（provenance.md/README 致谢段/THIRD-PARTY.md）
- 新功能文案零 ark|napcat|gsvi|astrbot 字样
- 训练包与 voice-engine 目录永不入 git
