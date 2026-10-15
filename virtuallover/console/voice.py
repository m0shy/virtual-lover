# -*- coding: utf-8 -*-
"""管理控制台 —— 语音页（voice）。VOICE_SPEC §3 控制台语音页（第 7 页）后端逻辑层。

职责与边界：
- /api/voice/* 端点的纯逻辑：状态卡、试听、启停、情绪映射表读写、base_url 一键修正；
- 语音本体一律走 voice_manager（VOICE_SPEC §1 VoiceManager）——试听「共用缓存与
  串行锁」的唯一实现点是 manager 的 preview/synthesize；本模块绝不自建缓存、
  绝不开第二把合成锁（仅一把控制台侧线程锁串行化并发试听请求本身）；
- 旧 /api/status 的语音 HTTP 探针（core.vl_status）原样保留；本页状态卡数据源换成
  voice_manager 状态（lifecycle 优先——冷启动 15s 内 HTTP 必误报，禁止裸探针上卡）；
- 严禁任意 URL 代理：本模块只对 vl.toml 的 tts.base_url 做归一化改写（一键修正），
  绝不向任何用户可控地址转发请求；试听文本/情绪只作为引擎参数由 manager 携带。

对 voice_manager 的鸭类型契约（全部可选；缺失时端点明确降级为 503/409，绝不假装在线）：
- ``status() -> dict``：状态卡第一数据源。识别键（全部可选，扁平结构）：
  lifecycle / ownership / character / registered_emotions / cache_count /
  cache_bytes / cache_max_entries / cache_max_bytes / pid / started_at /
  auto_start / base_url / log_path / last_error。
  registered_emotions 可为 list[str]（当前音色）或 dict{角色: [情绪]}（/character_list
  注册表原形，取当前音色条目）。
- ``preview(text, emotion) -> bytes | None``：试听合成（内部共享缓存 + 全局单飞锁）。
  可为同步或异步函数；**若内部依赖常驻事件循环对象（asyncio.Lock 等），必须实现为
  同步方法（内部 run_coroutine_threadsafe 到主循环），否则控制台线程用 asyncio.run
  驱动会与主循环争抢同一把锁**。返回 None 表示引擎未就绪/合成失败（降级语义）。
- ``start() / stop()``：启停；同步或异步皆可。stop 前本模块做 ownership 门禁：
  仅 ``status().ownership == "owned"`` 才放行（收编/外部进程绝不杀，宁可不杀不可误杀；
  status 不可得时同样拒绝）。
- ``reload_mapping()``：情绪映射落盘后的热加载钩子（可缺省，缺省仅落盘不重载）。
- ``apply_config()``：base_url 一键修正后的可选热应用钩子（可缺省）。

情绪映射表的存储（结构化安全写，参照 napcat_switch 模式：校验 + .bak + 原子替换 +
热加载）：
- 默认表 = VOICE_SPEC §0/§1 终裁值（硬编码，vl.toml [tts].emotion_map 若存在则夹在
  中间层），控制台改写落在 ``local/vl_data/voice/emotion_map.json``（覆盖层，
  与 voice-engine.pid 同目录）；生效表 = 默认表 ← vl.toml ← 覆盖文件。
- 覆盖文件 schema：{"version": 1, "updated_at": <epoch>, "map": {<mood>: <emotion>}}；
  读取容忍扁平形态（顶层直接是 mood→emotion）；损坏按缺失处理（只读不修）。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import re
import shutil
import threading
import time
import tomllib
import urllib.parse
from pathlib import Path

from virtuallover.config import ConfigError, dump_config, load_config
from virtuallover.soul import mood as mood_module
from virtuallover.voice.mapping import DEFAULT_EMOTION_MAP

from . import core

logger = logging.getLogger("virtuallover.console.voice")

__all__ = [
    "VoicePageService", "DEFAULT_EMOTION_MAP", "MOOD_KEY_ORDER",
    "PREVIEW_MAX_CHARS", "PREVIEW_MAX_AUDIO_BYTES", "emotion_map_path",
    "is_local_base_url",
]

# ---------------------------------------------------------------------------
# 契约常量（与 VOICE_SPEC §3 逐字一致）
# ---------------------------------------------------------------------------

# 试听上限：文本 ≤200 字；产物（合成 wav）≤8MB
PREVIEW_MAX_CHARS = 200
PREVIEW_MAX_AUDIO_BYTES = 8 * 1024 * 1024
# 映射值（GSVI 情绪名）长度上限；服务端对字符串参数做 .lower()，中文不受影响
EMOTION_VALUE_MAX_CHARS = 64
# 状态卡日志尾部行数与单行截断
LOG_TAIL_LINES = 12
LOG_TAIL_MAX_LINE_CHARS = 500
LOG_TAIL_MAX_BYTES = 8 * 1024

# 心情键次序（7 个有效状态 + fallback 兜底；顺序即映射表卡行序，键集与
# voice.mapping.DEFAULT_EMOTION_MAP 单一来源对账——导出顺序即声明序）
MOOD_KEY_ORDER = tuple(DEFAULT_EMOTION_MAP)

# 默认映射表单一来源：virtuallover.voice.mapping（VOICE_SPEC §1 终裁表；
# vl.toml [tts].emotion_map 与控制台覆盖文件依次叠加，见 effective_emotion_map）

# lifecycle 状态机文案（VOICE_SPEC §1：not_started→starting→ready→suspected_down→restarting|degraded）
LIFECYCLE_ZH = {
    "not_started": "未启动",
    "starting": "启动中",
    "ready": "就绪",
    "suspected_down": "疑似掉线",
    "restarting": "重启中",
    "degraded": "降级（熔断冷却）",
    "unknown": "未知",
}

# 进程归属文案（adopt-first：端口应答即收编，绝不杀外部实例）
OWNERSHIP_ZH = {
    "owned": "框架自有进程",
    "adopted": "由外部窗口启动（已收编）",
    "external": "外部进程",
    "none": "无进程",
}

# 状态卡从 manager.status() 认领的键（白名单合并，多余键忽略；
# registered_emotions 合并后在 status() 里再归一化）
_MANAGER_STATUS_KEYS = ("lifecycle", "ownership", "character", "base_url",
                        "log_path", "last_error", "pid", "started_at",
                        "uptime_s", "auto_start", "registered_emotions")
_MANAGER_CACHE_KEYS = {"cache_count": "count", "cache_bytes": "bytes",
                       "cache_max_entries": "max_entries", "cache_max_bytes": "max_bytes"}

# base_url 视为本机的 host 白名单（横幅告警只对非本机地址触发）
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# base_url 一键修正的目标值（VOICE_SPEC §5：归一化 127.0.0.1:5000）
_FIX_BASE_URL = "http://127.0.0.1:5000"
# 日志"最近错误行"匹配（行级，宽松命中）
_ERROR_LINE_RE = re.compile(r"error|exception|traceback|fatal|失败|错误|致命", re.IGNORECASE)


def emotion_map_path(repo_root: "Path | str") -> Path:
    """情绪映射覆盖文件的落点：<root>/local/vl_data/voice/emotion_map.json。

    与 voice-engine.pid 同目录（VOICE_SPEC §1：PID 落 local/vl_data/voice/）。
    """
    return Path(repo_root) / "local" / "vl_data" / "voice" / "emotion_map.json"


def is_local_base_url(base_url: str) -> bool:
    """base_url 是否指向本机（空值视为无需告警）。host.docker.internal 残留必炸。"""
    host = urllib.parse.urlsplit(str(base_url or "")).hostname
    if not host:
        return True
    return host.lower() in _LOCAL_HOSTS


def raw_tts_base_url(repo_root: "Path | str") -> str:
    """读 vl.toml [tts].base_url 原文（文件层，未归一化）。

    load_config 会在内存里把 host.docker.internal/localhost 归一化为 127.0.0.1
    （config.py _normalize_tts，改动只进内存、不改文件），运行时 cfg 看不到
    文件残留——横幅与一键修正必须看文件层（config 工程师注释明示
    「控制台横幅另有同源告警」）。文件缺失/解析失败返回 ""（上层回退内存值）。
    """
    path = Path(repo_root) / "local" / "config" / "vl.toml"
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, ValueError):
        return ""
    tts = raw.get("tts")
    if not isinstance(tts, dict):
        return ""
    value = tts.get("base_url")
    return str(value) if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# 小工具：同步/异步双态调用、日志尾部、原子写
# ---------------------------------------------------------------------------

def _call_any(obj: object, name: str, *args) -> "tuple[object, bool]":
    """调用 obj 的可选方法，兼容同步与协程；方法缺失返回 (None, False)。

    控制台跑在 ThreadingHTTPServer 的工作线程里，无事件循环——协程结果用
    asyncio.run 就地驱动（契约见模块 docstring：依赖常驻循环对象的方法必须
    实现为同步）。
    """
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None, False
    result = fn(*args)
    if inspect.isawaitable(result):
        async def _await() -> object:
            return await result
        return asyncio.run(_await()), True
    return result, True


def read_log_tail(path: "Path | str", *, lines: int = LOG_TAIL_LINES) -> "tuple[list[str], str]":
    """读日志文件尾部 lines 行（每行截断）+ 最后一条疑似错误行。

    文件缺失/不可读返回 ([], "")——绝不抛异常，状态卡降级展示。
    """
    p = Path(path)
    if not str(path) or not p.is_file():
        return [], ""
    try:
        with p.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL_MAX_BYTES))
            data = fh.read(LOG_TAIL_MAX_BYTES)
    except OSError as exc:
        logger.warning("语音引擎日志尾部读取失败（%s）：%s", p, exc)
        return [], ""
    text = data.decode("utf-8", errors="replace")
    if size > LOG_TAIL_MAX_BYTES:
        text = text.split("\n", 1)[-1]  # 首行多半是半截，丢弃
    all_lines = [ln.strip("\r") for ln in text.split("\n") if ln.strip()]
    tail = [ln[-LOG_TAIL_MAX_LINE_CHARS:] for ln in all_lines[-lines:]]
    last_error = ""
    for ln in reversed(all_lines):
        if _ERROR_LINE_RE.search(ln):
            last_error = ln[-LOG_TAIL_MAX_LINE_CHARS:]
            break
    return tail, last_error


def _atomic_write_text(path: Path, text: str, *, backup: bool, clock: "callable") -> str:
    """原子写文本文件：可选 .bak-<时间戳> 备份 → tmp 落盘 → os.replace。

    返回备份文件名（无备份返回 ""）。写失败抛 OSError，由调用方转 ApiError。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_name = ""
    if backup and path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
        bak = path.with_name(path.name + ".bak-" + stamp)
        shutil.copy2(path, bak)
        backup_name = bak.name
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return backup_name


