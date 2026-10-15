# -*- coding: utf-8 -*-
"""vl.toml 配置树 —— 加载 / 校验 / 序列化三件套（SPEC §11.1）。

职责：
- 用 dataclass 固化 vl.toml 的完整字段树（chat/embedding provider、persona、tts、
  分段参数、主动消息、mood、session_id 模板、admins、mnemosyne 行为参数……）；
- ``load_config``：tomllib 读取（Python ≥3.11 标准库）+ 启动校验（fail-fast，
  缺失必填项给中文报错）；
- ``dump_config``：把配置树序列化回 TOML 文本——tomllib 只读不写，因此自带一个
  小型序列化器，供 tools/config_migrate.py 生成 vl.toml、以及 bridge token 首次
  落盘时复用（避免再造第二套 TOML 写出逻辑）；
- ``ensure_bridge_token``：桥 token 缺失时用 ``secrets.token_urlsafe(32)`` 生成并
  回写（SPEC §3.3：禁沿用旧栈 13 字符弱 token）。

设计约定：
- 敏感值（api key / token / QQ 号）只存在于 local/config/vl.toml 与内存中，本模块
  的报错与日志一律不携带原文；
- 数值默认值全部取自现网配置实测（proactive 30-120 分钟、mnemosyne num_pairs=2、
  [context].max_context_tokens=32768 见 PERF_SPEC §3-O5 等），可被 vl.toml 覆盖；
- 未知键不硬失败（向后兼容考虑），以 warnings 提示防拼写错误。
"""

from __future__ import annotations

import dataclasses
import logging
import secrets
import tomllib
import urllib.parse
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

__all__ = [
    "ConfigError",
    "BridgeConfig", "ChatProviderConfig", "EmbeddingConfig", "PersonaConfig",
    "TtsConfig", "SegmentedReplyConfig", "RateLimitConfig", "ReplyConfig",
    "ProactiveConfig", "MoodConfig", "DiaryConfig", "MnemosyneConfig",
    "MemesConfig", "ConsoleConfig", "ToolsConfig", "ScheduleConfig",
    "RoutineConfig", "AnniversaryConfig",
    "StorageConfig", "VlConfig",
    "load_config", "dump_config", "ensure_bridge_token",
]

# 当前 schema 版本：vl.toml 顶部 schema_version 必须与此一致（SPEC §13 升级自动迁移的锚点）
SCHEMA_VERSION = 1


class ConfigError(RuntimeError):
    """配置缺失/非法。报错文案全部中文，直接面向用户。"""


logger = logging.getLogger("virtuallover.config")


def _default_emotion_map() -> dict:
    """[tts].emotion_map 默认值：VOICE_SPEC §1 终裁 7 态表（单一来源 voice.mapping）。"""
    from virtuallover.voice.mapping import DEFAULT_EMOTION_MAP  # 延迟导入防环

    return dict(DEFAULT_EMOTION_MAP)


# ---------------------------------------------------------------------------
# 配置树：各分节 dataclass（默认值=现网实测值，出处见各字段注释）
# ---------------------------------------------------------------------------

@dataclass
class BridgeConfig:
    """OneBot 反向 WS 服务端（SPEC §3）。"""
    host: str = "127.0.0.1"          # 终裁：绑 127.0.0.1，零局域网暴露
    port: int = 6199                  # 与旧栈 ws_reverse_port 一致
    path: str = "/ws"
    token: str = ""                   # 留空则 ensure_bridge_token 生成 token_urlsafe(32)
    self_id: str = ""                 # 机器人账号（迁移自 napcat onebot11_<QQ>.json 文件名）
    napcat_config_dir: str = "local/napcat_config"  # 供控制台"指向切换"动作动态发现 onebot11_*.json


@dataclass
class ChatProviderConfig:
    """OpenAI 兼容 chat 客户端（SPEC §4；PERF_SPEC §2 O1 thinking 双实例）。

    thinking 双实例语义（PERF_SPEC §2 O1 + §7.1-4）：
    - ``thinking``：主回复 + proactive 用的思考开关（disabled/enabled/auto），
      默认 disabled——首段延迟瘦身，关思考后少一次 reasoning 往返；
    - ``bg_thinking``：后台任务（日记 + 记忆总结）独立键，默认 enabled——
      后台思考实测显著提升产出质量（tmp/probe_bg_thinking A/B），且不在
      用户等待路径上；
    - ``timeout``：后台客户端读超时（120s 实测；后台思考 11.5s+）；
    - ``timeout_main``：主客户端读超时（30s，压住被弃线程的打包口径，
      §0 终裁 interrupt 一节）；30s 只降主客户端，后台保持 timeout。

    注：旧版此处的 max_context_tokens 是死键（装配与 truncate 只消费
    [context].max_context_tokens），PERF_SPEC §3-O5/§7.1-3 起删除——
    存量 vl.toml 残留该键时 load_config 给出迁移指引告警。
    """
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 120                # 后台客户端读超时，旧栈 provider_sources.timeout 实测 120
    timeout_main: int = 30            # 主客户端读超时（PERF_SPEC §7.1-4：30s 只降主客户端）
    # 打断地平线窗口（PERF_SPEC §7.2 回退键，判定实现在 brain/pipeline.py）：
    # 窗口内（首段未发出的 RUNNING 轮）新消息到达 → generation-id 丢弃当前轮
    # 并合并重跑；默认 1.5s，0=关=现行为，合法区间 [0,4]（越界 clamp+告警）；
    # 距上条 >2min 视为冷启动，一律不启用打断期。
    interrupt_window_seconds: float = 1.5
    thinking: str = "disabled"        # 主回复思考开关（O1：默认关思考）
    bg_thinking: str = "enabled"      # 后台任务思考开关（O1：日记/记忆总结保留思考）
    # ---- 多模态·图片识别（消费方 virtuallover/brain/vision.py）----
    # chat 模型是否支持视觉输入（image_url 多模态消息）。true：带图消息经
    # vision.process_image 下载图片并以 OpenAI 多模态格式发给本模型分析，
    # 描述替换消息里的「[图片]」占位；false（默认关）：维持「[图片]」占位、
    # 零额外调用、行为与旧版一致。给 [chat] 换支持视觉的模型
    # （doubao-vision / glm-4v / gpt-4o 等）后再打开。
    supports_vision: bool = False
    # fallback_provider_ids 键已删除（五轮修缮 R4）：SPEC §4 初版预留的列表键，
    # 从未有任何代码读取（假键）；智能降级由五轮修缮 R2 的 fallback_* 键组
    # （fallback_enabled/fallback_api_base/…，下方）承载，消费方
    # virtuallover/brain/router.py。存量 vl.toml 残留该键时 load_config 给出
    # 迁移指引告警（_build_section 特例分支）。
    # ---- 智能降级主备（五轮修缮第二轮；virtuallover/brain/router.py 消费）----
    # 实证：ark 欠费 403（vl.log「AccountOverdueError」）时她哑 8 小时——llm.py
    # 的 fallback 链早已造好但 build_chat_clients 从未接线。本组键开启后由
    # BrainRouter 包住主/后台客户端做「瞬态 per-request 降级 + 401/403 黏性
    # 切本地 + 30min 半开探针」。默认关：云端用户不装本地大脑也安全——
    # fallback 不可用时黏性模式只告警不 spawn，行为与关闭时一致。
    fallback_enabled: bool = False    # 总开关（默认关=零行为变化）
    fallback_api_base: str = "http://127.0.0.1:8002/v1"  # 本地 llama-server（scripts/brain_switch.py 实测端口；8001 预留给其他本地服务）
    fallback_model: str = "qwen2.5-7b-instruct"          # 客户端请求体 model 字段（单模型 server 忽略该字段，实测 b11040）
    # R7（FUSION_SPEC §3.3）：本地大脑 --api-key 单源——brain_switch/Router 拉起
    # server 时经 --api-key 武装，ChatClient 请求带同值 Bearer。默认 "local"=
    # 未升级占位（server 不武装，旧行为）；setup_writeback / brain_switch
    # ensure_local_api_key 会对弱值强生成 secrets.token_urlsafe(32) 并写回。
    # 敏感值只落 local/config/vl.toml（gitignored），绝不进代码/测试/日志。
    fallback_api_key: str = "local"
    fallback_timeout: int = 120       # 本地 7B 首 token 慢，读超时给足（后台/黏性共用）
    fallback_max_context_tokens: int = 4096  # 本地窗口小（server -c 6144，扣 system/记忆注入余量；brain_switch LOCAL_CONTEXT_TOKENS 同源）
    fallback_auto_spawn: bool = True  # 黏性触发且 8002 无应答时按需拉起 llama-server；false=只探测不 spawn
    # spawn 参数（brain_switch.py 实测值同源；路径含空格请改放无空格目录——
    # fallback_llama_args 按空格切分，不做引号解析）
    fallback_llama_exe: str = ""  # 通用性：默认空（不配置本地大脑也能纯云端运行）
    fallback_gguf: str = "models/gguf/local-brain-q4.gguf"
    fallback_llama_args: str = "-ngl 99 -c 6144 --cache-type-k q8_0 --cache-type-v q8_0 -t 8"
    # 切本地首次的温柔提示（人设内口吻；false=不发）
    fallback_notice_enabled: bool = True
    fallback_notice_text: str = "唔……我脑袋有点晕，说话可能慢一点，你多等人家一下下嘛，别担心呀～"
    # ---- 多模态 STT（语音转文字；消费方 virtuallover/brain/stt.py）----
    # stt_provider：空=不支持（语音消息降级为「[语音消息]」占位，与 [图片] 同款
    # 语义）；配了即启用 OpenAI 兼容 /audio/transcriptions 转写（如 'doubao'
    # 'whisper'/'openai' 等，当前实现只按 OpenAI 兼容协议发请求，值仅作标识）。
    # stt_api_key：STT 服务密钥（敏感值只落 local/config/vl.toml，gitignored，
    # 绝不进代码/日志）；留空=沿用 [chat].api_key（ark 等同账号 ASR 共 key 场景）。
    # stt_model：转写模型名，如 'whisper-1' 或方舟 doubao ASR 模型 ID；
    # 端点基址沿用 [chat].api_base（POST {api_base}/audio/transcriptions）。
    stt_provider: str = ""
    stt_api_key: str = ""
    stt_model: str = ""


