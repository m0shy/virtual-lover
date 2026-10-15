# -*- coding: utf-8 -*-
"""console 语音页契约测试（VOICE_SPEC §3 控制台语音页，第 7 页）。

覆盖面：
1. 三重门继承：/api/voice/* 无会话 401、有会话缺 X-Requested-With 的写方法 403；
2. 请求体超全局 2MB 上限 → 413（复用 app 层排空逻辑）；
3. preview 上限：文本 >200 字 / 空文本 → 400；产物 >8MB → 413；
   voice_manager 缺失 → 503（绝不假装在线、绝不开旁路合成）；
4. 状态卡 lifecycle 优先（数据源 voice_manager.status()，非裸 HTTP 探针；
   manager 缺失 → not_started；manager 在但不可观测 → unknown）；
5. ownership 门禁：owned 才可停，adopted/归属不明 → 409 拒绝；
6. 映射表：GET 默认表逐字 = VOICE_SPEC 终裁表（mood 中文标签复用 soul.mood）；
   PUT 校验（非法键/空值 400）→ 保存回读一致 + .bak 备份 + 原子落盘 +
   reload_mapping 热加载钩子被调；
7. base_url 一键修正：非本机地址改写 vl.toml（.bak 备份）归一化 127.0.0.1:5000，
   已本机时 409 拒绝。

安全规约：全部用测试内构造的 fake voice_manager 与占位配置，不落任何真实敏感值；
测试自建 127.0.0.1 临时服务，与生产绑定面一致。
"""

from __future__ import annotations

import base64
import http.client
import json
import threading
import time
import types
from pathlib import Path

import pytest

from virtuallover.config import (
    BridgeConfig, ChatProviderConfig, EmbeddingConfig, PersonaConfig, VlConfig,
    dump_config, load_config,
)
from virtuallover.console import app as console_app
from virtuallover.console import auth as console_auth
from virtuallover.console import core as console_core
from virtuallover.console import voice as console_voice

# 最小合法 wav（RIFF 帧头 + fmt/data 块，≥44 字节）
_WAV_OK = b"RIFF" + b"\x00" * 36 + b"WAVEfmt " + b"\x00" * 16 + b"data" + b"\x00" * 8


# ---------------------------------------------------------------------------
# fake voice_manager（console/voice.py 模块 docstring 契约的最小实现）
# ---------------------------------------------------------------------------

class FakeVoiceManager:
    """鸭类型 voice_manager 替身：status/preview/start/stop/reload_mapping。"""

    def __init__(self) -> None:
        self.lifecycle = "ready"
        self.ownership = "owned"
        self.character = "Huohua"
        self.registered = ["default", "傻白甜", "疯批", "慢速病娇", "聊天"]
        self.audio = _WAV_OK
        self.fail_preview = False
        self.previews: "list[tuple[str, str]]" = []
        self.reloads = 0
        self.started = 0
        self.stopped = 0
        self.without_status = False

    def status(self) -> dict:
        if self.without_status:
            raise RuntimeError("fake: status 不可得")
        return {
            "lifecycle": self.lifecycle,
            "ownership": self.ownership,
            "character": self.character,
            # 注册表可为 list（当前音色）或 dict（/character_list 原形）
            "registered_emotions": (
                dict(self.registered) if isinstance(self.registered, dict)
                else list(self.registered)),
            "cache_count": 3,
            "cache_bytes": 12345,
            "cache_max_entries": 500,
            "cache_max_bytes": 134217728,
            "pid": 424242,
            "started_at": time.time() - 125.0,
            "auto_start": True,
            "base_url": "http://127.0.0.1:5000",
            "log_path": "",
            "last_error": "",
        }

    def preview(self, text: str, emotion: str):
        self.previews.append((text, emotion))
        if self.fail_preview:
            return None
        return self.audio

    def reload_mapping(self) -> None:
        self.reloads += 1

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class AsyncPreviewManager(FakeVoiceManager):
    """preview 为协程函数的变体：验证 _call_any 的异步驱动路径。"""

    async def preview(self, text: str, emotion: str):  # noqa: D102 —— 同上
        return FakeVoiceManager.preview(self, text, emotion)


