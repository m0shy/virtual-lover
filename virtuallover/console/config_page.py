# -*- coding: utf-8 -*-
"""管理控制台 —— 配置页（config）。GET/PUT /api/config/{chat,embedding,persona} 逻辑层。

写入纪律（逐字参照 console/voice.py :meth:`fix_base_url` 金样板）：
``load_config`` 全量校验 → 内存改字段 → ``dump_config`` 序列化 → ``.bak-<时间戳>``
备份 → tmp 落盘 + ``os.replace`` 原子替换。任何一步失败都在写盘前中止
（ConfigError → 500，OSError → 500），绝不落半截配置。

与金样板的关键差异——**没有热加载**：vl 的 ``VlConfig`` 是启动时快照，写盘
不热加载，所以 PUT 响应一律 ``need_restart: true`` 明示「需重启 vl 进程后生效」
（金样板的 tts.base_url 有 ``apply_config`` 热应用钩子，本页三节都没有）。

安全纪律：
- api_key **回显只给 ``{"len": N}``**，任何 GET 响应都不携带密钥原文；PUT 时
  api_key 缺省/None = 保持现值（改 key 必须显式给非空新值，拒绝空串清空——
  config._validate 启动校验本就要求密钥非空）；
- 三重门自动继承（app 层 /api/* 会话认证 + PUT 属写方法走 X-Requested-With
  CSRF 门），本模块不另设旁路；
- 日志绝不打印密钥/token 全文（与 core.py 同纪律）。

GET 读文件层（与 PUT 同一基准：控制台改写永远基于 vl.toml 现状做读-改-写，
避免「GET 内存快照、PUT 文件现状」的错位）；文件不可读时降级回内存快照并在
响应里标注 ``source``（file/memory），两者皆不可得 503 如实报错。
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from pathlib import Path

from virtuallover.config import ConfigError, VlConfig, dump_config, load_config

from . import core
from .voice import _atomic_write_text  # noqa: SLF001 —— 同包复用金样板原子写实现

logger = logging.getLogger("virtuallover.console.config_page")

__all__ = ["ConfigPageService", "SECTIONS"]

# 可写分节白名单（key=URL 路径段，值=VlConfig 属性名；本页只开放这三节，
# bridge/tts 等敏感或已有专属页面的分节不进这里）
SECTIONS = ("chat", "embedding", "persona", "tts")

# ---------------------------------------------------------------------------
# 字段契约：GET/PUT 的字段面 + 校验上限（与 config.py dataclass 字段一一对应）
# ---------------------------------------------------------------------------

# [chat] thinking / bg_thinking 三态（PERF_SPEC §2 O1；与 config._THINKING_MODES
# 同语义——其为私有名，这里按契约常量本地声明，测试钉住两处一致）
THINKING_MODES = ("disabled", "enabled", "auto")
# [embedding].style 合法取值（config.EmbeddingConfig：openai 标准 / 多模态端点；
# R6：端点风格中性化 ark_multimodal→multimodal_single，旧值仍收（llm.py 构造时映射））
EMBEDDING_STYLES = ("openai", "multimodal_single", "ark_multimodal")

# 长度上限（防误粘贴整段日志/超长 payload 把 vl.toml 写爆）
URL_MAX_CHARS = 2048
MODEL_MAX_CHARS = 256
API_KEY_MAX_CHARS = 4096
PERSONA_NAME_MAX_CHARS = 128
SYSTEM_PROMPT_MAX_CHARS = 65536
BEGIN_DIALOG_MAX_CHARS = 2048
BEGIN_DIALOG_MAX_ENTRIES = 32
# [embedding].dim 合法区间（1=下限保底可建库；65536=远超现网 2048 的宽松上限）
DIM_MIN, DIM_MAX = 1, 65536

# 各节字段 → (是否密钥, GET 是否展示)。全部可 PUT；密钥字段 GET 打码。
_FIELD_TABLE: "dict[str, dict[str, bool]]" = {
    "chat": {
        "api_base": False,
        "model": False,
        "api_key": True,       # 密钥：GET 只回 {"len": N}
        "thinking": False,
        "bg_thinking": False,
        "fallback_llama_exe": False,
        "fallback_gguf": False,
        "fallback_api_base": False,
        "fallback_enabled": False,
        "fallback_model": False,
    },
    "embedding": {
        "api_base": False,
        "model": False,
        "api_key": True,       # 密钥：GET 只回 {"len": N}
        "style": False,
        "dim": False,
    },
    "persona": {
        "name": False,
        "system_prompt": False,
        "begin_dialogs": False,
    },
    "tts": {
        "enabled": False,
        "auto_start": False,
        "base_url": False,
        "character": False,
        "emotion": False,
        "text_lang": False,
        "audio_format": False,
        "always_send_text": False,
        "trigger_probability": False,
        "gsvi_root": False,
        "tts_auth_password": True,
    },
}

NEED_RESTART_NOTE = (
    "配置已写盘；vl 的配置是启动时快照，写盘不热加载——重启 vl 进程后生效"
)


# ---------------------------------------------------------------------------
# 字段级校验（PUT 值 → 清洗后可写值；非法抛 ApiError(400)）
# ---------------------------------------------------------------------------

def _require_str(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise core.ApiError(400, f"{field} 必须是字符串")
    return value.strip()


def _validate_url(field: str, value: object) -> str:
    text = _require_str(field, value)
    if not text:
        raise core.ApiError(400, f"{field} 不能为空")
    if len(text) > URL_MAX_CHARS:
        raise core.ApiError(400, f"{field} 超过 {URL_MAX_CHARS} 字符上限")
    scheme = urllib.parse.urlsplit(text).scheme.lower()
    if scheme not in ("http", "https"):
        raise core.ApiError(400, f"{field} 必须是 http(s) URL（当前缺 scheme 或非法）")
    return text


def _validate_model(field: str, value: object) -> str:
    text = _require_str(field, value)
    if not text:
        raise core.ApiError(400, f"{field} 不能为空")
    if len(text) > MODEL_MAX_CHARS:
        raise core.ApiError(400, f"{field} 超过 {MODEL_MAX_CHARS} 字符上限")
    return text


def _validate_api_key(field: str, value: object) -> str:
    text = _require_str(field, value)
    if not text:
        raise core.ApiError(400, f"{field} 不能为空（如需保留现值请省略该字段，"
                                 "不支持清空——启动校验要求密钥非空）")
    if len(text) > API_KEY_MAX_CHARS:
        raise core.ApiError(400, f"{field} 超过 {API_KEY_MAX_CHARS} 字符上限")
    return text


def _validate_bool(field: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise core.ApiError(400, f"{field} 必须是布尔值（true/false）")
    return value


def _validate_float(field: str, value: object) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise core.ApiError(400, f"{field} 必须是数字") from None
    if v < 0 or v > 1:
        raise core.ApiError(400, f"{field} 必须在 [0, 1] 区间")
    return v


def _validate_thinking(field: str, value: object) -> str:
    text = _require_str(field, value).lower()
    if text not in THINKING_MODES:
        raise core.ApiError(
            400, f"{field} 只允许 {'/'.join(THINKING_MODES)} 之一（当前 {text!r}）")
    return text


def _validate_style(field: str, value: object) -> str:
    text = _require_str(field, value).lower()
    if text not in EMBEDDING_STYLES:
        raise core.ApiError(
            400, f"{field} 只允许 {'/'.join(EMBEDDING_STYLES)} 之一（当前 {text!r}）")
    return text


def _validate_dim(field: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise core.ApiError(400, f"{field} 必须是整数（当前类型 {type(value).__name__}）")
    if not (DIM_MIN <= value <= DIM_MAX):
        raise core.ApiError(
            400, f"{field} 超出合法区间 [{DIM_MIN}, {DIM_MAX}]（当前 {value}）")
    return int(value)


def _validate_name(field: str, value: object) -> str:
    text = _require_str(field, value)
    if not text:
        raise core.ApiError(400, f"{field} 不能为空")
    if len(text) > PERSONA_NAME_MAX_CHARS:
        raise core.ApiError(400, f"{field} 超过 {PERSONA_NAME_MAX_CHARS} 字符上限")
    return text


def _validate_system_prompt(field: str, value: object) -> str:
    text = _require_str(field, value)
    if not text:
        raise core.ApiError(400, f"{field} 不能为空")
    if len(text) > SYSTEM_PROMPT_MAX_CHARS:
        raise core.ApiError(400, f"{field} 超过 {SYSTEM_PROMPT_MAX_CHARS} 字符上限")
    return text


def _validate_begin_dialogs(field: str, value: object) -> "list[str]":
    if not isinstance(value, list):
        raise core.ApiError(400, f"{field} 必须是字符串列表（user/assistant 交替）")
    if len(value) > BEGIN_DIALOG_MAX_ENTRIES:
        raise core.ApiError(400, f"{field} 超过 {BEGIN_DIALOG_MAX_ENTRIES} 条上限")
    if len(value) % 2 != 0:
        raise core.ApiError(
            400, f"{field} 必须是偶数条（user/assistant 交替成对；当前 {len(value)} 条）")
    clean: "list[str]" = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise core.ApiError(400, f"{field}[{index}] 必须是非空字符串")
        text = item.strip()
        if len(text) > BEGIN_DIALOG_MAX_CHARS:
            raise core.ApiError(
                400, f"{field}[{index}] 超过 {BEGIN_DIALOG_MAX_CHARS} 字符上限")
        clean.append(text)
    return clean


# 字段 → 校验器（PUT 值清洗统一入口）
_FIELD_VALIDATORS = {
    "api_base": _validate_url,
    "api_key": _validate_api_key,
    "model": _validate_model,
    "thinking": _validate_thinking,
    "bg_thinking": _validate_thinking,
    "style": _validate_style,
    "dim": _validate_dim,
    "name": _validate_name,
    "system_prompt": _validate_system_prompt,
    "begin_dialogs": _validate_begin_dialogs,
    # chat 路径/降级键（字符串路径与开关，宽松校验——空串合法=用默认值）
    "fallback_llama_exe": _require_str,
    "fallback_gguf": _require_str,
    "fallback_api_base": _require_str,
    "fallback_enabled": _validate_bool,
    "fallback_model": _require_str,
    # tts 键
    "enabled": _validate_bool,
    "auto_start": _validate_bool,
    "base_url": _require_str,
    "character": _require_str,
    "emotion": _require_str,
    "text_lang": _require_str,
    "audio_format": _require_str,
    "always_send_text": _validate_bool,
    "trigger_probability": _validate_float,
    "gsvi_root": _require_str,
    "tts_auth_password": _validate_api_key,
}


def validate_section_payload(section: str, payload: dict) -> dict:
    """校验 PUT 请求体，返回 {字段名: 清洗后新值}（只含出现的字段）。

    - 未知字段 400（配置编辑是显式动作，静默忽略会掩盖拼写错误）；
    - 空请求体（无任何已知字段）400——拒绝「无改动也重写文件」的空转写盘；
    - 字段值 None 视为「未提供」（保持现值），与缺省同义，方便前端整表回传。
    """
    if not isinstance(payload, dict):
        raise core.ApiError(400, "请求体必须是 JSON 对象")
    known = _FIELD_TABLE[section]
    unknown = [key for key in payload if key not in known]
    if unknown:
        raise core.ApiError(
            400, f"未知字段 {unknown}（[{section}] 可写字段：{sorted(known)}）")
    updates: dict = {}
    for field, is_secret in known.items():
        if field not in payload or payload[field] is None:
            continue
        updates[field] = _FIELD_VALIDATORS[field](field, payload[field])
    if not updates:
        raise core.ApiError(400, "请求体不含任何可写字段（无改动则无需保存）")
    return updates


# ---------------------------------------------------------------------------
# GET 渲染：密钥打码为 {"len": N}
# ---------------------------------------------------------------------------

def render_section(section: str, cfg_section: object) -> dict:
    """[section] 配置节 → GET 响应的 config 字段面（密钥只回长度）。"""
    rendered: dict = {}
    for field, is_secret in _FIELD_TABLE[section].items():
        value = getattr(cfg_section, field, None)
        if is_secret:
            rendered[field] = {"len": len(value) if isinstance(value, str) else 0}
        elif field == "begin_dialogs":
            rendered[field] = [str(item) for item in (value or [])]
        elif field == "dim":
            rendered[field] = int(value) if isinstance(value, (int, float)) else 0
        elif isinstance(value, bool):
            rendered[field] = value
        elif isinstance(value, (int, float)):
            rendered[field] = value
        else:
            rendered[field] = str(value or "")
    return rendered


# ---------------------------------------------------------------------------
# 页面服务
# ---------------------------------------------------------------------------

class ConfigPageService:
    """/api/config/{chat,embedding,persona} 的应用服务：GET 打码读 / PUT 金样板写。

    runtime 为鸭类型装配上下文（AppContext）：repo_root 定位 vl.toml；
    cfg 仅作文件不可读时的 GET 降级来源（内存快照）。
    """

    def __init__(self, runtime: object, *, clock: "callable | None" = None) -> None:
        self.runtime = runtime
        self.clock = clock or time.time

    # -------------------------------------------------------------- 内部取值

    def _root(self) -> "Path | None":
        root = getattr(self.runtime, "repo_root", None)
        return Path(root) if root else None

    def vl_toml_path(self) -> "Path | None":
        root = self._root()
        return root / "local" / "config" / "vl.toml" if root else None

    def _load_cfg_for_get(self) -> "tuple[VlConfig, str]":
        """GET 的配置来源：文件层优先（与 PUT 同基准），不可读降级内存快照。

        返回 (cfg, source)；两者皆不可得抛 503 如实报错，绝不假装可读。
        """
        path = self.vl_toml_path()
        if path is not None and path.is_file():
            try:
                return load_config(path), "file"
            except (ConfigError, ValueError) as exc:
                # ValueError 兜住 tomllib.TOMLDecodeError（损坏文件不是合法配置，
                # 降级内存快照并在日志留痕）
                logger.warning("vl.toml 读取/校验失败，GET 降级内存快照：%s", exc)
        snapshot = getattr(self.runtime, "cfg", None)
        if snapshot is not None and isinstance(snapshot, VlConfig):
            return snapshot, "memory"
        raise core.ApiError(
            503, "vl.toml 不可读且运行时配置未装配，无法读取配置")

    # -------------------------------------------------------------- GET

    def get_section(self, section: str) -> dict:
        """GET /api/config/<section>：字段面 + 密钥打码 + 来源标注。"""
        if section not in SECTIONS:
            raise core.ApiError(404, f"未知配置节：{section}")
        cfg, source = self._load_cfg_for_get()
        payload = {
            "ok": True,
            "section": section,
            "config": render_section(section, getattr(cfg, section)),
            "source": source,
        }
        path = self.vl_toml_path()
        payload["file_exists"] = bool(path and path.is_file())
        return payload

    # -------------------------------------------------------------- PUT

    def put_section(self, section: str, payload: object) -> dict:
        """PUT /api/config/<section>：金样板写（load→改→校验→.bak→原子写）。

        vl 的配置是启动时快照、写盘不热加载——响应一律 need_restart=true。
        """
        if section not in SECTIONS:
            raise core.ApiError(404, f"未知配置节：{section}")
        updates = validate_section_payload(section, payload)
        path = self.vl_toml_path()
        if path is None:
            raise core.ApiError(503, "运行根未配置（repo_root 缺失），无法保存配置")
        if not path.is_file():
            raise core.ApiError(404, f"未找到 vl.toml（{path}），无法保存配置")
        try:
            cfg = load_config(path)
        except (ConfigError, ValueError) as exc:
            # ValueError 兜住 tomllib.TOMLDecodeError：损坏文件一律写盘前中止，
            # 绝不基于半截配置生成新文件
            raise core.ApiError(500, f"vl.toml 读取/校验失败，已中止写入：{exc}") from exc

        cfg_section = getattr(cfg, section)
        for field, value in updates.items():
            setattr(cfg_section, field, value)

        try:
            backup = _atomic_write_text(
                path, dump_config(cfg), backup=True, clock=self.clock)
        except OSError as exc:
            raise core.ApiError(500, f"vl.toml 写入失败：{exc}") from exc
        logger.info("配置节 [%s] 已写盘（字段 %s，备份 %s；需重启生效）",
                    section, sorted(updates), backup or "无")
        return {
            "ok": True,
            "section": section,
            "applied_keys": sorted(updates),
            "backup": backup,
            "need_restart": True,
            "note": NEED_RESTART_NOTE,
        }