@dataclass
class EmbeddingConfig:
    """embedding 独立客户端（SPEC §4，与 chat 分字段配置）。"""
    # 总开关：false = 记忆功能整体关闭（检索/注入/写入全停，聊天/日记/语音不受
    # 影响）。全新安装 setup 跳过 embedding 问题即写 false（setup_writeback）；
    # _validate 因它豁免本节三个必填检查——没有它，跳过 embedding 的配置在
    # 启动校验就直接炸（原 fail-fast 不容忍缺失，vl 起不来）。
    enabled: bool = True
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    dim: int = 2048                   # 旧栈 embedding_dimensions 实测 2048
    timeout: int = 20                 # 旧栈实测 20s
    # 请求风格：openai = POST /embeddings（标准，data 为列表，可批量）；
    # multimodal_single = POST /embeddings/multimodal（多模态嵌入端点，单条输入；
    # R6 前名 ark_multimodal，load 时由 llm.py 内部映射兼容存量配置）
    # input 为 [{type:text,...}]，data 为 dict 单条——实测纯文本可用且
    # 维度 2048 与库元数据一致；迁移工具对 ark + ep- 接入点自动设置）
    style: str = "openai"


@dataclass
class PersonaConfig:
    """人格：全文迁移 + 开场白（SPEC §5）。"""
    name: str = ""                    # 人格名（session_id 模板首段）
    system_prompt: str = ""           # 1308 字符全文迁移
    begin_dialogs: List[str] = field(default_factory=list)  # 4 条开场白（交替 user/assistant 语义由 persona 组解释）


@dataclass
class TtsConfig:
    """GPT-SoVITS 客户端参数（SPEC §9 + VOICE_SPEC §1 语音框架扩展）。"""
    enabled: bool = True              # 旧栈 provider_tts_settings.enable 实测 true
    base_url: str = "http://127.0.0.1:5000"  # 宿主机直连（终裁；load 时归一化 host.docker.internal/localhost 残留）
    character: str = "Huohua"         # VOICE_SPEC §0 终裁：默认 Huohua（7 情绪承载联动；Hutao 单情绪致联动哑火，控制台可一键切回）
    emotion: str = ""                 # 旧栈 gsv_default_parms.gsv_emotion（兜底情绪；情绪联动走 emotion_map）
    text_lang: str = "zh"             # 旧栈 gsv_default_parms.gsv_text_lang
    audio_format: str = "wav"         # SPEC §9 固定 wav
    trigger_probability: float = 1.0  # VOICE_SPEC §2：按回复整体抽样一次的语音触发概率（1.0=每条回复都带语音=旧行为）
    timeout: int = 60                 # SPEC §9：60s 超时
    retries: int = 3                  # SPEC §9：3 次重试
    # ---- VOICE_SPEC §1 语音框架 v2 ----
    auto_start: bool = True           # 生命周期 auto_start 拉起 GSVI（adopt-first：端口有应答即收编，绝不杀外部实例）
    gsvi_root: str = "voice-engine"  # 语音引擎根目录（Popen argv/cwd 基准；相对仓库根；原 GPT-SoVITS-Inference/，R6 改名）
    emotion_map: dict = field(default_factory=lambda: _default_emotion_map())  # 7 态映射表（生效序=默认←此处←voice/emotion_map.json 覆盖层）
    default_emotion: str = "聊天"     # 回退链第二跳：配置默认情绪（映射值∉注册表时启用）
    cache_max_entries: int = 500              # 语音缓存条数上限（双上限之一，§1）
    cache_max_bytes: int = 128 * 1024 * 1024  # 语音缓存字节上限（双上限之二，§1：128MB）
    # ---- R7 网络安全收口（FUSION_SPEC §3.1）----
    # 引擎 HTTP Basic 口令（单源）：lifecycle 拉起前注入引擎 Inference/config.json
    # 的 enable_auth="true" + user={vl: 口令}，voice/gsvi.py 客户端同源带
    # Authorization 头。空=不武装（引擎无鉴权 + 防火墙告警，旧行为）。
    # 敏感值只落 local/config/vl.toml（gitignored），绝不进代码/测试/日志。
    tts_auth_password: str = ""


@dataclass
class SegmentedReplyConfig:
    """分段回复参数（SPEC §5 + PERF_SPEC §2 O2a，从旧配置迁移）。

    - ``words_count_threshold``：O2a 裁决 100→30（或 \\n\\n 强切），让 1-3 句
      回复真正变 2-3 条连发；存量 vl.toml 显式写旧默认 100 的由 load_config
      归一化为 30 并告警明示；
    - ``first_gap``：首段拟人抖动区间原文（"0.8-2.2" 秒），空串=关闭抖动。
      语义是并行下限（PERF_SPEC §7.1-1）：首段发送时刻 = max(LLM 完成,
      消息到达 + first_gap 抖动)——抖动不叠加延迟，只保拟人下限；
    - 段间隔（interval/min_gap/max_gap）维持 1.5-3.5s 分布，发送侧统一
      ≥1.2s 下限（sender.MIN_SEGMENT_GAP_SECONDS）。
    """
    enable: bool = True
    words_count_threshold: int = 30   # O2a 裁决：100→30（旧栈实测 100 由 load_config 归一化）
    split_mode: str = "regex"
    regex: str = ""
    split_words: List[str] = field(default_factory=list)
    interval_method: str = "log"
    interval: str = ""                # 形如 "1.5-3.5" 的秒数区间原文
    log_base: float = 2.6             # 旧栈实测
    min_gap: float = 1.5              # 发送间隔下界（秒），旧栈 interval 区间解析失败时的兜底
    max_gap: float = 3.5              # 发送间隔上界（秒）
    first_gap: str = "0.8-2.2"        # 首段拟人抖动区间（O2a；空串=关闭）
    # ---- 配置地基预置：打字速度模拟（消费方 brain/sender.py 分段发送链路）----
    # 每个字的打字秒数：段长 × typing_cps = 该段「打字耗时」，发送时刻再推迟
    # 这么久（拟人手打节奏）；0=关闭打字速度模拟（零行为变化）。消费契约同
    # sender.py 现有 duck typing（缺属性按关闭处理，兼容旧参数对象）。
    typing_cps: float = 0.08