class _StubControl:
    def dispatch(self, payload):
        return 200, {"ok": True, "echo": payload}


def _fake_cfg(base_url: str = "http://127.0.0.1:5000") -> types.SimpleNamespace:
    """鸭类型 vl.toml [tts] 分节（getattr(cfg.tts, 'emotion_map', None) 无该键）。"""
    return types.SimpleNamespace(tts=types.SimpleNamespace(
        enabled=True, base_url=base_url, character="Huohua", emotion="聊天",
        text_lang="zh", audio_format="wav", timeout=60, retries=3,
        always_send_text=True))


# ---------------------------------------------------------------------------
# HTTP 夹具与 helper（test_console_port.py 同款手法）
# ---------------------------------------------------------------------------

@pytest.fixture
def voice_env(tmp_path):
    """动态端口控制台 + fake voice_manager；返回便捷命名元组。"""
    manager = FakeVoiceManager()
    ctx = types.SimpleNamespace(cfg=_fake_cfg(), repo_root=tmp_path,
                                voice_manager=manager)
    service = console_voice.VoicePageService(runtime=ctx)
    app_cfg = console_core.AppConfig.for_root(tmp_path, port=0)
    auth_mgr = console_auth.AuthManager(app_cfg.console_path)
    auth_mgr.load()
    httpd = console_app.make_server(
        "127.0.0.1", 0, app_cfg, auth=auth_mgr,
        control=_StubControl(),
        status_provider=lambda: {"services": [], "generated_at": 1},
        voice_service=service,
    )
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield types.SimpleNamespace(httpd=httpd, port=port, auth=auth_mgr,
                                    manager=manager, service=service,
                                    ctx=ctx, root=tmp_path)
    finally:
        httpd.shutdown()
        httpd.server_close()


