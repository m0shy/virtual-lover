# -*- coding: utf-8 -*-
"""R7 网络安全收口回归（FUSION_SPEC §3，任务 5）。

五组（对应 ask 任务 1-4 的行为面 + 门禁锁）：

① ``_vl_pids`` 路径边界匹配——前缀同名副本（``<repo>-v2``）不再被裸子串
   误认领（跨副本误杀回归）；引号包裹/大小写不同的本仓命令行仍认领；
② ``_normalize_tts`` retries clamp——retries<1 钳到 1（修前撞 GsviClient
   构造 ValueError，语音引擎装配整体失败）；非法类型回退默认 3；
③ ``onebot_server._download_to`` 走 net.smart_urlopen——回环媒体源禁代理
   直连（代理修复最后缺口），转存落盘与超限拒绝语义不变；
④ ``LocalBrainManager.from_chat_cfg`` spawn host clamp——非回环
   fallback_api_base 拒绝拉起（allow_spawn=False），回环照旧；
⑤ R7 配套回归锁：
   - voice/gsvi.py Basic 认证头（有口令带 Authorization、无口令不带）；
   - voice/lifecycle 拉起前注入引擎 config.json（tts_host/enable_auth/user，
     其余键保全；空口令不武装）；
   - scripts/brain_switch：LLAMA_MARKER 与 --alias 同源 "local-brain"、
     _server_ready 探测 URL 改 /health、_server_accepts_key 复用核验；
     switch_toml_local 弃写死 'local'（单源 fallback_api_key）；
   - brain/router._default_probe 探测 URL 改 /health、spawn args 带
     --alias local-brain 与 --api-key（空 key 不带）。

全部注入假件（假 CIM/假 urlopen/假 spawner），不碰真实进程与网络；
任何真实口令/密钥不进本文件（占位值仅测试运行期内存态）。

运行：python -m pytest virtuallover/tests/test_security_fixes.py -q
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from virtuallover.config import (
    SCHEMA_VERSION,
    ChatProviderConfig,
    EmbeddingConfig,
    PersonaConfig,
    TtsConfig,
    VlConfig,
    dump_config,
    load_config,
)
from virtuallover.brain.router import LocalBrainManager
from virtuallover.voice.lifecycle import GsviLifecycle, LifecycleParams

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 装载 scripts/brain_switch.py（与 test_install_fixes 同手法：文件路径直载，
# 真名入 sys.modules——service.py 引用的就是同一模块对象）
# ---------------------------------------------------------------------------

def _load_brain_switch():
    spec = importlib.util.spec_from_file_location(
        "brain_switch", REPO_ROOT / "scripts" / "brain_switch.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["brain_switch"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def bs():
    return _load_brain_switch()


class _FakeResp:
    """urlopen 返回值的上下文管理器假件（status/read 契约面）。"""

    def __init__(self, status: int = 200, body: bytes = b""):
        self.status = status
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# ① _vl_pids 路径边界匹配（FUSION_SPEC §3.4：禁裸子串）
# ---------------------------------------------------------------------------

class TestVlPidsPathBoundary:

    def test_prefix_same_name_copy_not_claimed(self, bs):
        """前缀同名回归：``C:\\repo\\Virtual-lover-v2`` 的 vl 进程不得被本仓库
        （C:\\repo\\Virtual-lover）认领——修前裸子串 ``repo_low in cmd.lower()``
        会把前缀同名副本误认领，stop/restart 跨副本误杀。"""
        repo = r"C:\repo\Virtual-lover"

        def fake_runner(root: str):
            assert root == repo
            return [
                ("202", rf"{repo}-v2\.venv\Scripts\python.exe -m virtuallover"),
                ("302", rf"{repo}-v2\scripts\brain_switch.py local"),
            ]

        assert bs._vl_pids(repo=repo, runner=fake_runner) == []

    def test_own_repo_still_claimed(self, bs):
        """本仓进程照常强认领（边界修复不得伤及正常路径）。"""
        repo = r"C:\repo\Virtual-lover"

        def fake_runner(root: str):
            return [("101", rf"{repo}\.venv\Scripts\python.exe -m virtuallover")]

        assert bs._vl_pids(repo=repo, runner=fake_runner) == ["101"]

    def test_quoted_cmdline_claimed(self, bs):
        """引号包裹的解释器路径（CIM 常见形态）剥引号后仍按边界认领。"""
        repo = r"C:\repo\Virtual-lover"

        def fake_runner(root: str):
            return [("104", f'"{repo}\\.venv\\Scripts\\python.exe" -m virtuallover')]

        assert bs._vl_pids(repo=repo, runner=fake_runner) == ["104"]

    def test_case_and_slash_normalized(self, bs):
        """normcase 语义：大小写/正斜杠差异不漏认领（Windows 路径不区分）。"""
        repo = r"C:\repo\Virtual-lover"

        def fake_runner(root: str):
            return [("105", "c:/REPO/Virtual-Lover/.venv/Scripts/python.exe -m virtuallover")]

        assert bs._vl_pids(repo=repo, runner=fake_runner) == ["105"]

    def test_repo_exact_prefix_only(self, bs):
        """恰好同前缀但带分隔符边界：``...Virtual-loverX`` 不认领、
        ``...Virtual-lover\\子目录`` 认领。"""
        repo = r"C:\repo\Virtual-lover"

        def fake_runner(root: str):
            return [
                ("401", rf"{repo}X\.venv\Scripts\python.exe -m virtuallover"),   # 无边界
                ("402", rf"{repo}\tools\x.py --tag virtuallover"),               # 仓库内非 vl（无 -m）——但强认领只看路径开头+含 virtuallover 子串
            ]

        # 401 不认领；402 命令行含 virtuallover 且路径在仓库内（历史语义即认领，
        # 谓词只收紧「路径边界」，不改变「路径+包名」匹配面）
        assert bs._vl_pids(repo=repo, runner=fake_runner) == ["402"]


# ---------------------------------------------------------------------------
# ② _normalize_tts retries clamp（FUSION_SPEC §3.4）
# ---------------------------------------------------------------------------

def _full_cfg(**tts_patches) -> VlConfig:
    """全字段占位配置树（不含任何真实账号/密钥；test_config 同款最小面）。"""
    tts = TtsConfig(enabled=True, base_url="http://127.0.0.1:5000", character="角色",
                    emotion="平静", text_lang="zh", audio_format="wav",
                    timeout=60, retries=3)
    for key, val in tts_patches.items():
        setattr(tts, key, val)
    return VlConfig(
        schema_version=SCHEMA_VERSION,
        session_id="测试人格:FriendMessage:10000",
        admins=["10000"],
        whitelist=["10000"],
        log_level="INFO",
        persona=PersonaConfig(name="测试人格", system_prompt="你是\"她\"。",
                              begin_dialogs=["你好"]),
        chat=ChatProviderConfig(api_base="https://api.test/v1", api_key="sk-TEST",
                                model="test-model", timeout=120),
        embedding=EmbeddingConfig(api_base="https://emb.test/v1", api_key="sk-TEST",
                                  model="emb-model", dim=8, timeout=20),
        tts=tts,
    )


class TestNormalizeTtsRetriesClamp:

    def test_zero_retries_clamped_to_one(self, tmp_path):
        """retries=0 → clamp 1（修前 GsviClient 构造 ValueError 炸掉语音装配）。"""
        path = tmp_path / "vl.toml"
        path.write_text(dump_config(_full_cfg(retries=0)), encoding="utf-8")
        cfg = load_config(path)
        assert cfg.tts.retries == 1

    def test_negative_retries_clamped_to_one(self, tmp_path):
        path = tmp_path / "vl.toml"
        path.write_text(dump_config(_full_cfg(retries=-5)), encoding="utf-8")
        assert load_config(path).tts.retries == 1

    def test_non_numeric_retries_falls_back_to_default(self, tmp_path):
        """类型非法（字符串）→ 回退默认 3，而不是带病放行。"""
        path = tmp_path / "vl.toml"
        path.write_text(dump_config(_full_cfg(retries="很多")), encoding="utf-8")
        assert load_config(path).tts.retries == 3

    def test_valid_retries_untouched(self, tmp_path):
        path = tmp_path / "vl.toml"
        path.write_text(dump_config(_full_cfg(retries=7)), encoding="utf-8")
        assert load_config(path).tts.retries == 7

    def test_tts_auth_password_roundtrip(self, tmp_path):
        """R7 新键 [tts].tts_auth_password 进 dump/load 往返（单源 vl.toml）。"""
        path = tmp_path / "vl.toml"
        path.write_text(dump_config(_full_cfg(tts_auth_password="pw-占位-只入内存")),
                        encoding="utf-8")
        assert load_config(path).tts.tts_auth_password == "pw-占位-只入内存"


# ---------------------------------------------------------------------------
# ③ _download_to 走 smart_urlopen（FUSION_SPEC §3.4：代理修复最后缺口）
# ---------------------------------------------------------------------------

class TestDownloadToProxyBypass:

    def test_uses_smart_urlopen_and_writes_file(self, tmp_path, monkeypatch):
        """_download_to 必须经 onebot_server.smart_urlopen（=net.smart_urlopen）
        出网：回环媒体源禁代理直连；UA 头与超时按原契约传递。"""
        from virtuallover.bridge import onebot_server

        seen: dict = {}

        def fake_urlopen(request, timeout=None, **kwargs):
            seen["url"] = request.full_url
            seen["ua"] = request.headers.get("User-agent")
            seen["timeout"] = timeout
            return _FakeResp(200, b"RIFF....wav-bytes")

        monkeypatch.setattr(onebot_server, "smart_urlopen", fake_urlopen)
        dest = tmp_path / "out.bin"
        onebot_server._download_to("http://127.0.0.1:9/media/a.wav", dest)
        assert seen["url"] == "http://127.0.0.1:9/media/a.wav"
        assert seen["ua"] == "virtuallover-bridge/1.0"
        assert seen["timeout"] == onebot_server.MEDIA_DOWNLOAD_TIMEOUT
        assert dest.read_bytes() == b"RIFF....wav-bytes"

    def test_oversize_media_rejected(self, tmp_path, monkeypatch):
        """超限拒绝语义不变（MAX_MEDIA_BYTES+1 字节即弃）。"""
        from virtuallover.bridge import onebot_server

        monkeypatch.setattr(
            onebot_server, "smart_urlopen",
            lambda request, timeout=None, **kw: _FakeResp(
                200, b"x" * (onebot_server.MAX_MEDIA_BYTES + 1)))
        dest = tmp_path / "big.bin"
        with pytest.raises(ValueError, match="上限"):
            onebot_server._download_to("http://example.invalid/big", dest)
        assert not dest.exists()

    def test_default_is_real_smart_urlopen(self):
        """接线回归：模块默认符号必须来自 virtuallover.net（防止改回裸 urlopen）。"""
        from virtuallover import net
        from virtuallover.bridge import onebot_server
        assert onebot_server.smart_urlopen is net.smart_urlopen


# ---------------------------------------------------------------------------
# ④ Router spawn host clamp（FUSION_SPEC §3.4：非回环拒绝拉起）
# ---------------------------------------------------------------------------

def _chat_cfg(**patches) -> SimpleNamespace:
    base = dict(
        fallback_enabled=True,
        fallback_api_base="http://127.0.0.1:8002/v1",
        fallback_model="qwen2.5-7b-instruct",
        fallback_api_key="",
        fallback_timeout=120,
        fallback_max_context_tokens=4096,
        fallback_llama_exe="",
        fallback_gguf="",
        fallback_llama_args="",
        fallback_auto_spawn=True,
        fallback_notice_enabled=False,
        fallback_notice_text="",
    )
    base.update(patches)
    return SimpleNamespace(**base)


class TestSpawnHostClamp:

    def test_non_loopback_refused(self):
        """非回环 fallback_api_base → allow_spawn=False（拒绝拉起，只探测）。"""
        m = LocalBrainManager.from_chat_cfg(
            _chat_cfg(fallback_api_base="http://192.168.1.5:8002/v1"),
            repo_root=".")
        assert m.allow_spawn is False
        assert m.host == "192.168.1.5"

    def test_loopback_spawn_allowed(self):
        m = LocalBrainManager.from_chat_cfg(_chat_cfg(), repo_root=".")
        assert m.allow_spawn is True
        assert m.host == "127.0.0.1"

    def test_auto_spawn_false_still_refused_shape(self):
        """显式 fallback_auto_spawn=false 与 clamp 结果一致（不放大权限）。"""
        m = LocalBrainManager.from_chat_cfg(
            _chat_cfg(fallback_api_base="http://10.0.0.8:8002/v1",
                      fallback_auto_spawn=False),
            repo_root=".")
        assert m.allow_spawn is False

    def test_non_loopback_with_localhost_alias_also_clamped(self):
        """localhost/::1 归一为 127.0.0.1 后放行；其它主机名（如 LAN 名）拒绝。"""
        ok = LocalBrainManager.from_chat_cfg(
            _chat_cfg(fallback_api_base="http://localhost:8002/v1"), repo_root=".")
        assert ok.allow_spawn is True and ok.host == "127.0.0.1"
        bad = LocalBrainManager.from_chat_cfg(
            _chat_cfg(fallback_api_base="http://nas.local:8002/v1"), repo_root=".")
        assert bad.allow_spawn is False


# ---------------------------------------------------------------------------
# ⑤ R7 配套回归锁
# ---------------------------------------------------------------------------

class TestGsviBasicAuth:

    def test_auth_header_present_when_password_set(self, monkeypatch):
        """有口令：/tts 与 /character_list 都带 Authorization: Basic（vl:口令）。

        gsvi 客户端在方法内 ``from ..net import smart_urlopen``（局部导入，
        调用时解析），故打桩点在 virtuallover.net 模块属性。"""
        import asyncio

        import virtuallover.net as net_mod
        from virtuallover.voice import gsvi as g
        seen: list = []

        def fake_urlopen(req, timeout=None, **kw):
            seen.append(req)
            body = (b'{"\xe8\xa7\x92\xe8\x89\xb2": ["\xe5\xb9\xb3\xe9\x9d\x99"]}'
                    if req.full_url.endswith("character_list")
                    else b"RIFF" + b"\x00" * 60)
            return _FakeResp(200, body)

        monkeypatch.setattr(net_mod, "smart_urlopen", fake_urlopen)
        client = g.GsviClient(base_url="http://127.0.0.1:5000", character="角色",
                              auth_password="pw-占位")
        registry = asyncio.run(client.character_list())
        assert registry == {"角色": ["平静"]}
        wav = asyncio.run(client.synthesize("你好"))
        assert wav.startswith(b"RIFF")
        assert len(seen) == 2
        for req in seen:
            token = req.get_header("Authorization")
            assert token and token.startswith("Basic ")
            assert base64.b64decode(token.split(" ", 1)[1]) == "vl:pw-占位".encode("utf-8")

    def test_no_header_without_password(self, monkeypatch):
        """无口令（默认）：绝不带 Authorization 头（旧行为）。"""
        import asyncio

        import virtuallover.net as net_mod
        from virtuallover.voice import gsvi as g
        seen: list = []

        def fake_urlopen(req, timeout=None, **kw):
            seen.append(req)
            return _FakeResp(200, b"RIFF" + b"\x00" * 60)

        monkeypatch.setattr(net_mod, "smart_urlopen", fake_urlopen)
        client = g.GsviClient(base_url="http://127.0.0.1:5000", character="角色")
        assert asyncio.run(client.synthesize("你好")).startswith(b"RIFF")
        assert all(req.get_header("Authorization") is None for req in seen)

    def test_gsvi_auth_user_constant(self):
        """客户端与服务端注入的同一用户名常量（两侧漂移即认证必挂）。"""
        from virtuallover.voice import gsvi as g
        from virtuallover.voice import lifecycle as lc
        assert g.GSVI_AUTH_USER == "vl"
        assert lc.GSVI_AUTH_USER == "vl"


class TestLifecycleConfigInjection:

    def _engine_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "engine"
        (root / "Inference").mkdir(parents=True)
        (root / "Inference" / "config.json").write_text(json_dumps({
            "locale": "auto", "tts_port": 5000, "tts_host": "0.0.0.0",
            "enable_auth": "false",
            "user": {"admin": "admin123"},
            "备注2": "tts_port是tts服务的端口号",
        }), encoding="utf-8")
        return root

    def test_injection_arms_auth_and_loopback_host(self, tmp_path):
        """拉起前注入：tts_host 强制回环、enable_auth="true"（字符串）、
        user 整体覆盖为 {vl: 口令}（清掉 admin123 弱口令）、其余键保全。"""
        root = self._engine_root(tmp_path)
        calls: list = []

        def fake_spawner(argv, cwd):
            calls.append((list(argv), cwd))
            return 4242

        lc = GsviLifecycle(
            LifecycleParams(gsvi_root=str(root), host="127.0.0.1", port=5000,
                            auth_password="pw-占位"),
            repo_root=tmp_path, spawner=fake_spawner)
        assert lc._spawn() == 4242
        assert calls, "spawner 必须被调用"
        data = json_loads((root / "Inference" / "config.json").read_text(encoding="utf-8"))
        assert data["tts_host"] == "127.0.0.1"
        assert data["enable_auth"] == "true"          # 字符串（上游 .lower() 判定）
        assert data["user"] == {"vl": "pw-占位"}
        assert data["locale"] == "auto"               # 其余键保全
        assert "备注2" in data

    def test_no_password_keeps_auth_off(self, tmp_path):
        """空口令=不武装：enable_auth/user 保持现状（旧行为），tts_host 仍强制回环。"""
        root = self._engine_root(tmp_path)
        lc = GsviLifecycle(
            LifecycleParams(gsvi_root=str(root), host="127.0.0.1", port=5000,
                            auth_password=""),
            repo_root=tmp_path, spawner=lambda argv, cwd: 1)
        lc._spawn()
        data = json_loads((root / "Inference" / "config.json").read_text(encoding="utf-8"))
        assert data["tts_host"] == "127.0.0.1"
        assert data["enable_auth"] == "false"
        assert data["user"] == {"admin": "admin123"}

    def test_missing_config_does_not_block_spawn(self, tmp_path):
        """config.json 缺失：注入跳过（告警），拉起照常（引擎走上游默认）。"""
        root = tmp_path / "bare-engine"
        root.mkdir()
        calls: list = []
        lc = GsviLifecycle(
            LifecycleParams(gsvi_root=str(root), host="127.0.0.1", port=5000,
                            auth_password="pw-占位"),
            repo_root=tmp_path, spawner=lambda argv, cwd: calls.append(1) or 7)
        assert lc._spawn() == 7
        assert calls == [1]


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=4)


def json_loads(text: str):
    import json
    return json.loads(text)


class TestBrainSwitchArmed:

    def test_marker_alias_single_source(self, bs):
        """击杀标记与 --alias 同源 "local-brain"（漂移=核验恒拒杀）。"""
        assert bs.LLAMA_MARKER == "local-brain"
        assert bs.LOCAL_MODEL == "local-brain"
        assert "--alias" in bs.LLAMA_ARGS
        assert bs.LLAMA_ARGS[bs.LLAMA_ARGS.index("--alias") + 1] == bs.LLAMA_MARKER

    def test_server_ready_probes_health(self, bs, monkeypatch):
        """_server_ready 改打 /health（armed server 免鉴权端点；加载期 503=未就绪）。"""
        seen: list = []

        def fake_urlopen(target, timeout=None, **kw):
            seen.append(str(getattr(target, "full_url", None) or target))
            return _FakeResp(200)

        monkeypatch.setattr(bs, "_direct_urlopen", fake_urlopen)
        assert bs._server_ready(8002) is True
        assert seen and seen[0].endswith(":8002/health")

    def test_llama_alive_treats_503_as_alive(self, bs, monkeypatch):
        """看护探活语义：加载期 503（HTTPError）=活；连接拒绝=死。"""
        import urllib.error

        def loading(target, timeout=None, **kw):
            raise urllib.error.HTTPError(str(target), 503, "loading", {}, None)

        def dead(target, timeout=None, **kw):
            raise OSError("refused")

        monkeypatch.setattr(bs, "_direct_urlopen", loading)
        assert bs._llama_alive(8002) is True
        monkeypatch.setattr(bs, "_direct_urlopen", dead)
        assert bs._llama_alive(8002) is False

    def test_server_accepts_key_matrix(self, bs, monkeypatch):
        """复用核验：unarmed server 忽略头→200 复用；armed 换 key→401 停旧重拉。"""
        monkeypatch.setattr(bs, "LLAMA_API_KEY", "x" * 32)

        def armed_ok(target, timeout=None, **kw):
            auth = target.get_header("Authorization") or ""
            return _FakeResp(200 if auth == f"Bearer {'x' * 32}" else 401)

        monkeypatch.setattr(bs, "_direct_urlopen", armed_ok)
        assert bs._server_accepts_key(8002) is True

        monkeypatch.setattr(bs, "LLAMA_API_KEY", "y" * 32)
        assert bs._server_accepts_key(8002) is False

    def test_switch_toml_local_drops_local_constant(self, tmp_path, bs, monkeypatch):
        """switch_toml_local 弃写死 'local'：api_key=单源 fallback_api_key
        （弱/缺时强生成并写回）；再次切换幂等（不换 key）。"""
        toml = tmp_path / "vl.toml"
        toml.write_text(
            '[chat]\napi_base = "https://api.test/v1"\nmodel = "m"\n'
            'api_key = "sk-TEST"\nfallback_api_key = "local"\n'
            '[context]\nmax_context_tokens = 32768\n', encoding="utf-8")
        backup = tmp_path / "vl.toml.cloud-backup"
        monkeypatch.setattr(bs, "VL_TOML", toml)
        monkeypatch.setattr(bs, "BACKUP", backup)
        monkeypatch.setattr(bs, "LLAMA_API_KEY", "")   # 隔离：模块加载会读到真实部署 toml 的 key

        bs.switch_toml_local()
        import tomllib
        with open(toml, "rb") as fh:
            chat = tomllib.load(fh)["chat"]
        assert chat["api_base"].startswith("http://127.0.0.1:8002")
        assert chat["fallback_api_key"] != "local"
        assert len(chat["fallback_api_key"]) >= bs.LOCAL_API_KEY_MIN_LEN
        assert chat["api_key"] == chat["fallback_api_key"]   # 客户端=服务端同 key

        first = chat["fallback_api_key"]
        bs.switch_toml_local()   # 幂等：强 key 原样保留
        with open(toml, "rb") as fh:
            assert tomllib.load(fh)["chat"]["fallback_api_key"] == first

    def test_switch_toml_local_legacy_toml_falls_back(self, tmp_path, bs, monkeypatch):
        """老结构 toml（[chat] 无 fallback_api_key 键）：key 写不进则回退占位
        'local'（此时 server 也不武装，两侧一致仍可对话），绝不 SystemExit。"""
        toml = tmp_path / "vl.toml"
        toml.write_text(
            '[chat]\napi_base = "https://api.test/v1"\nmodel = "m"\n'
            'api_key = "sk-TEST"\n'
            '[context]\nmax_context_tokens = 32768\n', encoding="utf-8")
        monkeypatch.setattr(bs, "VL_TOML", toml)
        monkeypatch.setattr(bs, "BACKUP", tmp_path / "b.cloud-backup")
        monkeypatch.setattr(bs, "LLAMA_API_KEY", "")
        bs.switch_toml_local()
        import tomllib
        with open(toml, "rb") as fh:
            chat = tomllib.load(fh)["chat"]
        assert chat["api_key"] == "local"

    def test_ensure_key_skips_strong_existing(self, tmp_path, bs, monkeypatch):
        """已有强 key：ensure 原样返回不换（幂等）。"""
        toml = tmp_path / "vl.toml"
        strong = "K" * 40
        toml.write_text(f'[chat]\napi_base = "x"\nmodel = "m"\napi_key = "sk"\n'
                        f'fallback_api_key = "{strong}"\n', encoding="utf-8")
        monkeypatch.setattr(bs, "VL_TOML", toml)
        monkeypatch.setattr(bs, "LLAMA_API_KEY", "")
        assert bs.ensure_local_api_key() == strong
        assert bs.LLAMA_API_KEY == strong

    def test_setup_writeback_generates_strong_key_on_fresh_install(self, tmp_path):
        """setup 写回正向回归：全新安装（带 Key/QQ）必生成强 fallback_api_key
        （FUSION_SPEC §3.3：setup 一次到位）；空参调用不写（既有契约）。"""
        from virtuallover.tools.init_config import run_init
        from virtuallover.tools.setup_writeback import writeback

        assert run_init(tmp_path) == 0
        assert writeback(tmp_path, api_key="sk-TEST-SETUP-KEY",
                         qq="123456789") == 0
        cfg = load_config(tmp_path / "local" / "config" / "vl.toml")
        assert cfg.chat.fallback_api_key != "local"
        assert len(cfg.chat.fallback_api_key) >= 24


class TestRouterArmed:

    def test_default_probe_hits_health(self, monkeypatch):
        """Router 探测闭包改打 /health（armed server /v1/models 恒 401 假阴性）。

        _default_probe 闭包内 ``from virtuallover.net import smart_urlopen``
        （调用时解析），打桩点在 virtuallover.net 模块属性。"""
        import virtuallover.net as net_mod
        from virtuallover.brain import router as r
        seen: list = []

        def fake_urlopen(target, timeout=None, **kw):
            seen.append(str(getattr(target, "full_url", None) or target))
            return _FakeResp(200)

        monkeypatch.setattr(net_mod, "smart_urlopen", fake_urlopen)
        probe = r._default_probe("127.0.0.1", 8002, 0.1)
        assert probe() is True
        assert seen and seen[0].endswith(":8002/health")

    def test_spawn_args_carry_alias_and_key(self):
        """Router spawn args：--alias 中性标记 + --api-key 单源；空 key 不带。"""
        m = LocalBrainManager.from_chat_cfg(
            _chat_cfg(fallback_api_key="K" * 40), repo_root=".")
        i = m.spawn_args.index("--alias")
        assert m.spawn_args[i + 1] == "local-brain"
        j = m.spawn_args.index("--api-key")
        assert m.spawn_args[j + 1] == "K" * 40

    def test_spawn_args_omit_key_when_empty(self):
        m = LocalBrainManager.from_chat_cfg(_chat_cfg(fallback_api_key=""),
                                            repo_root=".")
        assert "--api-key" not in m.spawn_args
        assert "--alias" in m.spawn_args

    def test_alias_constant_matches_brain_switch_marker(self, bs):
        """包内常量与 scripts 常量人工同步锁（scripts/ 不能 import 包，漂移即
        击杀核验拒杀对方拉起的进程）。"""
        from virtuallover.brain.router import LOCAL_BRAIN_ALIAS
        assert LOCAL_BRAIN_ALIAS == bs.LLAMA_MARKER