# ---------------------------------------------------------------------------
# 情绪映射表：读 / 校验 / 写
# ---------------------------------------------------------------------------

def read_emotion_override(path: "Path | str") -> "dict | None":
    """读覆盖文件里的 map 层；缺失/损坏/形态非法一律 None（只读不修，不抛异常）。"""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("情绪映射覆盖文件损坏，按缺失处理（%s）：%s", p, exc)
        return None
    if isinstance(raw, dict) and isinstance(raw.get("map"), dict):
        raw = raw["map"]
    if not isinstance(raw, dict):
        return None
    clean: dict = {}
    for key, value in raw.items():
        if key in MOOD_KEY_ORDER and isinstance(value, str) and value.strip():
            clean[key] = value.strip()
    return clean or None


def effective_emotion_map(cfg_tts: object, override: "dict | None") -> dict:
    """生效映射表 = 默认表 ← vl.toml [tts].emotion_map（若字段存在）← 覆盖文件。"""
    merged = dict(DEFAULT_EMOTION_MAP)
    toml_layer = getattr(cfg_tts, "emotion_map", None)
    if isinstance(toml_layer, dict):
        for key, value in toml_layer.items():
            if key in MOOD_KEY_ORDER and isinstance(value, str) and value.strip():
                merged[key] = value.strip()
    if override:
        merged.update(override)
    return {key: merged[key] for key in MOOD_KEY_ORDER}