@dataclass
class RateLimitConfig:
    """发送速率限制（SPEC §5：10 条/30s stall）。"""
    window_seconds: int = 30
    max_count: int = 10


@dataclass
class ReplyConfig:
    """主回复发送侧行为。"""
    segmented: SegmentedReplyConfig = field(default_factory=SegmentedReplyConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)


@dataclass
class ProactiveConfig:
    """主动消息（SPEC §8.3，8 条行为的参数面；行为实现归 soul/proactive.py）。"""
    enable: bool = False
    session_id: str = ""              # 主动消息目标会话（旧栈 friend_settings.session_list[0]）
    min_interval_minutes: int = 30    # ①随机间隔下界（实测 30）
    max_interval_minutes: int = 120   # ①随机间隔上界（实测 120）
    quiet_hours: str = "23-7"         # ②安静时段（跨午夜语义）
    max_unanswered: int = 3           # ③unanswered≥3 暂停
    boot_trigger_minutes: int = 30    # ④启动 30min 无消息触发一次
    prompt_template: str = ""         # ⑤模板文本=用户数据，从旧配置迁入（含 {{current_time}} 等占位符）
    tts_enabled: bool = True          # ⑧TTS 优先（实测 enable_tts=true）
    always_send_text: bool = False    # ⑧dual_output=false 语义：成功只发语音（实测）
    segmented: SegmentedReplyConfig = field(default_factory=SegmentedReplyConfig)  # 阈值 80 字实测
    # ---- 五轮修缮 R3（行为实现归 soul/proactive.py）----
    recent_user_window_minutes: float = 5.0  # 1b：_fire 前近期活跃避让窗口（分钟）
    retry_min_minutes: int = 5               # 1a：发送未送达的重试间隔下界（分钟）
    retry_max_minutes: int = 10              # 1a：发送未送达的重试间隔上界（分钟）
    slowdown_min_minutes: int = 360          # 1c：降频模式间隔下界（6h）
    slowdown_max_minutes: int = 720          # 1c：降频模式间隔上界（12h）
    prompt_template_slowdown: str = ""       # 1c：克制版模板（空=回退 prompt_template）
    history_context_messages: int = 10       # 1d：近况注入的对话条数（8-12）
    history_context_chars: int = 1200        # 1d：近况素材块字符预算
    # ---- R9 功能融合（行为实现归 soul/anniversary.py）----
    # 纪念日仪式感：以「认识第一天」（会话库最早一条消息）为锚点，在第 7/30/100
    # 天各经主动消息通道送一次纪念日消息。配置地基起默认开；生效仍需
    # proactive.enable=true（她要开口说话，走的正是主动消息的 LLM+发送链路）。
    anniversary: bool = True
    # ---- 配置地基预置：担心型追问 + 每日话题（行为实现归 soul/proactive.py 后续迭代）----
    # 担心型追问：超过 worried_after_hours 小时没收到他的任何消息时，用本模板
    # （占位符 {{last_user_gap}}=距上次回消息的时长，由消费方渲染）主动关心一次；
    # 超过 give_up_after_hours 小时仍无回应则放弃追问（防对着失联的人连环轰炸）。
    # 模板是用户数据：默认文案只是出厂值，控制台/vl.toml 可整段替换。
    prompt_template_worried: str = (
        "距离他上次回你消息已经 {{last_user_gap}} 了。你是他有点担心又怕吵到他的"
        "女朋友：猜猜他是忙疯了、睡着了还是不舒服，真切地表达担心，可以小小地怪他"
        "让你担心，但别连环追问，最后留一句「看到消息回我一下好不好」。"
    )
    worried_after_hours: float = 24.0   # 触发担心型追问的沉寂阈值（小时）
    give_up_after_hours: float = 72.0   # 放弃追问的沉寂阈值（小时）
    daily_topic: bool = True            # 每日话题开关（主动消息缺素材时兜底找话题）


@dataclass
class MoodConfig:
    """vl_mood_engine 现网活配置（port-of-own，SPEC §8.1 + PERF_SPEC §3-O6）。"""
    keyword_groups: dict = field(default_factory=dict)  # sweet/praise/rude 三组关键词
    daily_bonus_enabled: bool = True
    inject_enabled: bool = True       # mood 快照注入（SPEC §5 开关）
    # mood 片段注入位置（PERF_SPEC §3-O6 + §7.2 回退键）：
    # user（默认）= 拼到末条 user 消息侧（资料块包裹）——system 前缀逐日稳定，
    #               是 provider 侧 prompt 隐式缓存（prompt_cache_key）的命中开关；
    # system      = 旧行为回退键（拼在 system 人格全文之后）。
    fragment_position: str = "user"
    # R3 情绪灵敏度：普通消息的单次回落幅度（原实现硬编码 0.1——两次普通消息
    # 就把一次关键词唤起的情绪浇灭，审计实证 calm 是常态 15/16）。0.04 让情绪
    # 有约 25 条普通消息的自然半衰期，可控可回退。
    calm_down_per_message: float = 0.04


@dataclass
class DiaryConfig:
    """vl_diary 日记参数（SPEC §8.2）。"""
    diary_time: str = "22:30"         # 触发时刻（+0-20min 抖动由 soul/diary.py 实现）
    min_chars: int = 100
    max_chars: int = 200
    max_history_messages: int = 60
    max_excerpt_chars: int = 4000
    prompt_template: str = ""         # 用户数据，原样迁入


@dataclass
class MnemosyneConfig:
    """记忆行为参数（SPEC §7 + PERF_SPEC §3-O5 检索兜底三补丁）。"""
    num_pairs: int = 2                # 写入触发：每 num_pairs*2 条消息总结一次
    top_k: int = 3                    # 注入条数（O5 补丁：2→3，多带一条近旁记忆兜底）
    candidate_k: int = 8              # 候选数（SPEC §7.1 终裁，旧配置无此字段）
    # 检索相似度下限（O5 补丁）：余弦低于该值的命中不注入（宁缺勿滥防噪声）；
    # 0=关闭下限（旧行为）。/eval 评测检索不受此限（benchmark 自管）。
    min_similarity: float = 0.3
    max_prompt_chars: int = 4000      # 查询文本截断（旧栈 max_prompt_chars_for_embedding）
    inject_enabled: bool = True
    # injection_position 键已删除（五轮修缮 R5）：SPEC §7 终裁记忆注入位置固定
    # user_prompt prepend（禁 system——persona.assemble 硬编码，见 brain/persona.py），
    # 该键从未被任何代码读取。存量 vl.toml 残留时 load_config 给迁移指引告警。
    # time_anchor 键已删除（五轮修缮 R3）：自旧栈迁移以来从未被任何代码读取的
    # 死配置（旧键 use_summary_time_anchor 实测遗存），且 R3 起记忆注入行改用
    # 相对时间分档（store.relative_time_label），该开关永久失去语义。存量
    # vl.toml 若残留该键，load_config 按未知键告警忽略，不阻断启动。