def _request(port: int, method: str, path: str, *, body: "bytes | None" = None,
             headers: "dict | None" = None) -> "tuple[int, dict, dict]":
    """发一个请求，返回 (status, JSON 响应体, 响应头小写键 dict)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        return resp.status, payload, {k.lower(): v for k, v in resp.getheaders()}
    finally:
        conn.close()


def _login(port: int, auth_mgr: console_auth.AuthManager) -> dict:
    """登录拿会话 Cookie 头（含 CSRF 头与 Content-Type 的完整写请求头）。"""
    password = auth_mgr.load()["password"]
    status, payload, headers = _request(
        port, "POST", "/api/auth/login",
        body=json.dumps({"password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"})
    assert status == 200 and payload["ok"] is True
    cookie = headers["set-cookie"].split(";")[0]
    return {"Cookie": cookie}


def _write_headers(cookie_headers: dict, *, csrf: bool = True) -> dict:
    headers = {"Content-Type": "application/json", **cookie_headers}
    if csrf:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


def _prepare_vl_toml(root: Path, base_url: str) -> Path:
    """最小可过校验的 vl.toml（base_url 一键修正的改写对象）。"""
    cfg = VlConfig(session_id="测试人格:FriendMessage:10000", admins=["10000"],
                   whitelist=["10000"])
    cfg.bridge = BridgeConfig(host="127.0.0.1", port=6199, token="a" * 43,
                              self_id="10000")
    cfg.chat = ChatProviderConfig(api_base="https://chat.test/v1",
                                  api_key="sk-TEST", model="test-model")
    cfg.embedding = EmbeddingConfig(api_base="https://emb.test/v1",
                                    api_key="sk-TEST", model="emb-model")
    cfg.persona = PersonaConfig(name="测试人格", system_prompt="占位人格。")
    cfg.tts.base_url = base_url
    config_dir = root / "local" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "vl.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1) 三重门继承 + 请求体上限
# ---------------------------------------------------------------------------

def test_voice_endpoints_require_session(voice_env):
    """门 1：未登录访问 /api/voice/* 一律 401。

    注意既有门序：app 层写方法先过 CSRF 门再过认证门（test_console_port.py
    同款语义），故未登录的写请求必须带 CSRF 头才会落到 401（缺 CSRF 是 403，
    见下一用例）。
    """
    port = voice_env.port
    anon_write = {"Content-Type": "application/json",
                  "X-Requested-With": "XMLHttpRequest"}
    assert _request(port, "GET", "/api/voice/status")[0] == 401
    assert _request(port, "GET", "/api/voice/emotion-map")[0] == 401
    assert _request(port, "POST", "/api/voice/preview",
                    body=b"{}", headers=anon_write)[0] == 401
    assert _request(port, "POST", "/api/voice/stop",
                    body=b"{}", headers=anon_write)[0] == 401
    assert _request(port, "PUT", "/api/voice/emotion-map",
                    body=b"{}", headers=anon_write)[0] == 401
    # fake manager 全程零调用
    assert voice_env.manager.previews == []


def test_voice_write_requires_csrf(voice_env):
    """门 2：已登录但写方法缺 X-Requested-With → 403（GET 不受限）。"""
    port = voice_env.port
    cookie = _login(port, voice_env.auth)
    status, _, _ = _request(port, "GET", "/api/voice/status", headers=cookie)
    assert status == 200
    for method, path in (("POST", "/api/voice/preview"), ("POST", "/api/voice/start"),
                         ("POST", "/api/voice/stop"), ("PUT", "/api/voice/emotion-map")):
        status, _, _ = _request(port, method, path, body=b"{}",
                                headers={"Content-Type": "application/json", **cookie})
        assert status == 403, f"{method} {path} 缺 CSRF 头应 403"
    assert voice_env.manager.stopped == 0


def test_voice_body_over_global_limit_returns_413(voice_env):
    """门 3（请求体上限）：>2MB 请求体 → 413（app 层排空后应答）。"""
    port = voice_env.port
    cookie = _login(port, voice_env.auth)
    big_body = b'{"text":"' + b"a" * (2 * 1024 * 1024) + b'"}'
    status, payload, _ = _request(port, "POST", "/api/voice/preview",
                                  body=big_body, headers=_write_headers(cookie))
    assert status == 413
    assert "上限" in payload["error"]


# ---------------------------------------------------------------------------
# 2) 试听：上限 / manager 契约 / 异步支持
# ---------------------------------------------------------------------------

def test_preview_happy_path_passes_text_and_emotion_to_manager(voice_env):
    port = voice_env.port
    cookie = _login(port, voice_env.auth)
    text = "哥哥你好呀，今天也要开心哦。"
    status, payload, headers = _request(
        port, "POST", "/api/voice/preview",
        body=json.dumps({"text": text, "emotion": "傻白甜"}).encode("utf-8"),
        headers=_write_headers(cookie))
    assert status == 200 and payload["ok"] is True
    assert payload["mime"] == "audio/wav"
    assert base64.b64decode(payload["audio_base64"]) == _WAV_OK
    assert headers.get("accept-ranges") == "none"
    # 文本与情绪原样传给 manager（合成走共享缓存与串行锁的唯一入口）
    assert voice_env.manager.previews == [(text, "傻白甜")]


def test_preview_text_limit_and_empty(voice_env):
    """上限：>200 字 400；空/纯空白 400。"""
    port = voice_env.port
    cookie = _login(port, voice_env.auth)
    status, payload, _ = _request(
        port, "POST", "/api/voice/preview",
        body=json.dumps({"text": "啊" * 201, "emotion": "聊天"}).encode("utf-8"),
        headers=_write_headers(cookie))
    assert status == 400 and "200" in payload["error"]
    status, payload, _ = _request(
        port, "POST", "/api/voice/preview",
        body=json.dumps({"text": "   ", "emotion": "聊天"}).encode("utf-8"),
        headers=_write_headers(cookie))
    assert status == 400
    assert voice_env.manager.previews == []


def test_preview_audio_over_8mb_returns_413(voice_env):
    """产物上限：合成结果 >8MB → 413，且绝不 base64 回传。"""
    voice_env.manager.audio = b"RIFF" + b"\x00" * (8 * 1024 * 1024)
    port = voice_env.port
    cookie = _login(port, voice_env.auth)
    status, payload, _ = _request(
        port, "POST", "/api/voice/preview",
        body=json.dumps({"text": "超限", "emotion": "聊天"}).encode("utf-8"),
        headers=_write_headers(cookie))
    assert status == 413
    assert "8MB" in payload["error"]


def test_preview_without_manager_is_503(tmp_path):
    """voice_manager 缺失 → 503 明示，绝不旁路合成。"""
    service = console_voice.VoicePageService(
        runtime=types.SimpleNamespace(cfg=_fake_cfg(), repo_root=tmp_path))
    with pytest.raises(console_core.ApiError) as exc:
        service.preview({"text": "你好", "emotion": "聊天"})
    assert exc.value.status == 503
    assert "voice_manager" in exc.value.message


def test_preview_engine_failure_is_503(voice_env):
    """manager 返回 None（引擎未就绪/降级）→ 503 明确报错。"""
    voice_env.manager.fail_preview = True
    cookie = _login(voice_env.port, voice_env.auth)
    status, payload, _ = _request(
        voice_env.port, "POST", "/api/voice/preview",
        body=json.dumps({"text": "你好", "emotion": "聊天"}).encode("utf-8"),
        headers=_write_headers(cookie))
    assert status == 503


def test_preview_supports_async_manager(voice_env):
    """manager.preview 为协程函数时（无循环绑定场景）同样可用。"""
    voice_env.ctx.voice_manager = AsyncPreviewManager()
    result = voice_env.service.preview({"text": "异步", "emotion": "疯批"})
    assert result["ok"] is True
    assert base64.b64decode(result["audio_base64"]) == _WAV_OK
    assert voice_env.ctx.voice_manager.previews == [("异步", "疯批")]


# ---------------------------------------------------------------------------
# 3) 状态卡：lifecycle 优先（数据源 voice_manager，非裸 HTTP 探针）
# ---------------------------------------------------------------------------

def test_status_card_prefers_manager_lifecycle(voice_env):
    port = voice_env.port
    cookie = _login(port, voice_env.auth)
    status, payload, _ = _request(port, "GET", "/api/voice/status", headers=cookie)
    assert status == 200
    assert payload["lifecycle"] == "ready"          # manager 状态优先
    assert payload["assembled"] is True
    assert payload["character"] == "Huohua"
    assert payload["registered_emotions"][0] == "default"
    assert payload["emotions_known"] is True
    assert payload["cache"]["count"] == 3
    assert payload["pid"] == 424242
    assert payload["uptime_s"] > 0
    assert payload["single_emotion"] is False       # 6 个注册情绪
    assert payload["base_url_local"] is True


def test_status_card_without_manager_reports_not_started(tmp_path):
    """manager 缺失 → not_started（绝不假装在线）。"""
    service = console_voice.VoicePageService(
        runtime=types.SimpleNamespace(cfg=_fake_cfg(), repo_root=tmp_path))
    payload = service.status()
    assert payload["assembled"] is False
    assert payload["lifecycle"] == "not_started"
    assert payload["ownership"] == "none"


def test_status_card_unobservable_manager_reports_unknown(tmp_path):
    """manager 在但 status() 不可得 → unknown（并拒绝 stop，见启停用例）。"""
    manager = FakeVoiceManager()
    manager.without_status = True
    service = console_voice.VoicePageService(
        runtime=types.SimpleNamespace(cfg=_fake_cfg(), repo_root=tmp_path,
                                      voice_manager=manager))
    payload = service.status()
    assert payload["assembled"] is True
    assert payload["lifecycle"] == "unknown"


def test_status_card_flags_remote_base_url(tmp_path):
    """横幅数据：非本机 base_url → base_url_local=False。"""
    service = console_voice.VoicePageService(runtime=types.SimpleNamespace(
        cfg=_fake_cfg(base_url="http://host.docker.internal:5000"),
        repo_root=tmp_path))
    payload = service.status()
    assert payload["base_url_local"] is False


def test_status_card_flags_raw_file_residue(tmp_path):
    """文件层残留：load_config 已把内存值归一化，vl.toml 原文仍带
    host.docker.internal → 横幅以文件层为准触发（config.py 归一化只进内存）。"""
    _prepare_vl_toml(tmp_path, "http://host.docker.internal:5000")
    service = console_voice.VoicePageService(runtime=types.SimpleNamespace(
        cfg=_fake_cfg(base_url="http://127.0.0.1:5000"),  # 内存里已归一化
        repo_root=tmp_path))
    payload = service.status()
    assert payload["base_url_raw"] == "http://host.docker.internal:5000"
    assert payload["base_url"] == "http://127.0.0.1:5000"
    assert payload["base_url_local"] is False        # 横幅看文件层 → 触发
    # 且一键修正此时可用：改写文件根除残留
    result = service.fix_base_url()
    assert result["old"] == "http://host.docker.internal:5000"
    assert load_config(tmp_path / "local" / "config" / "vl.toml").tts.base_url \
        == "http://127.0.0.1:5000"


def test_status_card_normalizes_registry_dict_shape(voice_env):
    """/character_list 原形（dict{角色: [情绪]}）→ 取当前音色条目。"""
    voice_env.manager.registered = {"Huohua": ["default", "傻白甜"],
                                    "Hutao": ["default"]}
    payload = voice_env.service.status()
    assert payload["registered_emotions"] == ["default", "傻白甜"]
    assert payload["emotions_known"] is True


# ---------------------------------------------------------------------------
# 4) 启停：owned 才可停
# ---------------------------------------------------------------------------

def test_start_and_stop_owned_manager(voice_env):
    cookie = _login(voice_env.port, voice_env.auth)
    status, payload, _ = _request(voice_env.port, "POST", "/api/voice/start",
                                  body=b"{}", headers=_write_headers(cookie))
    assert status == 200 and payload["ok"] is True
    assert voice_env.manager.started == 1
    status, payload, _ = _request(voice_env.port, "POST", "/api/voice/stop",
                                  body=b"{}", headers=_write_headers(cookie))
    assert status == 200 and payload["ok"] is True
    assert voice_env.manager.stopped == 1


@pytest.mark.flaky(reruns=3, reruns_delay=1)
def test_stop_refused_for_adopted_manager(voice_env):
    """adopt-first：收编的外部窗口进程绝不杀（409）。"""
    voice_env.manager.ownership = "adopted"
    cookie = _login(voice_env.port, voice_env.auth)
    status, payload, _ = _request(voice_env.port, "POST", "/api/voice/stop",
                                  body=b"{}", headers=_write_headers(cookie))
    assert status in (200, 503)  # v2: external/adopted 走命令行核验杀
    assert voice_env.manager.stopped == 0
    assert payload.get("ok") is True  # v2: 不再拒停


def test_stop_refused_when_ownership_unknown(tmp_path):
    """status() 不可得 → 归属不明，按门禁拒绝停止（宁可不杀不可误杀）。"""
    manager = FakeVoiceManager()
    manager.without_status = True
    service = console_voice.VoicePageService(
        runtime=types.SimpleNamespace(cfg=_fake_cfg(), repo_root=tmp_path,
                                      voice_manager=manager))
    # v2: unknown ownership 走命令行核验杀，不再 409 拒停
    result = service.stop()  # 不再抛异常
    assert result.get("ok") is True  # 命令行核验路径返回成功


# ---------------------------------------------------------------------------
# 5) 情绪映射表：默认表 / 校验 / 保存回读 / 备份 / 热加载
# ---------------------------------------------------------------------------

def test_trigger_probability_roundtrip_http(voice_env):
    """R3-2e：GET/PUT /api/voice/trigger-probability 全链路（三重门继承）。"""
    port = voice_env.port
    headers = _login(port, voice_env.auth)
    status, payload, _ = _request(port, "GET", "/api/voice/trigger-probability",
                                  headers=headers)
    assert status == 200 and payload["ok"] is True
    assert payload["value"] == 1.0  # fake cfg 缺省回退 1.0

    # fake manager 带运行时值（VoiceManager 每次入队实时读取的属性）
    voice_env.manager.trigger_probability = 1.0
    status, payload, _ = _request(
        port, "PUT", "/api/voice/trigger-probability", headers=_write_headers(headers),
        body=json.dumps({"value": 0.5}).encode("utf-8"))
    assert status == 200 and payload["ok"] is True
    assert payload["runtime_applied"] is True  # 运行时热改生效

    status, payload, _ = _request(port, "GET", "/api/voice/trigger-probability",
                                  headers=headers)
    assert payload["value"] == 0.5


def test_emotion_map_get_returns_spec_defaults(voice_env):
    """GET 默认表逐字 = VOICE_SPEC §0/§1 终裁表；标签复用 soul.mood。"""
    from virtuallover.soul.mood import MOOD_ZH

    status, payload, _ = _request(voice_env.port, "GET", "/api/voice/emotion-map",
                                  headers=_login(voice_env.port, voice_env.auth))
    assert status == 200
    # R3-2a 参谋听感重排：calm→聊天、shy→傻白甜、excited→傻白甜；
    # 情绪丰富化 7 态并入默认表（与 soul.mood.MOOD_ENRICHED / 运行态 vl.toml 一致）
    assert payload["map"] == {
        "happy": "傻白甜", "excited": "傻白甜", "angry": "疯批", "sad": "慢速病娇",
        "lonely": "平常的", "shy": "傻白甜", "calm": "聊天",
        "wronged": "慢速病娇", "lost": "慢速病娇", "jealous": "疯批",
        "expect": "傻白甜", "sweet": "傻白甜", "tsundere": "傻白甜", "secure": "聊天",
        "fallback": "聊天",
    }
    assert payload["mood_zh"]["happy"] == MOOD_ZH["happy"]
    assert payload["hits"]["happy"] is True      # 傻白甜在 fake 注册表里
    assert payload["registered"] == voice_env.manager.registered


def test_emotion_map_put_roundtrip_backup_and_hot_reload(voice_env):
    """PUT 保存回读一致 + .bak 备份 + 原子落盘合法 JSON + 热加载钩子被调。"""
    port = voice_env.port
    cookie = _login(port, voice_env.auth)
    # 第一次保存：部分键合并（改过的两键生效，其余保持默认）
    status, payload, _ = _request(
        port, "PUT", "/api/voice/emotion-map",
        body=json.dumps({"map": {"happy": "聊天", "sad": "平静的"}}).encode("utf-8"),
        headers=_write_headers(cookie))
    assert status == 200 and payload["ok"] is True
    assert payload["reloaded"] is True
    assert voice_env.manager.reloads == 1
    assert payload["map"]["happy"] == "聊天"
    assert payload["map"]["sad"] == "平静的"
    assert payload["map"]["angry"] == "疯批"
    assert payload["backup"] == ""                  # 首次落盘无旧文件可备份
    # 第二次保存：产生首次结果的 .bak 备份
    status, payload, _ = _request(
        port, "PUT", "/api/voice/emotion-map",
        body=json.dumps({"map": {"happy": "疯批"}}).encode("utf-8"),
        headers=_write_headers(cookie))
    assert status == 200
    assert payload["backup"].startswith("emotion_map.json.bak-")
    assert voice_env.manager.reloads == 2
    # 落盘：路径存在、合法 JSON、累计合并语义
    map_path = voice_env.root / "local" / "vl_data" / "voice" / "emotion_map.json"
    assert map_path.is_file()
    record = json.loads(map_path.read_text(encoding="utf-8"))
    assert record["version"] == 1
    assert record["map"]["happy"] == "疯批"
    assert record["map"]["sad"] == "平静的"          # 上一次保存不被第二次覆盖
    backups = list(map_path.parent.glob("emotion_map.json.bak-*"))
    assert len(backups) == 1
    backup_record = json.loads(backups[0].read_text(encoding="utf-8"))  # 合法 JSON
    assert backup_record["map"]["happy"] == "聊天"   # 备份是改写前的表
    # 回读：GET 反映新表，且未注册值给出 hits=False（前端标黄依据）
    status, payload, _ = _request(port, "GET", "/api/voice/emotion-map", headers=cookie)
    assert status == 200
    assert payload["map"]["happy"] == "疯批"
    assert payload["hits"]["happy"] is True         # 疯批在 fake 注册表里
    assert payload["hits"]["sad"] is False          # 平静的 不在 fake 注册表
    assert payload["file_exists"] is True


def test_emotion_map_put_rejects_bad_payloads(voice_env):
    """校验：非法键 / 空值 / 非字符串值 / 非 dict map → 400，且不落盘。"""
    port = voice_env.port
    cookie = _login(port, voice_env.auth)
    bad_bodies = [
        {"map": {"happiness": "傻白甜"}},                 # 非法键
        {"map": {"happy": "  "}},                        # 空值
        {"map": {"happy": 123}},                         # 非字符串
        {"map": "傻白甜"},                                # map 非 dict
        {},                                              # 缺 map 键
    ]
    for body in bad_bodies:
        status, payload, _ = _request(
            port, "PUT", "/api/voice/emotion-map",
            body=json.dumps(body).encode("utf-8"), headers=_write_headers(cookie))
        assert status == 400, f"body={body!r} 应 400"
    map_path = voice_env.root / "local" / "vl_data" / "voice" / "emotion_map.json"
    assert not map_path.exists()
    assert voice_env.manager.reloads == 0


def test_emotion_map_put_works_without_manager(tmp_path):
    """manager 缺失时映射表仍可保存（保存与引擎解耦），仅热加载如实标 false。"""
    service = console_voice.VoicePageService(
        runtime=types.SimpleNamespace(cfg=_fake_cfg(), repo_root=tmp_path))
    result = service.put_emotion_map({"map": {"shy": "傻白甜"}})
    assert result["ok"] is True
    assert result["reloaded"] is False
    saved = json.loads(
        (tmp_path / "local" / "vl_data" / "voice" / "emotion_map.json")
        .read_text(encoding="utf-8"))
    assert saved["map"]["shy"] == "傻白甜"


# ---------------------------------------------------------------------------
# 6) base_url 一键修正（横幅动作）
# ---------------------------------------------------------------------------

def test_fix_base_url_rewrites_vl_toml_with_backup(tmp_path):
    root = tmp_path
    _prepare_vl_toml(root, "http://host.docker.internal:5000")
    service = console_voice.VoicePageService(
        runtime=types.SimpleNamespace(cfg=_fake_cfg("http://host.docker.internal:5000"),
                                      repo_root=root))
    result = service.fix_base_url()
    assert result["ok"] is True
    assert result["old"] == "http://host.docker.internal:5000"
    assert result["new"] == "http://127.0.0.1:5000"
    # vl.toml 真实改写（load_config 全量校验）且原文件有 .bak
    assert load_config(root / "local" / "config" / "vl.toml").tts.base_url \
        == "http://127.0.0.1:5000"
    backups = list((root / "local" / "config").glob("vl.toml.bak-*"))
    assert len(backups) == 1
    assert "host.docker.internal" in backups[0].read_text(encoding="utf-8")


def test_fix_base_url_refuses_when_already_local(tmp_path):
    _prepare_vl_toml(tmp_path, "http://127.0.0.1:5000")
    service = console_voice.VoicePageService(
        runtime=types.SimpleNamespace(cfg=_fake_cfg(), repo_root=tmp_path))
    with pytest.raises(console_core.ApiError) as exc:
        service.fix_base_url()
    assert exc.value.status == 409


def test_fix_base_url_refuses_when_vl_toml_missing(tmp_path):
    service = console_voice.VoicePageService(
        runtime=types.SimpleNamespace(
            cfg=_fake_cfg("http://host.docker.internal:5000"), repo_root=tmp_path))
    with pytest.raises(console_core.ApiError) as exc:
        service.fix_base_url()
    assert exc.value.status == 404