def validate_emotion_map_payload(payload: dict) -> dict:
    """校验 PUT /api/voice/emotion-map 请求体，返回清洗后的（允许部分）映射。

    校验失败抛 ApiError(400)。值不限于注册情绪：注册表只在 UI 层标黄提示
    （引擎未就绪时也要能保存），服务端只做结构校验。
    """
    mapping = payload.get("map")
    if not isinstance(mapping, dict):
        raise core.ApiError(400, '请求体必须是 {"map": {<心情>: <情绪>}} 形态的 JSON 对象')
    if len(mapping) > 32:
        raise core.ApiError(400, "映射键数量超上限（最多 32 个）")
    clean: dict = {}
    for key, value in mapping.items():
        if key not in MOOD_KEY_ORDER:
            raise core.ApiError(
                400, f"非法映射键 {key!r}：只允许 14 个有效心情 "
                     f"happy/excited/angry/sad/lonely/shy/calm/"
                     f"wronged/lost/jealous/expect/sweet/tsundere/secure + fallback")
        if not isinstance(value, str) or not value.strip():
            raise core.ApiError(400, f"映射值必须是非空字符串：{key}")
        value = value.strip()
        if len(value) > EMOTION_VALUE_MAX_CHARS:
            raise core.ApiError(
                400, f"映射值超过 {EMOTION_VALUE_MAX_CHARS} 字符上限：{key}")
        clean[key] = value
    return clean