@dataclass
class MemesConfig:
    """表情包后处理（SPEC §8.4，参数实测自 meme_manager_config.json）。"""
    enable: bool = True
    probability: int = 60             # emotions_probability=60（命中率 60%）
    max_images: int = 1               # max_emotions_per_message=1 且 strict=true
    memes_dir: str = "local/shared_plugin_data/meme_manager/memes"  # 398 张本地图（19 个 tag 目录）
    extra_tags: List[str] = field(default_factory=list)  # 白名单叠加项（默认 19 图库 tag + 10 历史 tag）


@dataclass
class ConsoleConfig:
    """管理控制台（SPEC §10，workbench v2 移植；随主进程同起同停）。"""
    enable: bool = True
    host: str = "127.0.0.1"           # 三重门之一：仅本机绑定
    port: int = 8787                  # 与 workbench v1/v2 一致，compose 只对本机发布
    # R7 中性端口（FUSION_SPEC §3.2）：协议端 WebUI 代理上游（compose 宿主侧
    # 127.0.0.1:61990 → 容器 6099）。回退旧映射/自定义端口时改此键，不必改代码
    nc_base: str = "http://127.0.0.1:61990"


@dataclass
class ToolsConfig:
    """联网搜索（多模态能力工程；消费方 virtuallover/brain/websearch.py）。

    - ``web_search_enabled``：总开关（默认 true）。false 时聊天管线不做
      搜索意图检测，行为与旧版一致；
    - ``search_provider``：'duckduckgo'（免费无 key）| 'tavily'（有 key 更准）；
    - ``tavily_api_key``：Tavily 密钥。空（默认）= 即使 provider 填了 tavily
      也回落 DuckDuckGo（无 key 兜底语义）；敏感值只落 local/config/vl.toml
      （gitignored），绝不进代码/测试/日志。
    """
    web_search_enabled: bool = True
    search_provider: str = "bing"  # 默认 Bing 中国版（免 key 中国直连；可选 tavily/duckduckgo）
    tavily_api_key: str = ""
    # ---- 配置地基预置：天气感知（消费方后续接入，键名先钉死）----
    # 城市名（如 "杭州"），用于拉取当日天气并注入聊天上下文；空（默认）=关闭
    # 天气感知，行为与未配置时一致。用户隐私考量：城市由用户自愿配置，只落
    # local/config/vl.toml，不进代码/日志。
    weather_city: str = ""


def _default_schedule_entries() -> List[dict]:
    """[schedule].entries 默认课表：大二在校一周作息（工作日 "1-5"，周末 "0,6"，0=周日）。

    每次调用重建全部 dict（default_factory 语义），实例之间互不串改。
    activity 文案是「她此刻正在做什么」的第一人称素材，注入聊天时原样引用。
    """
    return [
        # ---- 工作日（周一~周五）----
        {"days": "1-5", "start": "08:00", "end": "09:40", "activity": "在上早八课"},
        {"days": "1-5", "start": "09:50", "end": "11:30", "activity": "在上课"},
        {"days": "1-5", "start": "11:30", "end": "13:30", "activity": "在食堂吃饭、回宿舍午休"},
        {"days": "1-5", "start": "14:00", "end": "15:40", "activity": "在上课"},
        {"days": "1-5", "start": "15:50", "end": "17:30", "activity": "在实验室做实验"},
        {"days": "1-5", "start": "17:30", "end": "19:00", "activity": "在吃晚饭、陪室友操场散步"},
        {"days": "1-5", "start": "19:00", "end": "22:00", "activity": "在图书馆自习"},
        # ---- 周末（0=周日，6=周六）----
        {"days": "0,6", "start": "09:30", "end": "12:00", "activity": "睡懒觉、窝在床上追动漫"},
        {"days": "0,6", "start": "12:00", "end": "14:00", "activity": "和室友出去吃午饭"},
        {"days": "0,6", "start": "14:00", "end": "18:00", "activity": "逛小吃街、在校园闲逛"},
        {"days": "0,6", "start": "18:00", "end": "23:00", "activity": "在宿舍追剧、打游戏"},
    ]


@dataclass
class ScheduleConfig:
    """作息课表（配置地基预置段；消费方 soul 侧日程模块后续接入）。

    - ``enabled``：总开关，false 时聊天管线不注入「她此刻在做什么」；
    - ``entries``：课表条目列表，每项含 days/start/end/activity 四个 str——
      days 形如 "1-5"（周一~周五）或 "0,6"（周末；0=周日）；start/end 为
      "HH:MM" 24 小时制；activity 为该时段正在做的事（聊天素材原文）。
    """
    enabled: bool = True
    entries: List[dict] = field(default_factory=_default_schedule_entries)


@dataclass
class RoutineConfig:
    """早晚安锚点（配置地基预置段；消费方 soul 侧作息模块后续接入）。

    - morning_time / night_time："HH:MM" 锚点时刻。night 默认 22:50，与
      [diary] diary_time=22:30 的日记错开 20 分钟，避免晚安和日记挤同一时段；
    - *_jitter_minutes：锚点随机抖动幅度（分钟）——天天准点报备会露机器感，
      抖动让「她」的作息像真人。
    """
    enabled: bool = True
    morning_time: str = "07:30"           # 早安锚点
    morning_jitter_minutes: int = 10      # 早安抖动（分钟）
    night_time: str = "22:50"             # 晚安锚点（与日记 22:30 错开）
    night_jitter_minutes: int = 5         # 晚安抖动（分钟）


@dataclass
class AnniversaryConfig:
    """生日纪念日（配置地基预置段；消费方 soul 侧生日模块后续接入）。

    两侧生日分开配，格式 "MM-DD"（如 "03-14"），空（默认）=不启用该侧。
    生日是个人信息：只落 local/config/vl.toml（gitignored），不进代码/测试/日志。
    """
    user_birthday: str = ""           # 他的生日（MM-DD；空=不启用）
    her_birthday: str = ""            # 她的生日（MM-DD；空=不启用）


@dataclass
class StorageConfig:
    """本地数据路径（相对仓库根，均在 gitignore 的 local/ 下）。"""
    data_dir: str = "local/vl_data"
    history_db: str = "local/vl_data/history.db"       # SPEC §6
    memory_db: str = "local/vl_data/memory.db"         # SPEC §7.1 内嵌向量库
    mirror_file: str = "local/vl_data/mirror_v4.json"  # 双写镜像（回退脚本据其合并回 data_v4.db）
    # napcat_backup_dir 键已删除（五轮修缮 R5）：假键——迁移/回退的备份实际落
    # 同目录 .bak-<时间戳>（tools/config_migrate.py），一键备份走 backup.bat 的
    # backup\<时间戳>，控制台 backup 动作走 AppConfig.backup_dir，三处都不读本键。