def mood_labels() -> dict:
    """心情中文标签：复用 soul.mood 的 MOOD_ZH（禁复制映射表），fallback 兜底标签本地补。"""
    labels = {key: str(mood_module.MOOD_ZH.get(key, key)) for key in MOOD_KEY_ORDER}
    labels["fallback"] = "兜底"
    return labels


def normalize_registered(raw: object, character: str) -> "tuple[list[str], bool]":
    """归一化注册情绪：list 直收；dict（/character_list 原形）取当前音色条目。

    返回 (情绪列表, 注册表是否已知)。未知（manager 未提供/引擎未就绪）时列表为空。
    """
    if isinstance(raw, dict):
        values: "list | None" = None
        if character and isinstance(raw.get(character), list):
            values = raw[character]
        else:
            union: "list[str]" = []
            for item in raw.values():
                if isinstance(item, list):
                    for emo in item:
                        if isinstance(emo, str) and emo not in union:
                            union.append(emo)
            values = union
        known = True
    elif isinstance(raw, list):
        values = raw
        known = True
    else:
        values, known = [], False
    clean: "list[str]" = []
    for emo in values or []:
        if isinstance(emo, str) and emo.strip() and emo not in clean:
            clean.append(emo.strip())
        if len(clean) >= 256:
            break
    return clean, known


# ---------------------------------------------------------------------------
# 页面服务
# ---------------------------------------------------------------------------

class VoicePageService:
    """/api/voice/* 的应用服务：状态卡 / 试听 / 启停 / 映射表 / base_url 修正。

    runtime 为鸭类型装配上下文（AppContext）：请求时实时读 voice_manager 槽位
    （getattr，槽位缺失/未装配一律如实降级）。repo_root 缺失时映射表写路径不可用。
    """

    def __init__(self, runtime: object, *, clock: "callable | None" = None) -> None:
        self.runtime = runtime
        self.clock = clock or time.time
        # 控制台侧试听串行锁：串行化并发试听请求本身；与聊天管线的全局单飞
        # 由 voice_manager 内部的锁负责（本模块绝不开第二把合成锁）
        self._preview_lock = threading.Lock()

    # -------------------------------------------------------------- 内部取值

    def _root(self) -> "Path | None":
        root = getattr(self.runtime, "repo_root", None)
        return Path(root) if root else None

    def _tts_cfg(self) -> object:
        return getattr(getattr(self.runtime, "cfg", None), "tts", None)

    def _manager(self) -> object:
        if self.runtime is None:
            return None
        return getattr(self.runtime, "voice_manager", None)

    def _require_manager(self) -> object:
        manager = self._manager()
        if manager is None:
            raise core.ApiError(503, "语音框架未装配（voice_manager 缺失），语音页动作不可用")
        return manager

    def _manager_status(self, manager: object) -> "dict | None":
        """manager.status() 的安全读取：缺失/非 dict/抛异常一律 None（降级路径各自处理）。"""
        try:
            raw, present = _call_any(manager, "status")
        except Exception as exc:  # noqa: BLE001 —— 状态读取失败不拖垮状态卡
            logger.warning("voice_manager.status() 抛异常，按不可得处理：%s", exc)
            return None
        return raw if present and isinstance(raw, dict) else None

    # ------------------------------------------------------------ 状态卡

    def status(self) -> dict:
        """/api/voice/status 响应体：lifecycle 优先，注册情绪与横幅数据齐备。"""
        cfg_tts = self._tts_cfg()
        base_url = str(getattr(cfg_tts, "base_url", "") or "")
        manager = self._manager()
        payload: dict = {
            "ok": True,
            "enabled": bool(getattr(cfg_tts, "enabled", False)),
            "assembled": manager is not None,
            "lifecycle": "not_started",
            "lifecycle_zh": LIFECYCLE_ZH["not_started"],
            "ownership": "none",
            "ownership_zh": OWNERSHIP_ZH["none"],
            "character": str(getattr(cfg_tts, "character", "") or ""),
            "registered_emotions": [],
            "emotions_known": False,
            "single_emotion": False,
            "cache": {"count": 0, "bytes": 0, "max_entries": 0, "max_bytes": 0},
            "pid": None,
            "started_at": None,
            "uptime_s": None,
            "auto_start": None,
            "base_url": base_url,
            "base_url_raw": "",
            "base_url_local": True,
            "log_path": "",
            "log_tail": [],
            "last_error": "",
            "preview_max_chars": PREVIEW_MAX_CHARS,
            "preview_max_bytes": PREVIEW_MAX_AUDIO_BYTES,
            "trigger_probability": self._current_trigger_probability(),
        }

        if manager is not None:
            raw = self._manager_status(manager)
            if raw is None:
                # manager 在但不可观测：lifecycle 如实报未知，绝不假装在线
                payload["lifecycle"] = "unknown"
                payload["lifecycle_zh"] = LIFECYCLE_ZH["unknown"]
            else:
                for key in _MANAGER_STATUS_KEYS:
                    if key in raw and raw[key] is not None:
                        payload[key] = raw[key]
                for src, dst in _MANAGER_CACHE_KEYS.items():
                    if isinstance(raw.get(src), (int, float)):
                        payload["cache"][dst] = int(raw[src])
                lifecycle = str(payload.get("lifecycle") or "")
                if lifecycle not in LIFECYCLE_ZH:
                    payload["lifecycle"] = "unknown"
                payload["lifecycle_zh"] = LIFECYCLE_ZH[payload["lifecycle"]]
                # 字符串槽位统一收敛（manager 侧字段形态不做假设）
                for key in ("character", "base_url", "log_path", "last_error"):
                    payload[key] = str(payload.get(key) or "")
                ownership = str(payload.get("ownership") or "")
                payload["ownership_zh"] = OWNERSHIP_ZH.get(ownership, ownership or "未知")
                started_at = payload.get("started_at")
                if isinstance(started_at, (int, float)) and started_at > 0:
                    # 回退口径：manager 只给 started_at 时按 wall clock 求差。
                    # 真 voice_engine 会自带 uptime_s（lifecycle 内部同钟求差，
                    # started_at 是 monotonic 值——禁止跨基准相减），已在上面
                    # 白名单合并里优先覆盖；此分支只为旧形态/fake 兜底。
                    if not isinstance(payload.get("uptime_s"), (int, float)):
                        payload["uptime_s"] = max(0.0, float(self.clock()) - float(started_at))

        # 横幅判断优先看文件层残留（load_config 只归一化内存值，文件可能仍带
        # host.docker.internal/localhost——config 工程师注释明示横幅同源告警）；
        # 无文件层信息时回退内存/manager 值
        root = self._root()
        raw_file = raw_tts_base_url(root) if root else ""
        payload["base_url_raw"] = raw_file
        check_value = raw_file or str(payload.get("base_url") or "")
        payload["base_url_local"] = is_local_base_url(check_value) if check_value else True

        registered, known = normalize_registered(
            payload.pop("registered_emotions", None), payload["character"])
        payload["registered_emotions"] = registered
        payload["emotions_known"] = known
        payload["single_emotion"] = bool(known and len(registered) <= 1)

        # 日志尾部：manager 给的路径优先，缺省用运行根约定路径
        log_path = str(payload.get("log_path") or "")
        if not log_path and self._root() is not None:
            log_path = str(self._root() / "local" / "vl_data" / "logs" / "voice-engine.log")
        payload["log_path"] = log_path
        tail, last_error = read_log_tail(log_path)
        payload["log_tail"] = tail
        if last_error and not payload.get("last_error"):
            payload["last_error"] = last_error

        # 映射表生效值 + 注册命中标记（未命中 → 前端标黄"联动不会生效"）
        override = read_emotion_override(
            emotion_map_path(self._root())) if self._root() else None
        eff = effective_emotion_map(cfg_tts, override)
        payload["emotion_map"] = eff
        payload["emotion_hits"] = {
            key: (value in registered) if known else None
            for key, value in eff.items()
        }
        payload["defaults"] = dict(DEFAULT_EMOTION_MAP)
        payload["mood_zh"] = mood_labels()
        return payload

    # ------------------------------------------------------------ 试听

    def preview(self, payload: dict) -> dict:
        """POST /api/voice/preview：文本 ≤200 字 + 情绪 → base64 JSON（产物 ≤8MB）。

        合成走 voice_manager 的 preview 契约（共享缓存与全局单飞锁）；manager
        缺失/未实现/返回空一律明确报错，绝不静默降级到旁路合成。
        """
        if not isinstance(payload, dict):
            raise core.ApiError(400, "请求体必须是 JSON 对象")
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise core.ApiError(400, "试听文本不能为空")
        text = text.strip()
        if len(text) > PREVIEW_MAX_CHARS:
            raise core.ApiError(
                400, f"试听文本超过 {PREVIEW_MAX_CHARS} 字上限（当前 {len(text)} 字）")
        emotion = payload.get("emotion", "")
        if not isinstance(emotion, str):
            raise core.ApiError(400, "emotion 必须是字符串")
        emotion = emotion.strip()
        if len(emotion) > EMOTION_VALUE_MAX_CHARS:
            raise core.ApiError(
                400, f"emotion 超过 {EMOTION_VALUE_MAX_CHARS} 字符上限")

        manager = self._require_manager()
        started = time.perf_counter()
        with self._preview_lock:
            try:
                audio, present = _call_any(manager, "preview", text, emotion)
            except Exception as exc:  # noqa: BLE001 —— 合成异常转明确错误
                logger.warning("试听合成异常：%s", exc)
                raise core.ApiError(503, f"试听合成失败：{exc}") from exc
        if not present:
            raise core.ApiError(
                503, "voice_manager 未实现 preview(text, emotion)（控制台↔语音管理器"
                     "契约见 console/voice.py 模块 docstring），试听不可用")
        if audio is None:
            raise core.ApiError(503, "合成失败：语音引擎未就绪或本次合成被降级（返回空）")
        if not isinstance(audio, (bytes, bytearray)):
            raise core.ApiError(503, "voice_manager.preview 返回类型异常（应为 wav 字节或 None）")
        audio = bytes(audio)
        if len(audio) > PREVIEW_MAX_AUDIO_BYTES:
            raise core.ApiError(
                413, f"试听产物 {len(audio)} 字节超过 {PREVIEW_MAX_AUDIO_BYTES} 字节（8MB）上限")
        elapsed_ms = int(round((time.perf_counter() - started) * 1000))
        return {
            "ok": True,
            "mime": "audio/wav",
            "audio_base64": base64.b64encode(audio).decode("ascii"),
            "bytes": len(audio),
            "chars": len(text),
            "emotion": emotion,
            "elapsed_ms": elapsed_ms,
        }

    # ------------------------------------------------------------ 启停

    def start(self) -> dict:
        """/api/voice/start：请求 voice_manager 启动引擎（冷启就绪预算 300s）。"""
        manager = self._require_manager()
        try:
            _, present = _call_any(manager, "start")
        except Exception as exc:  # noqa: BLE001
            raise core.ApiError(503, f"启动请求失败：{exc}") from exc
        if not present:
            raise core.ApiError(503, "voice_manager 未实现 start()")
        return {"ok": True, "started": True,
                "note": "已请求启动：冷启动就绪可能 1-2 分钟（预算 300s），状态以生命周期为准"}

    def stop(self) -> dict:
        """/api/voice/stop：owned 直接停；external/adopted 走命令行核验杀（本机单用户信任域）。

        v2 修正：引擎通常由 一键启动.bat → service.py start 外部拉起（与 vl 同级而非
        vl 子进程），vl 生命周期探测到后标记 external/adopted——旧门禁 409 拒停导致
        用户无法通过控制台关闭语音引擎。本机单用户（127.0.0.1-only）信任域下，
        「PID+命令行含 entry.py」核验杀与 service.py stop 等价安全。
        """
        manager = self._require_manager()
        raw = self._manager_status(manager)
        ownership = str(raw.get("ownership") or "") if raw else ""
        if ownership == "owned":
            try:
                _, present = _call_any(manager, "stop")
            except Exception as exc:  # noqa: BLE001
                raise core.ApiError(503, f"停止请求失败：{exc}") from exc
            if not present:
                raise core.ApiError(503, "voice_manager 未实现 stop()")
            return {"ok": True, "stopped": True,
                    "note": "已请求停止（PID 与命令行核验由 voice_manager 负责）"}

        # external / adopted / unknown：命令行核验杀（与 service.py stop 同款）
        killed = self._kill_voice_process_by_cmdline()
        if killed > 0:
            return {"ok": True, "stopped": True, "killed": killed,
                    "note": f"已停止 {killed} 个语音引擎进程（命令行含 voice_engine_entry.py 核验）"}
        return {"ok": True, "stopped": True, "killed": 0,
                "note": "语音引擎已不在运行"}

    @staticmethod
    def _kill_voice_process_by_cmdline() -> int:
        """按命令行含 voice_engine_entry.py 核验杀语音引擎（与 service.py gsvi_stop 同款）。"""
        import json as _json
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process | "
             "Where-Object {$_.CommandLine -match 'voice_engine_entry\\.py'} | "
             "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"],
            capture_output=True).stdout
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        try:
            rows = _json.loads(out or "[]")
        except ValueError:
            return 0
        if isinstance(rows, dict):
            rows = [rows]
        n = 0
        for r in rows:
            pid = str(r.get("ProcessId", ""))
            if pid.isdigit():
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                n += 1
        return n

    # ------------------------------------------------------------ 映射表

    def get_emotion_map(self) -> dict:
        """/api/voice/emotion-map GET：生效表 + 默认表 + 注册命中标记。"""
        cfg_tts = self._tts_cfg()
        root = self._root()
        path = emotion_map_path(root) if root else None
        override = read_emotion_override(path) if path else None
        eff = effective_emotion_map(cfg_tts, override)
        manager = self._manager()
        registered: "list[str]" = []
        known = False
        if manager is not None:
            raw = self._manager_status(manager) or {}
            registered, known = normalize_registered(
                raw.get("registered_emotions"), str(raw.get("character") or ""))
        return {
            "ok": True,
            "map": eff,
            "defaults": dict(DEFAULT_EMOTION_MAP),
            "mood_zh": mood_labels(),
            "mood_keys": list(MOOD_KEY_ORDER),
            "registered": registered,
            "emotions_known": known,
            "hits": {key: (value in registered) if known else None
                     for key, value in eff.items()},
            "path": str(path) if path else "",
            "file_exists": bool(path and path.is_file()),
            "vl_toml_layer": isinstance(getattr(cfg_tts, "emotion_map", None), dict),
        }

    def put_emotion_map(self, payload: dict) -> dict:
        """/api/voice/emotion-map PUT：校验 → .bak 备份 → 原子替换 → 热加载钩子。

        部分键合法（合并进当前生效表后整体落盘 8 键）；热加载经
        voice_manager.reload_mapping()（缺省时仅落盘，响应里如实标注）。
        """
        if not isinstance(payload, dict):
            raise core.ApiError(400, "请求体必须是 JSON 对象")
        clean = validate_emotion_map_payload(payload)
        root = self._root()
        if root is None:
            raise core.ApiError(503, "运行根未配置（repo_root 缺失），无法保存映射表")
        path = emotion_map_path(root)
        current = effective_emotion_map(self._tts_cfg(), read_emotion_override(path))
        merged = {**current, **clean}
        record = {"version": 1, "updated_at": float(self.clock()), "map": merged}
        try:
            backup = _atomic_write_text(
                path, json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                backup=True, clock=self.clock)
        except OSError as exc:
            raise core.ApiError(500, f"映射表写入失败：{exc}") from exc
        reloaded = False
        manager = self._manager()
        if manager is not None:
            try:
                _, present = _call_any(manager, "reload_mapping")
                reloaded = bool(present)
            except Exception as exc:  # noqa: BLE001 —— 热加载失败不回滚落盘结果
                logger.warning("映射表热加载钩子失败（下次读取仍会生效）：%s", exc)
        logger.info("情绪映射表已保存（%d 键，备份 %s，热加载 %s）",
                    len(merged), backup or "无", "成功" if reloaded else "未执行")
        return {"ok": True, "map": merged, "backup": backup,
                "reloaded": reloaded, "path": str(path)}

    # ------------------------------------------------------ 语音触发概率旋钮

    def _current_trigger_probability(self) -> float:
        """当前生效的 trigger_probability：manager 运行值优先，回退 vl.toml 内存值。"""
        manager = self._manager()
        live = getattr(manager, "trigger_probability", None)
        if isinstance(live, (int, float)):
            return float(live)
        return float(getattr(self._tts_cfg(), "trigger_probability", 1.0) or 1.0)

    def get_trigger_probability(self) -> dict:
        """/api/voice/trigger-probability GET：当前生效值与来源。"""
        manager = self._manager()
        live = getattr(manager, "trigger_probability", None)
        source = "manager" if isinstance(live, (int, float)) else "config"
        return {"ok": True, "value": self._current_trigger_probability(),
                "source": source}

    def set_trigger_probability(self, payload: dict) -> dict:
        """PUT /api/voice/trigger-probability：运行时热改 + vl.toml 持久化（R3-2e）。

        - 真实生效的旋钮：VoiceManager 每次入队实时读 trigger_probability，
          运行时赋值下一条回复立刻按新概率抽样；
        - 持久化经 load_config → 改字段 → dump_config（.bak 备份 + 原子替换，
          fix_base_url 同手法），重启后仍是同一个值；
        - manager 缺席时只持久化（语音框架未装配，如实上报 runtime_applied=false）。
        """
        if not isinstance(payload, dict):
            raise core.ApiError(400, "请求体必须是 JSON 对象")
        raw = payload.get("value")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise core.ApiError(400, "value 必须是 0-1 的数字") from None
        if not (0.0 <= value <= 1.0):
            raise core.ApiError(400, "trigger_probability 需在 [0, 1] 区间")
        manager = self._manager()
        runtime_applied = False
        if manager is not None and hasattr(manager, "trigger_probability"):
            manager.trigger_probability = value
            runtime_applied = True
        persisted = False
        backup = ""
        root = self._root()
        vl_toml = (root / "local" / "config" / "vl.toml") if root else None
        if vl_toml is not None and vl_toml.is_file():
            try:
                cfg = load_config(vl_toml)
                cfg.tts.trigger_probability = value
                backup = _atomic_write_text(
                    vl_toml, dump_config(cfg), backup=True, clock=self.clock)
                persisted = True
            except (ConfigError, OSError) as exc:
                # 持久化失败不回滚运行时值：立即生效，重启后回旧值（日志可见）
                logger.warning("trigger_probability 持久化失败（运行时已生效）：%s", exc)
        logger.info("语音触发概率已更新：%.2f（运行时 %s，vl.toml %s）", value,
                    "已生效" if runtime_applied else "未应用（manager 缺席）",
                    f"已持久化（备份 {backup or '无'}）" if persisted else "未持久化")
        return {"ok": True, "value": value, "runtime_applied": runtime_applied,
                "persisted": persisted, "backup": backup}

    # ------------------------------------------------------ base_url 一键修正

    def fix_base_url(self) -> dict:
        """base_url 非 127.0.0.1 时一键归一化：vl.toml 改写（.bak 备份 + 原子替换）。

        门禁与报告都以**文件层原文**为准（raw_tts_base_url）——load_config 只把
        host.docker.internal/localhost 归一化进内存，文件残留只有改写文件才能根除。
        已为本机地址时 409 拒绝改写，避免误把用户有意配置的远端引擎覆盖掉。
        """
        root = self._root()
        if root is None:
            raise core.ApiError(503, "运行根未配置（repo_root 缺失），无法修正 base_url")
        vl_toml = Path(root) / "local" / "config" / "vl.toml"
        raw_file = raw_tts_base_url(root) if vl_toml.is_file() else ""
        current = raw_file or str(getattr(self._tts_cfg(), "base_url", "") or "")
        if not current or is_local_base_url(current):
            raise core.ApiError(409, f"base_url（{current or '未配置'}）已指向本机，无需修正")
        if not vl_toml.is_file():
            raise core.ApiError(404, f"未找到 vl.toml（{vl_toml}），无法修正 base_url")
        try:
            cfg = load_config(vl_toml)
        except ConfigError as exc:
            raise core.ApiError(500, f"vl.toml 读取/校验失败，已中止改写：{exc}") from exc
        old = current
        cfg.tts.base_url = _FIX_BASE_URL
        try:
            backup = _atomic_write_text(
                vl_toml, dump_config(cfg), backup=True, clock=self.clock)
        except OSError as exc:
            raise core.ApiError(500, f"vl.toml 改写失败：{exc}") from exc
        applied: "bool | None" = None
        manager = self._manager()
        if manager is not None:
            try:
                _, present = _call_any(manager, "apply_config")
                applied = bool(present)
            except Exception as exc:  # noqa: BLE001
                logger.warning("base_url 热应用钩子失败（重启语音引擎后生效）：%s", exc)
                applied = False
        logger.info("tts.base_url 已修正：%s -> %s（备份 %s）", old, _FIX_BASE_URL,
                    backup or "无")
        return {"ok": True, "old": old, "new": _FIX_BASE_URL, "backup": backup,
                "applied": applied,
                "note": "vl.toml 已改写；若引擎进程在运行，重启语音引擎后生效"}