@dataclass
class VlConfig:
    """vl.toml 根配置。标量字段放在最前，保证 TOML 序列化时先于任何 [table]。"""
    schema_version: int = SCHEMA_VERSION
    session_id: str = ""              # 会话模板实值：`<人格名>:FriendMessage:<QQ号>`（从 data_v4.db 带出）
    admins: List[str] = field(default_factory=list)    # 管理员（§8.5，等价旧 ADMIN 语义）
    whitelist: List[str] = field(default_factory=list)  # 白名单（§8.6 门禁；旧栈 id_whitelist）
    log_level: str = "INFO"
    # timezone 键已删除（五轮修缮 R4）：自旧栈迁移以来从未被任何代码读取的死配置
    # （注释声称的"diary/proactive 时刻基准"实为虚指——diary/compute_next_diary_run
    # 与 proactive 调度一律 datetime.now() 取本机系统时区），照 R3 time_anchor 先例
    # 删除。存量 vl.toml 若残留该键，load_config 给出迁移指引告警（已忽略，不阻断）。

    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    chat: ChatProviderConfig = field(default_factory=ChatProviderConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    mood: MoodConfig = field(default_factory=MoodConfig)
    diary: DiaryConfig = field(default_factory=DiaryConfig)
    mnemosyne: MnemosyneConfig = field(default_factory=MnemosyneConfig)
    memes: MemesConfig = field(default_factory=MemesConfig)
    console: "ConsoleConfig" = field(default_factory=lambda: ConsoleConfig())
    tools: "ToolsConfig" = field(default_factory=lambda: ToolsConfig())
    schedule: "ScheduleConfig" = field(default_factory=lambda: ScheduleConfig())
    routine: "RoutineConfig" = field(default_factory=lambda: RoutineConfig())
    anniversary: "AnniversaryConfig" = field(default_factory=lambda: AnniversaryConfig())
    storage: StorageConfig = field(default_factory=StorageConfig)
    context: "ContextConfig" = field(default_factory=lambda: ContextConfig())


@dataclass
class ContextConfig:
    """上下文管理（SPEC §5：不做 LLM 压缩摘要，只 truncate；PERF_SPEC §3-O5）。

    max_context_tokens 语义：模型上下文窗口的预算口径，truncate 预算 =
    max_context_tokens * truncate_ratio。默认 32768（PERF_SPEC §3-O5 终裁：
    窗口外细节以记忆摘要形态存活，不宣传无损）。

    逃生档（显式改大，配置示例）：
    - 131072（128k）：长对话全量保真场景——如需回查数月内逐句原文；
    - 262144（256k）：旧栈口径（豆包 lite 实测窗口）——仅在确有超长会话
      且计费可接受时使用；默认 32k 已覆盖日常会话且显著降低 token 成本。
    """
    max_context_tokens: int = 32768
    truncate_ratio: float = 0.82      # 阈值触发丢最旧 1 turn


# ---------------------------------------------------------------------------
# dataclass <-> dict 转换
# ---------------------------------------------------------------------------

# 各分节名 -> dataclass 类型（load_config 的构建表）
_SECTIONS = {
    "bridge": BridgeConfig,
    "chat": ChatProviderConfig,
    "embedding": EmbeddingConfig,
    "persona": PersonaConfig,
    "tts": TtsConfig,
    "reply": ReplyConfig,
    "proactive": ProactiveConfig,
    "mood": MoodConfig,
    "diary": DiaryConfig,
    "mnemosyne": MnemosyneConfig,
    "memes": MemesConfig,
    "console": ConsoleConfig,
    "tools": ToolsConfig,
    "schedule": ScheduleConfig,
    "routine": RoutineConfig,
    "anniversary": AnniversaryConfig,
    "storage": StorageConfig,
    "context": ContextConfig,
}


def _build_section(cls: type, raw: dict, prefix: str) -> Any:
    """按 dataclass 字段白名单从 raw 构造实例；未知键给警告，缺失键走默认值。

    注解在 ``from __future__ import annotations`` 下全是字符串，故用
    ``get_type_hints`` 解析真实类型后再判嵌套 dataclass（如 reply.segmented）。
    """
    import typing

    known = {f.name for f in dataclasses.fields(cls)}
    for key in raw:
        if key not in known:
            if prefix == "chat" and key == "max_context_tokens":
                # 死键显式指引（PERF_SPEC §3-O5/§7.1-3：窗口上限只认 [context] 键）
                warnings.warn(
                    "vl.toml [chat] max_context_tokens 是已删除的死键（已忽略），"
                    "上下文窗口上限请改配 [context] max_context_tokens")
            elif prefix == "chat" and key == "fallback_provider_ids":
                # 假键显式指引（五轮修缮 R4：SPEC §4 预留但从未被读取，智能降级
                # 改由 R2 fallback_* 键组承载，消费方 virtuallover/brain/router.py）
                warnings.warn(
                    "vl.toml [chat] fallback_provider_ids 是已删除的假键（已忽略），"
                    "智能降级请改配 [chat] fallback_* 键组"
                    "（fallback_enabled/fallback_api_base/fallback_model 等）")
            elif prefix == "tts" and key == "always_send_text":
                # 死键显式指引（五轮修缮 R5，照 R4 fallback_provider_ids 先例）：
                # R3 终裁主聊天恒为「文字先发、语音随后附加」双轨（docs/语音.md），
                # 本键对主聊天不生效；主动消息的同类真开关是 [proactive].always_send_text
                warnings.warn(
                    "vl.toml [tts] always_send_text 是已删除的死键（已忽略）：主聊天"
                    "永远是文字先发、语音随后附加，该键不生效；主动消息的语音/文字"
                    "双发开关请配 [proactive] always_send_text")
            elif prefix == "mnemosyne" and key == "injection_position":
                # 死键显式指引（五轮修缮 R5）：注入位置固定 user_prompt prepend
                # （SPEC §7 终裁禁 system），该键从未被读取
                warnings.warn(
                    "vl.toml [mnemosyne] injection_position 是已删除的死键（已忽略）："
                    "记忆注入位置固定 user 侧 prepend（禁 system），不可配置")
            elif prefix == "storage" and key == "napcat_backup_dir":
                # 死键显式指引（五轮修缮 R5）：备份落点由各动作自管
                # （迁移=同目录 .bak-*、一键备份=backup.bat、控制台=AppConfig.backup_dir）
                warnings.warn(
                    "vl.toml [storage] napcat_backup_dir 是已删除的死键（已忽略）："
                    "备份落点无需配置（迁移走同目录 .bak-*，一键备份 scripts/backup.bat）")
            else:
                warnings.warn(f"vl.toml [{prefix}] 存在未知键 {key!r}（已忽略），请检查拼写或 schema_version")
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name in raw:
            val = raw[f.name]
            if dataclasses.is_dataclass(hints.get(f.name)):
                val = _build_section(hints[f.name], val or {}, f"{prefix}.{f.name}")
            kwargs[f.name] = val
    return cls(**kwargs)


def load_config(path: str | Path) -> VlConfig:
    """读取并校验 vl.toml。文件缺失/校验失败抛 ConfigError（中文文案）。"""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(
            f"配置文件不存在：{p}\n"
            "请先执行一次性迁移：python -m virtuallover.tools.config_migrate\n"
            "（vl.toml 由旧栈配置迁移生成，含密钥，存放于 local/ 不入库）"
        )
    with open(p, "rb") as fh:
        raw = tomllib.load(fh)

    for key in raw:
        if key not in (set(_SECTIONS) | {"schema_version", "session_id", "admins",
                                         "whitelist", "log_level"}):
            if key == "timezone":
                # 死键显式指引（五轮修缮 R4，照 [chat] max_context_tokens 先例）
                warnings.warn(
                    "vl.toml 顶层键 timezone 是已删除的死键（已忽略）：diary/proactive "
                    "的时刻基准从未读取过它（一律取本机系统时区），无需迁移")
            else:
                warnings.warn(f"vl.toml 存在未知顶层键 {key!r}（已忽略），请检查拼写或 schema_version")

    cfg = VlConfig(
        schema_version=raw.get("schema_version", SCHEMA_VERSION),
        session_id=raw.get("session_id", ""),
        admins=list(raw.get("admins", [])),
        whitelist=list(raw.get("whitelist", [])),
        log_level=raw.get("log_level", "INFO"),
    )
    for name, cls in _SECTIONS.items():
        if name in raw and isinstance(raw[name], dict):
            setattr(cfg, name, _build_section(cls, raw[name], name))

    _validate(cfg)
    _normalize_tts(cfg)  # VOICE_SPEC §1/§5：base_url 归一 + 默认音色切换 + 映射表合并
    _normalize_perf(cfg)  # PERF_SPEC §7.1-3：存量 vl.toml 归一化（旧默认 100/缺字段兜底）+ 生效值日志
    _normalize_tools(cfg)  # 多模态工程：[tools] 联网搜索键归一化（provider 非法回落 duckduckgo）
    return cfg


def _validate(cfg: VlConfig) -> None:
    """启动校验（fail-fast）。逐项给出中文缺失说明。"""
    if cfg.schema_version != SCHEMA_VERSION:
        raise ConfigError(
            f"vl.toml 的 schema_version={cfg.schema_version!r}，当前框架要求 "
            f"{SCHEMA_VERSION}。请升级框架或重跑迁移脚本生成配置。"
        )
    missing: List[str] = []
    if not cfg.chat.api_base:
        missing.append("chat.api_base（chat provider 端点）")
    if not cfg.chat.api_key:
        missing.append("chat.api_key（chat provider 密钥）")
    if not cfg.chat.model:
        missing.append("chat.model（chat 模型名）")
    if cfg.embedding.enabled:
        # embedding.enabled=false 时整节豁免（记忆功能关闭的降级语义，
        # setup_writeback 跳过 embedding 时写 false——见 EmbeddingConfig 注释）
        if not cfg.embedding.api_base:
            missing.append("embedding.api_base（embedding 端点）")
        if not cfg.embedding.api_key:
            missing.append("embedding.api_key（embedding 密钥）")
        if not cfg.embedding.model:
            missing.append("embedding.model（embedding 模型名）")
    if not cfg.persona.name:
        missing.append("persona.name（人格名）")
    if not cfg.persona.system_prompt:
        missing.append("persona.system_prompt（人格全文）")
    if not cfg.session_id:
        missing.append("session_id（会话模板，如 人格名:FriendMessage:QQ号）")
    if not cfg.admins:
        missing.append("admins（管理员账号列表，/mood /diary /eval 权限判定依赖它）")
    if missing:
        raise ConfigError(
            "vl.toml 必填项缺失（" + str(len(missing)) + " 项）：\n  - "
            + "\n  - ".join(missing)
            + "\n请检查 local/config/vl.toml，或重跑 python -m virtuallover.tools.config_migrate"
        )
    if cfg.bridge.token and len(cfg.bridge.token) < 32:
        raise ConfigError(
            "bridge.token 长度不足 32：请删除该字段留空，由框架重新生成强随机 token"
            "（禁沿用旧栈弱 token，SPEC §3.3）。"
        )


def _normalize_tts(cfg: VlConfig) -> None:
    """[tts] 运行期归一化（VOICE_SPEC §1/§5；只改内存配置，绝不回写用户 vl.toml）。

    - base_url 归一：host.docker.internal / localhost → 127.0.0.1（旧栈容器直连
      残留对原生进程是必炸死路——docs/语音.md §3 文档债的代码面修复）；缺 scheme
      补 http://，缺端口补 5000；改动打 warning（控制台横幅另有同源告警，§3）；
    - 默认音色切换：character 为旧栈单情绪音色 Hutao 时切 Huohua 并 warning 明示
      声音变化（§0 终裁；控制台可一键切回）；
    - emotion_map 合并：用户在 vl.toml 只写部分行时，其余行走默认表（而非落入
      fallback 键），映射语义与终裁表保持一致。
    """
    raw = str(cfg.tts.base_url or "").strip()
    s = raw or "http://127.0.0.1:5000"
    if "://" not in s:
        s = "http://" + s
    parts = urllib.parse.urlsplit(s)
    host = (parts.hostname or "").lower()
    port = parts.port or 5000
    if host in ("host.docker.internal", "localhost"):
        host = "127.0.0.1"
    normalized = urllib.parse.urlunsplit(
        ("http", f"{host}:{port}", parts.path.rstrip("/"), "", ""))
    if normalized != raw:
        cfg.tts.base_url = normalized
        logger.warning(
            "[tts] base_url 已归一化：%r → %r（host.docker.internal/localhost 是"
            "旧栈容器直连残留，原生进程必须直连 127.0.0.1，否则语音必哑）",
            raw, normalized)
    if str(cfg.tts.character or "").strip().lower() == "hutao":
        cfg.tts.character = "Huohua"
        logger.warning(
            "[tts] 声音变化明示：默认音色由旧栈单情绪音色 Hutao 切换为 Huohua"
            "（VOICE_SPEC §0 终裁：Huohua 的 7 个注册情绪承载情绪联动，Hutao 仅"
            " default 单情绪、联动必哑火）。可在控制台语音页一键切回 Hutao。")
    user_map = getattr(cfg.tts, "emotion_map", None)
    if isinstance(user_map, dict) and user_map:
        merged = _default_emotion_map()
        merged.update(user_map)
        cfg.tts.emotion_map = merged
    # R7（FUSION_SPEC §3.4）：retries clamp ≥1——修前 retries=0/负数会撞
    # GsviClient 构造期的 ValueError（voice/gsvi.py「retries 至少为 1」），
    # 语音引擎装配整体失败=她全程哑；clamp 到 1（仅首次尝试，不再重试）
    try:
        retries = int(cfg.tts.retries)
    except (TypeError, ValueError):
        retries = 3
    if retries < 1:
        logger.warning(
            "[tts] retries=%r 非法（至少 1=首次尝试），已 clamp 为 1"
            "（修前会撞 GsviClient 构造 ValueError，语音引擎装配整体失败）",
            cfg.tts.retries)
        retries = 1
    cfg.tts.retries = retries


# ---------------------------------------------------------------------------
# PERF_SPEC §7.1-3 存量归一化（对齐 _normalize_tts 手法：改内存配置 + 告警明示，
# 绝不回写用户 vl.toml）
# ---------------------------------------------------------------------------

# thinking 合法取值（O1：ark/doubao thinking 三态，PERF_SPEC §2）
_THINKING_MODES = ("disabled", "enabled", "auto")
# 旧默认值（O2a 前的现网实测）：存量 vl.toml 显式写 100 的归一化为新默认
_WORDS_COUNT_THRESHOLD_LEGACY = 100
# words_count_threshold 合法范围：低于 10 每句必拆（过碎），高于 500 等于不拆
_WORDS_COUNT_THRESHOLD_MIN = 10
_WORDS_COUNT_THRESHOLD_MAX = 500
# timeout 合法范围：主客户端 5s 起步（防 0/负数把请求瞬间掐死）、后台封顶 300s
_TIMEOUT_MAIN_MIN, _TIMEOUT_MAIN_MAX = 5, 120
_TIMEOUT_BG_MIN, _TIMEOUT_BG_MAX = 10, 300
# [chat].interrupt_window_seconds 合法范围（PERF_SPEC §7.2 回退键）：0=关打断期
_INTERRUPT_WINDOW_MIN, _INTERRUPT_WINDOW_MAX = 0.0, 4.0
# [chat].fallback_max_context_tokens 下限（五轮修缮第二轮）：512=本地小窗口保底，
# 上限沿用 _CONTEXT_TOKENS_MAX（256k 逃生档口径；本地实测 4096）
_FALLBACK_CONTEXT_MIN = 512
# [context].max_context_tokens 合法范围（PERF_SPEC §3-O5）：默认 32768；上限
# 262144=256k 逃生档封顶（豆包 lite 实测窗口），下限 1024 保底可装配
DEFAULT_MAX_CONTEXT_TOKENS = 32768
_CONTEXT_TOKENS_MIN, _CONTEXT_TOKENS_MAX = 1024, 262144
# 旧默认值（O5 前的现网实测，旧代码 dump 残留）：存量 vl.toml 写 256000 的归一化为 32768
_CONTEXT_TOKENS_LEGACY = 256000
# [mood].fragment_position 合法取值（PERF_SPEC §3-O6 + §7.2 回退键）
_MOOD_FRAGMENT_POSITIONS = ("user", "system")
# [mnemosyne].top_k 旧默认值（O5 前的现网实测）：存量 vl.toml 显式写 2 的归一化为 3
_TOP_K_LEGACY = 2


def _normalize_perf(cfg: VlConfig) -> None:
    """性能键存量归一化（PERF_SPEC §7.1-3；只改内存配置，绝不回写用户 vl.toml）。

    - [chat] thinking / bg_thinking：非法取值回退默认并告警（旧配置缺字段时
      dataclass 默认即生效——主回复关思考、后台开思考，O1 语义）；
    - [chat] timeout_main / timeout：clamp 到合法区间并告警（防手滑配 0/负数）；
    - [reply.segmented] words_count_threshold：旧默认 100 归一化为 O2a 新默认 30
      （现网存量配置显式写着 100，不做此步 O2a 裁决落不了地）；越界 clamp；
    - [context] max_context_tokens：clamp 到 [1k, 256k] 并告警（O5 终裁默认
      32768，128k/256k 是显式逃生档）；proactive.segmented 同口径 clamp；
    - [mood] fragment_position：只认 user/system（O6 回退键，非法回退 user）；
    - 最后打印三项生效值（timeout/words_count_threshold/max_context_tokens，
      §7.1-3 启动日志要求），供部署后核对。
    """
    chat = cfg.chat
    # thinking 双开关：只认三态词，非法回退默认（旧配置缺字段走 dataclass 默认）
    mode = str(getattr(chat, "thinking", "") or "").strip().lower()
    if mode not in _THINKING_MODES:
        chat.thinking = "disabled"
        if mode:
            logger.warning(
                "[chat] thinking=%r 非法（可选 disabled/enabled/auto），已回退默认 "
                "disabled（PERF_SPEC O1：主回复关思考压首段延迟）", mode)
    bg_mode = str(getattr(chat, "bg_thinking", "") or "").strip().lower()
    if bg_mode not in _THINKING_MODES:
        chat.bg_thinking = "enabled"
        if bg_mode:
            logger.warning(
                "[chat] bg_thinking=%r 非法（可选 disabled/enabled/auto），已回退默认 "
                "enabled（PERF_SPEC O1：日记/记忆总结保留思考质量）", bg_mode)
    # 超时 clamp：主客户端 5-120s、后台 10-300s
    try:
        main_timeout = int(chat.timeout_main)
    except (TypeError, ValueError):
        main_timeout = 30
    if not (_TIMEOUT_MAIN_MIN <= main_timeout <= _TIMEOUT_MAIN_MAX):
        clamped = min(max(main_timeout, _TIMEOUT_MAIN_MIN), _TIMEOUT_MAIN_MAX)
        logger.warning(
            "[chat] timeout_main=%s 超出合法区间 [%d, %d]，已 clamp 为 %d",
            chat.timeout_main, _TIMEOUT_MAIN_MIN, _TIMEOUT_MAIN_MAX, clamped)
        main_timeout = clamped
    chat.timeout_main = main_timeout
    try:
        bg_timeout = int(chat.timeout)
    except (TypeError, ValueError):
        bg_timeout = 120
    if not (_TIMEOUT_BG_MIN <= bg_timeout <= _TIMEOUT_BG_MAX):
        clamped = min(max(bg_timeout, _TIMEOUT_BG_MIN), _TIMEOUT_BG_MAX)
        logger.warning(
            "[chat] timeout=%s 超出合法区间 [%d, %d]，已 clamp 为 %d",
            chat.timeout, _TIMEOUT_BG_MIN, _TIMEOUT_BG_MAX, clamped)
        bg_timeout = clamped
    chat.timeout = bg_timeout

    # 打断地平线窗口（PERF_SPEC §7.2 回退键）：clamp 到 [0, 4]，0=关=现行为
    raw_window = getattr(chat, "interrupt_window_seconds", 1.5)
    try:
        window = float(raw_window)
    except (TypeError, ValueError):
        window = 1.5
    if not (_INTERRUPT_WINDOW_MIN <= window <= _INTERRUPT_WINDOW_MAX):
        clamped = min(max(window, _INTERRUPT_WINDOW_MIN), _INTERRUPT_WINDOW_MAX)
        logger.warning(
            "[chat] interrupt_window_seconds=%s 超出合法区间 [%.0f, %.0f]，"
            "已 clamp 为 %.2f（0=关打断期）",
            raw_window, _INTERRUPT_WINDOW_MIN, _INTERRUPT_WINDOW_MAX, clamped)
        window = clamped
    chat.interrupt_window_seconds = window

    # 智能降级键归一化（五轮修缮第二轮）：fallback 超时/上下文预算 clamp
    try:
        fb_timeout = int(getattr(chat, "fallback_timeout", 120))
    except (TypeError, ValueError):
        fb_timeout = 120
    if not (_TIMEOUT_BG_MIN <= fb_timeout <= _TIMEOUT_BG_MAX):
        clamped = min(max(fb_timeout, _TIMEOUT_BG_MIN), _TIMEOUT_BG_MAX)
        logger.warning(
            "[chat] fallback_timeout=%s 超出合法区间 [%d, %d]，已 clamp 为 %d",
            chat.fallback_timeout, _TIMEOUT_BG_MIN, _TIMEOUT_BG_MAX, clamped)
        fb_timeout = clamped
    chat.fallback_timeout = fb_timeout
    try:
        fb_ctx = int(getattr(chat, "fallback_max_context_tokens", 4096))
    except (TypeError, ValueError):
        fb_ctx = 4096
    if not (_FALLBACK_CONTEXT_MIN <= fb_ctx <= _CONTEXT_TOKENS_MAX):
        clamped = min(max(fb_ctx, _FALLBACK_CONTEXT_MIN), _CONTEXT_TOKENS_MAX)
        logger.warning(
            "[chat] fallback_max_context_tokens=%s 超出合法区间 [%d, %d]，已 clamp 为 %d",
            chat.fallback_max_context_tokens, _FALLBACK_CONTEXT_MIN,
            _CONTEXT_TOKENS_MAX, clamped)
        fb_ctx = clamped
    chat.fallback_max_context_tokens = fb_ctx

    # words_count_threshold：旧默认 100 → O2a 新默认 30；越界 clamp
    seg = cfg.reply.segmented
    try:
        threshold = int(seg.words_count_threshold)
    except (TypeError, ValueError):
        threshold = 30
    if threshold == _WORDS_COUNT_THRESHOLD_LEGACY:
        seg.words_count_threshold = 30
        logger.warning(
            "[reply.segmented] words_count_threshold=100 是旧默认（O2a 前实测），"
            "已归一化为 30——让 1-3 句回复真正变 2-3 条连发（PERF_SPEC §2 O2a）；"
            "如确需不拆段请显式调高该值")
    elif not (_WORDS_COUNT_THRESHOLD_MIN <= threshold <= _WORDS_COUNT_THRESHOLD_MAX):
        clamped = min(max(threshold, _WORDS_COUNT_THRESHOLD_MIN), _WORDS_COUNT_THRESHOLD_MAX)
        logger.warning(
            "[reply.segmented] words_count_threshold=%s 超出合法区间 [%d, %d]，"
            "已 clamp 为 %d", seg.words_count_threshold,
            _WORDS_COUNT_THRESHOLD_MIN, _WORDS_COUNT_THRESHOLD_MAX, clamped)
        seg.words_count_threshold = clamped

    # [context].max_context_tokens：clamp 到 [1k, 256k]（PERF_SPEC §3-O5 + §7.1-3）。
    # 默认 32768；131072（128k）/262144（256k）是显式逃生档，超出 256k 一律拉回。
    # 存量文件里的 256000 是旧默认（旧代码 dump 机械带出，非用户显式选择），
    # 归一为 32768——否则 O5 终裁对现网落不了地（与 words_count_threshold 100→30
    # 同构；显式要大窗口请写 131072/262144 逃生档值）。
    try:
        max_ctx = int(cfg.context.max_context_tokens)
    except (TypeError, ValueError):
        max_ctx = DEFAULT_MAX_CONTEXT_TOKENS
    if max_ctx == _CONTEXT_TOKENS_LEGACY:
        cfg.context.max_context_tokens = DEFAULT_MAX_CONTEXT_TOKENS
        logger.warning(
            "[context] max_context_tokens=256000 是旧默认（旧代码 dump 残留），"
            "已归一化为 32768（PERF_SPEC §3-O5：窗口外细节以记忆摘要形态存活，"
            "不宣传无损；确需全量保真请显式写逃生档 131072/262144）")
        max_ctx = DEFAULT_MAX_CONTEXT_TOKENS
    elif not (_CONTEXT_TOKENS_MIN <= max_ctx <= _CONTEXT_TOKENS_MAX):
        clamped = min(max(max_ctx, _CONTEXT_TOKENS_MIN), _CONTEXT_TOKENS_MAX)
        logger.warning(
            "[context] max_context_tokens=%s 超出合法区间 [%d, %d]，已 clamp 为 %d"
            "（默认 32768；128k/256k 是逃生档，窗口外细节以记忆摘要形态存活）",
            cfg.context.max_context_tokens,
            _CONTEXT_TOKENS_MIN, _CONTEXT_TOKENS_MAX, clamped)
        max_ctx = clamped
    cfg.context.max_context_tokens = max_ctx

    # proactive 分段阈值同口径 clamp（与主回复共用 SegmentedReplyConfig 结构）
    pseg = cfg.proactive.segmented
    try:
        p_threshold = int(pseg.words_count_threshold)
    except (TypeError, ValueError):
        p_threshold = 80
    if not (_WORDS_COUNT_THRESHOLD_MIN <= p_threshold <= _WORDS_COUNT_THRESHOLD_MAX):
        p_clamped = min(max(p_threshold, _WORDS_COUNT_THRESHOLD_MIN), _WORDS_COUNT_THRESHOLD_MAX)
        logger.warning(
            "[proactive.segmented] words_count_threshold=%s 超出合法区间 [%d, %d]，"
            "已 clamp 为 %d", pseg.words_count_threshold,
            _WORDS_COUNT_THRESHOLD_MIN, _WORDS_COUNT_THRESHOLD_MAX, p_clamped)
        pseg.words_count_threshold = p_clamped

    # [mood] fragment_position：只认 user/system（PERF_SPEC §3-O6 + §7.2 回退键）
    position = str(getattr(cfg.mood, "fragment_position", "") or "").strip().lower()
    if position not in _MOOD_FRAGMENT_POSITIONS:
        if position:
            logger.warning(
                "[mood] fragment_position=%r 非法（可选 user/system），已回退默认 "
                "user（O6：mood 片段在 user 侧是 prompt 隐式缓存的命中开关；"
                "system=旧行为回退）", position)
        cfg.mood.fragment_position = "user"

    # [mnemosyne] top_k：旧默认 2 → O5 兜底补丁 3（存量 vl.toml 与迁移产物都
    # 显式写 2，不改默认值必须在此归一，否则 O5 落不了地）；越界 clamp [1, 10]
    try:
        top_k = int(cfg.mnemosyne.top_k)
    except (TypeError, ValueError):
        top_k = 3
    if top_k == _TOP_K_LEGACY:
        cfg.mnemosyne.top_k = 3
        logger.warning(
            "[mnemosyne] top_k=2 是旧默认（O5 前实测），已归一化为 3——"
            "检索注入多带一条近旁记忆兜底（PERF_SPEC §3-O5）")
    elif not (1 <= top_k <= 10):
        clamped = min(max(top_k, 1), 10)
        logger.warning(
            "[mnemosyne] top_k=%s 超出合法区间 [1, 10]，已 clamp 为 %d",
            cfg.mnemosyne.top_k, clamped)
        cfg.mnemosyne.top_k = clamped

    # 启动日志打印生效值（§7.1-3：timeout/words_count_threshold/max_context_tokens；
    # 五轮修缮第二轮追加智能降级开关与本地大脑端点）
    logger.info(
        "[perf] 生效值：chat.timeout_main=%ds chat.timeout=%ds "
        "reply.segmented.words_count_threshold=%d context.max_context_tokens=%d "
        "thinking=%s bg_thinking=%s first_gap=%r interrupt_window=%.2fs "
        "fallback=%s（%s model=%s ctx=%d）",
        chat.timeout_main, chat.timeout, seg.words_count_threshold,
        cfg.context.max_context_tokens, chat.thinking, chat.bg_thinking,
        seg.first_gap, chat.interrupt_window_seconds,
        "开" if chat.fallback_enabled else "关",
        chat.fallback_api_base, chat.fallback_model, chat.fallback_max_context_tokens,
    )


# ---------------------------------------------------------------------------
# [tools] 联网搜索归一化（多模态能力工程；照 _normalize_perf 手法：改内存配置 +
# 告警明示，绝不回写用户 vl.toml）
# ---------------------------------------------------------------------------

# [tools].search_provider 合法取值（多模态能力工程：duckduckgo=免费无 key，
# tavily=有 key 更准；其它取值回落 duckduckgo 并告警）
_SEARCH_PROVIDERS = ("bing", "duckduckgo", "tavily")


def _normalize_tools(cfg: VlConfig) -> None:
    """[tools] 运行期归一化：search_provider 非法回落 bing（只改内存配置）。"""
    provider = str(getattr(cfg.tools, "search_provider", "") or "").strip().lower()
    if provider not in _SEARCH_PROVIDERS:
        if provider:
            logger.warning(
                "[tools] search_provider=%r 非法（可选 bing/duckduckgo/tavily），已回落 "
                "bing（免 key 中国直连；要用 Tavily 请把该键改为 'tavily' 并配 "
                "tavily_api_key）", provider)
        cfg.tools.search_provider = "bing"
        return
    cfg.tools.search_provider = provider


# ---------------------------------------------------------------------------
# TOML 序列化（tomllib 只读不写，这里自带一个面向本配置树的小型写出器）
# ---------------------------------------------------------------------------

def _toml_escape(s: str) -> str:
    """TOML basic string 转义（双引号单行形态，换行等控制字符全部 \\uXXXX/\\n 化）。"""
    out: List[str] = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u%04X" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_value(v: Any) -> str:
    """标量/字符串列表 → TOML 值文本。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _toml_escape(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        # 动态键（mood.keyword_groups 的 sweet/praise/rude）→ inline table
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + " }"
    raise ConfigError(f"无法序列化为 TOML 的类型：{type(v).__name__}")


def _dump_table(name: str, obj: Any, lines: List[str]) -> None:
    """输出一个 [table]：先标量后子表——TOML 语法要求键值归属其前最近的表头。"""
    scalars, subs = [], []
    for f in dataclasses.fields(obj):
        (subs if dataclasses.is_dataclass(getattr(obj, f.name)) else scalars).append(f)
    if name:
        lines.append("")
        lines.append(f"[{name}]")
    for f in scalars:
        lines.append(f"{f.name} = {_toml_value(getattr(obj, f.name))}")
    for f in subs:
        _dump_table(f"{name}.{f.name}" if name else f.name, getattr(obj, f.name), lines)


def dump_config(cfg: VlConfig) -> str:
    """配置树 → TOML 文本。根标量在前，随后各分节表（字段顺序=dataclass 声明序）。"""
    lines: List[str] = []
    scalars, subs = [], []
    for f in dataclasses.fields(cfg):
        (subs if dataclasses.is_dataclass(getattr(cfg, f.name)) else scalars).append(f)
    for f in scalars:
        lines.append(f"{f.name} = {_toml_value(getattr(cfg, f.name))}")
    for f in subs:
        _dump_table(f.name, getattr(cfg, f.name), lines)
    return "\n".join(lines) + "\n"


def ensure_bridge_token(cfg: VlConfig, path: str | Path) -> str:
    """桥 token 缺失时生成强随机值并整体回写 vl.toml（生成物文件，重写不保留注释）。

    返回生效 token。token_urlsafe(32) 生成 43 字符 URL 安全随机串（SPEC §3.3）。
    """
    if cfg.bridge.token:
        return cfg.bridge.token
    cfg.bridge.token = secrets.token_urlsafe(32)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_config(cfg), encoding="utf-8")
    return cfg.bridge.token
