# -*- coding: utf-8 -*-
"""五轮修缮 R4 安装链路修复测试（走查审计①② + 多副本隔离 + 路径可配置化）。

覆盖面（与审计条目一一对应）：
① 本地大脑判定精确化：tomllib 结构化读 [chat].api_base（直测函数本身，
   不旁路）——含「云端新装配置含 fallback_api_base 默认值」回归用例：init
   骨架 dump 必写 fallback_api_base=http://127.0.0.1:8002/v1（config.py
   ChatConfig 字段默认），修前全文子串匹配把云端新装误判本地 → cmd_start
   走 ensure_gguf 在无模型机器 SystemExit 死亡、cmd_cloud 后看护重拉 8002；
② writeback 语义修复：whitelist 必写（占位 10000 清除）+ session 尾段/admins
   （主人）与 bridge.self_id（机器人）两套语义分开，宽松模式单号兜底；
③ 多副本隔离：_vl_pids 仓库路径 + virtuallover 双匹配（假 CIM 注入直测）；
   read_console_port 读 vl.toml [console].port（失败回退 8787）；
④ make_server 关 SO_REUSEADDR（ConsoleHTTPServer.allow_reuse_address=False，
   端口被占 bind 即 OSError）；
⑤ brain_switch 路径可配置化：vl.toml [chat] fallback_gguf / fallback_llama_exe /
   fallback_convert_python 覆盖默认路径（LLAMA_ARGS 随 GGUF 重建）；
⑥ SYS_PY = Path(sys.executable)（修前硬编码 anaconda 路径换机必炸）；
⑦ cmd_start 云端分支桩流：napcat/vl/HTTP 全打桩——云端新装不再碰 ensure_gguf，
   就绪探测与控制台地址读 [console].port。

安全规约：QQ 用 111111111 / 222222222 编造占位号段，Key 一律 sk-TEST 占位；
CIM/Popen/端口探测/HTTP 全注入假件——不触碰真实进程、真实 vl.toml 与真实
vl.toml 写盘；brain_switch 全局路径覆盖用例用 try/finally 恢复原值。
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_BOT_QQ = "111111111"     # 编造占位：机器人小号
_OWNER_QQ = "222222222"   # 编造占位：主人常用号
_TEST_KEY = "sk-TEST-INSTALL-FIXES-000"

_CLOUD_TOML = """\
schema_version = 1
session_id = "小语:FriendMessage:10000"
admins = ["10000"]
whitelist = ["10000"]

[bridge]
host = "127.0.0.1"
port = 6199
path = "/ws"
token = ""
self_id = "10000"

[chat]
api_base = "https://ark.cn-beijing.volces.com/api/v3"
api_key = "PENDING_SETUP_CHAT_KEY"
model = "doubao-seed-1-6-flash-250828"
fallback_api_base = "http://127.0.0.1:8002/v1"

[console]
host = "127.0.0.1"
port = 8787
"""


def _load(name: str, path: Path):
    """按独立模块名加载脚本（test_service_guard 同款手法）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def svc():
    """加载 scripts/service.py（其 import 链会带出真名 brain_switch）。"""
    return _load("service_install_fixes", REPO_ROOT / "scripts" / "service.py")


@pytest.fixture()
def bs(svc):
    """service.py 引用的同一个 brain_switch 模块对象（真名 sys.modules）。"""
    return sys.modules["brain_switch"]


def _write_toml(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "vl.toml"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# ① 本地大脑判定精确化（回归：fallback_api_base 默认值污染）
# ---------------------------------------------------------------------------

def test_cloud_fresh_install_with_fallback_default_is_not_local(tmp_path, bs, svc):
    """审计①回归：云端新装 vl.toml 含 dump 必写的 fallback_api_base 默认值。

    init 骨架天然就是审计现场：[chat].api_base=方舟预设 + fallback_api_base=
    http://127.0.0.1:8002/v1（config.py 字段默认）。修前全文子串匹配
    '127.0.0.1:8002' 命中后者 → 误判本地。直测判定函数本身（不旁路）。
    """
    from virtuallover.tools.init_config import run_init
    assert run_init(tmp_path) == 0
    toml = tmp_path / "local" / "config" / "vl.toml"
    text = toml.read_text(encoding="utf-8")
    assert "127.0.0.1:8002" in text  # 污染源确实写在文件里（修前误判的根）
    # 判定只认 [chat].api_base——fallback_api_base 是降级预案，与当前大脑无关
    assert bs.vl_toml_uses_local_brain(toml) is False
    # service.py 薄包装同一实现（monkeypatch bs.VL_TOML 后直测无参调用）
    orig = bs.VL_TOML
    bs.VL_TOML = toml
    try:
        assert svc._vl_toml_uses_local_brain() is False
    finally:
        bs.VL_TOML = orig


def test_local_api_base_is_local(tmp_path, bs):
    """[chat].api_base 指向 http://127.0.0.1:8002 → 判本地。"""
    toml = _write_toml(tmp_path, _CLOUD_TOML.replace(
        "https://ark.cn-beijing.volces.com/api/v3", "http://127.0.0.1:8002/v1"))
    assert bs.vl_toml_uses_local_brain(toml) is True


def test_fallback_alone_never_counts_as_local(tmp_path, bs):
    """只有 fallback_api_base 指本地、api_base 云端 → 不是本地（审计①最小形）。"""
    assert "fallback_api_base = \"http://127.0.0.1:8002/v1\"" in _CLOUD_TOML
    toml = _write_toml(tmp_path, _CLOUD_TOML)
    assert bs.vl_toml_uses_local_brain(toml) is False


@pytest.mark.parametrize("text", [
    "",  # 空文件
    "not [valid toml",  # TOML 损坏
    "[bridge]\nport = 6199\n",  # 无 [chat] 段
    "[chat]\napi_base = \"\"\n",  # api_base 空串
])
def test_read_failures_fall_back_to_cloud(tmp_path, bs, text):
    """文件缺失/损坏/键缺一律回退 False（按云端处理，看护不炸）。"""
    assert bs.vl_toml_uses_local_brain(tmp_path / "missing.toml") is False
    toml = _write_toml(tmp_path, text)
    assert bs.vl_toml_uses_local_brain(toml) is False


def test_cmd_status_structured_verdict(tmp_path, bs, capsys, monkeypatch):
    """cmd_status 同口径：云端配置（含 fallback 默认值）显示「云端 ark」。"""
    toml = _write_toml(tmp_path, _CLOUD_TOML)
    monkeypatch.setattr(bs, "VL_TOML", toml)
    monkeypatch.setattr(bs, "_port_listening", lambda port, timeout=2.0: False)
    monkeypatch.setattr(bs, "_vl_pids", lambda repo=None, runner=None: [])
    bs.cmd_status()
    out = capsys.readouterr().out
    assert "云端" in out
    assert "本地 Qwen2.5-7B" not in out


def test_brain_guard_syncs_with_uses_local_verdict(svc):
    """大脑看护 判定同步：uses_local=False（云端）时即使 8002 探活也复位不拉。

    审计①的看护侧后果：cmd_cloud 切回云端后，被污染的判定让看护每跳重拉
    8002——判定修准后，切云即复位（spawner 永不被调）。
    """
    calls = []
    guard = svc.大脑看护(uses_local_brain=lambda: False,
                           probe=lambda: True, spawner=lambda: calls.append(1))
    guard.state = "degraded"          # 预置任意非 ok 状态（模拟切云前的看护史）
    guard.degraded_since = 0.0
    guard.deaths = [1.0, 2.0]
    guard._clock = lambda: 10_000.0   # 冷却早已满
    guard.tick()
    assert guard.state == "ok"
    assert calls == []                # 复位而非半开重拉
    guard.tick()
    assert calls == []


# ---------------------------------------------------------------------------
# ② writeback：whitelist 必写 + 主人/机器人两套语义
# ---------------------------------------------------------------------------

@pytest.fixture()
def fresh_root(tmp_path):
    from virtuallover.tools.init_config import run_init
    assert run_init(tmp_path) == 0
    return tmp_path


def test_writeback_writes_whitelist_and_clears_placeholder(fresh_root):
    """审计②回归：写回后 whitelist 必含主人号，init 占位 10000 不残留。

    修前漏写 whitelist：门禁 = whitelist ∪ admins（onebot_server.setup），
    残留 ["10000"] + admins 只写机器人号 → 主人消息被桥侧静默忽略（沉默）。
    """
    from virtuallover.tools.setup_writeback import writeback
    from virtuallover.config import load_config
    assert writeback(fresh_root, api_key=_TEST_KEY, qq=_BOT_QQ) == 0
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert cfg.whitelist == [_BOT_QQ]
    assert "10000" not in cfg.whitelist
    assert cfg.admins == [_BOT_QQ]
    assert cfg.session_id.endswith(f":FriendMessage:{_BOT_QQ}")
    assert cfg.bridge.self_id == _BOT_QQ


def test_writeback_owner_and_bot_semantics_split(fresh_root):
    """两套语义分开：session 尾段/admins=主人，self_id=机器人，whitelist 两号。"""
    from virtuallover.tools.setup_writeback import writeback
    from virtuallover.config import load_config
    assert writeback(fresh_root, api_key=_TEST_KEY,
                     qq=_BOT_QQ, owner_qq=_OWNER_QQ) == 0
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert cfg.session_id.endswith(f":FriendMessage:{_OWNER_QQ}")
    assert cfg.admins == [_OWNER_QQ]
    assert sorted(cfg.whitelist) == [_BOT_QQ, _OWNER_QQ]   # 宽松模式两号都加白
    assert "10000" not in cfg.whitelist
    assert cfg.bridge.self_id == _BOT_QQ


def test_writeback_owner_requires_bot_qq(fresh_root):
    """只给主人号不给机器人号 → 拒绝（self_id 与反向 WS 代写都靠机器人号）。"""
    from virtuallover.tools.setup_writeback import writeback
    assert writeback(fresh_root, owner_qq=_OWNER_QQ) == 1


def test_writeback_invalid_owner_rejected_without_write(fresh_root):
    from virtuallover.tools.setup_writeback import writeback
    vl_toml = fresh_root / "local" / "config" / "vl.toml"
    before = vl_toml.read_text(encoding="utf-8")
    assert writeback(fresh_root, qq=_BOT_QQ, owner_qq="12a45") == 1
    assert vl_toml.read_text(encoding="utf-8") == before


def test_writeback_same_owner_and_bot_is_loose_mode(fresh_root):
    """owner==bot（单号自测）→ 与宽松单号模式一致，不产生重复加白。"""
    from virtuallover.tools.setup_writeback import writeback
    from virtuallover.config import load_config
    assert writeback(fresh_root, qq=_BOT_QQ, owner_qq=_BOT_QQ) == 0
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert cfg.whitelist == [_BOT_QQ]
    assert cfg.admins == [_BOT_QQ]
    assert cfg.bridge.self_id == _BOT_QQ


# ---------------------------------------------------------------------------
# ③ 多副本隔离：_vl_pids 双匹配 / read_console_port
# ---------------------------------------------------------------------------

def test_vl_pids_scoped_to_repo(bs):
    """假 CIM 三行：仓库+virtuallover 收；只 virtuallover（他副本）排除；
    仓库但非 vl 排除。修前第二行会被误杀（多副本互杀）。"""
    repo = r"C:\repo\Virtual-lover"
    other = r"C:\repo\other-copy"

    def fake_runner(root: str):
        assert root == repo  # 谓词按仓库绝对路径匹配
        return [
            ("101", rf"{repo}\.venv\Scripts\python.exe -m virtuallover"),  # 强认领
            ("202", rf"{other}\.venv\Scripts\python.exe -m virtuallover"),  # 他副本
            ("303", rf"{repo}\scripts\brain_switch.py status"),  # 非 vl
        ]

    assert bs._vl_pids(repo=repo, runner=fake_runner) == ["101"]


def test_vl_pids_bare_module_claimed_via_port(bs):
    """第 2 层兜底：无 .venv 部署形态（PATH python -m virtuallover，命令行
    不含仓库路径）+ 监听 vl 端口 → 认领。修前（审计③第一版只留路径双匹配）
    生产机被判「未运行」：stop 杀不到、status 假象、看护双拉。"""
    repo = r"C:\repo\Virtual-lover"

    def fake_runner(root: str):
        return [("38216", r"C:\tool\anaconda3\python.exe -m virtuallover")]

    assert bs._vl_pids(repo=repo, runner=fake_runner,
                       port_owner=lambda ports: {"38216"}) == ["38216"]


def test_vl_pids_bare_module_without_port_not_claimed(bs):
    """裸模块形态但端口不在手（他副本的 vl 抢到了端口）→ 不认领（宁漏杀
    不误杀：本副本 stop/restart 不得动他副本拉起并持有端口的 vl）。"""
    repo = r"C:\repo\Virtual-lover"

    def fake_runner(root: str):
        return [("38216", r"C:\tool\anaconda3\python.exe -m virtuallover")]

    assert bs._vl_pids(repo=repo, runner=fake_runner,
                       port_owner=lambda ports: set()) == []


def test_vl_pids_bare_module_non_module_shape_ignored(bs):
    """命令行含 virtuallover 但不是 ``-m virtuallover`` 裸模块形态（如仓库
    外脚本引用包名）→ 兜底层不认领（端口在收也不行）。"""
    repo = r"C:\repo\Virtual-lover"

    def fake_runner(root: str):
        return [("404", r"C:\tool\anaconda3\python.exe C:\elsewhere\use_vl_lib.py"
                        " --tag virtuallover")]

    assert bs._vl_pids(repo=repo, runner=fake_runner,
                       port_owner=lambda ports: {"404"}) == []


def test_port_owner_output_bytes(bs):
    """R7 真机回归（R6-R8 验证链 a 实测复现）：_port_owner_pids 修前直接对
    subprocess stdout（bytes）.split()——bytes.isdigit() 恒真，返回 {b'14436'}
    这样的 bytes 集合；_vl_pids 第 3 层 str in bytes 集合恒 False → 无 pid
    文件的无 .venv 形态 vl 永远认领不到，stop 杀不到（真机 stop 输出无 vl 行、
    8787/6199 仍监听）。解析收敛进 _parse_port_owner_output 后锁死三形态。"""
    assert bs._parse_port_owner_output(b"14436\r\n14437\r\n") == {"14436", "14437"}
    assert bs._parse_port_owner_output("  80 \n") == {"80"}
    assert bs._parse_port_owner_output(None) == set()
    assert bs._parse_port_owner_output(b"") == set()
    assert bs._parse_port_owner_output(b"junk\n12345") == {"12345"}
    # 元素必须是 str——_vl_pids 的 `pid in owners` 才能命中
    out = bs._parse_port_owner_output(b"38216")
    assert out == {"38216"} and all(type(p) is str for p in out)


def test_vl_pids_strong_match_wins_without_port(bs):
    """强认领优先：.venv 形态即使端口还没监听（启动中）也收——兜底层只在
    强认领空转时才参与。"""
    repo = r"C:\repo\Virtual-lover"

    def fake_runner(root: str):
        return [("101", rf"{repo}\.venv\Scripts\python.exe -m virtuallover")]

    assert bs._vl_pids(repo=repo, runner=fake_runner,
                       port_owner=lambda ports: set()) == ["101"]
    # 端口谓词不应被调用（强认领短路）
    def boom(ports):
        raise AssertionError("强认领命中时不应查端口")
    assert bs._vl_pids(repo=repo, runner=fake_runner, port_owner=boom) == ["101"]


def test_vl_pids_pid_file_claim_beats_port_window(bs, tmp_path, monkeypatch):
    """第 2 层：vl.pid 文件认领——spawn 已写 pid、vl 还没监听端口（启动早期）
    也能认领，端口谓词不必可用。修前此窗口被看护误判死亡 → 双拉抢端口
    （r4_final 全新安装真机复现）。"""
    repo = r"C:\repo\Virtual-lover"
    monkeypatch.setattr(bs, "VL_PID", tmp_path / "vl.pid")

    def fake_runner(root: str):
        return [("38216", r"C:\tool\anaconda3\python.exe -m virtuallover")]

    # pid 文件不存在 → 退到端口层
    assert bs._vl_pids(repo=repo, runner=fake_runner,
                       port_owner=lambda ports: {"38216"}) == ["38216"]
    # pid 文件在且 pid 活着（在 CIM 行里）→ 直接认领，端口谓词不被调用
    monkeypatch.setattr(bs, "VL_PID", tmp_path / "vl.pid")
    Path(tmp_path / "vl.pid").write_text("38216", encoding="ascii")

    def boom(ports):
        raise AssertionError("pid 文件认领命中时不应查端口")
    assert bs._vl_pids(repo=repo, runner=fake_runner, port_owner=boom) == ["38216"]
    # pid 文件指向已死 pid（不在 CIM 行里）→ 不认领，退回端口层
    Path(tmp_path / "vl.pid").write_text("99999", encoding="ascii")
    assert bs._vl_pids(repo=repo, runner=fake_runner,
                       port_owner=lambda ports: set()) == []


def test_vl_absent_confirm_needs_two_streaks(svc):
    """看护 vl 判死确认：单跳判空不拉（查询抖动吸收），连续 2 跳才拉；
    中途判活即清零。修前单跳即拉——r4_final 真机双拉竞态的直接根因。"""
    confirm = svc.VlAbsentConfirm()
    assert confirm.observe(True) is False
    assert confirm.observe(False) is False      # 第 1 跳判空：不拉
    assert confirm.streak == 1
    assert confirm.observe(True) is False       # 判活清零
    assert confirm.streak == 0
    assert confirm.observe(False) is False
    assert confirm.observe(False) is True       # 连续第 2 跳：拉
    assert confirm.streak == 0                  # 拉起后清零
    assert confirm.observe(False) is False      # 重新计数


def test_vl_absent_confirm_min_one(svc):
    """confirm 最小为 1（防手滑配 0——0 会让每跳都拉）。"""
    assert svc.VlAbsentConfirm(confirm=0).confirm == 1


def test_vl_pids_escapes_quotes_in_repo(bs):
    """仓库路径含单引号：不再进 PowerShell -like 字面量（Python 侧子串匹配），
    原路径原样传给 runner 即可。"""
    seen = {}

    def fake_runner(root: str):
        seen["root"] = root
        return []
    assert bs._vl_pids(repo=r"C:\o'brien\vl", runner=fake_runner) == []
    assert seen["root"] == r"C:\o'brien\vl"


def test_read_console_port_from_toml(tmp_path, bs):
    toml = _write_toml(tmp_path, _CLOUD_TOML.replace("port = 8787", "port = 9911"))
    assert bs.read_console_port(toml) == 9911


def test_read_console_port_defaults(tmp_path, bs):
    assert bs.read_console_port(tmp_path / "missing.toml") == 8787          # 缺文件
    assert bs.read_console_port(_write_toml(tmp_path, "bad [toml")) == 8787  # 损坏
    assert bs.read_console_port(_write_toml(tmp_path, "[chat]\napi_base = \"x\"\n")) == 8787  # 缺段
    bad = _write_toml(tmp_path, _CLOUD_TOML.replace("port = 8787", "port = 99999"))
    assert bs.read_console_port(bad) == 8787                                 # 非法端口


# ---------------------------------------------------------------------------
# ④ 控制台关 SO_REUSEADDR
# ---------------------------------------------------------------------------

def test_console_server_disallows_address_reuse(tmp_path):
    from virtuallover.console import app as console_app
    from virtuallover.console import auth as console_auth
    from virtuallover.console import core as console_core
    assert console_app.ConsoleHTTPServer.allow_reuse_address is False

    cfg = console_core.AppConfig.for_root(tmp_path, port=0)
    auth_mgr = console_auth.AuthManager(cfg.console_path)
    auth_mgr.load()
    httpd = console_app.make_server("127.0.0.1", 0, cfg, auth=auth_mgr)
    try:
        assert httpd.allow_reuse_address is False
        port = httpd.server_address[1]
        # 端口已被存活监听占用（真起一次 serve 再抢绑定）：关 SO_REUSEADDR 后
        # 第二个 bind 必须 OSError fail-fast（修前 Windows 上静默共存双监听）。
        import threading
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with pytest.raises(OSError):
                console_app.make_server("127.0.0.1", port, cfg, auth=auth_mgr)
        finally:
            httpd.shutdown()
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# ⑤ 路径可配置化 / ⑥ SYS_PY
# ---------------------------------------------------------------------------

def test_sys_py_is_current_interpreter(bs):
    """⑥ 修前硬编码 C:\\tool\\anaconda3\\python.exe，换机兜底必炸。"""
    assert bs.SYS_PY == Path(sys.executable)


def test_path_overrides_from_chat(bs):
    assert bs.path_overrides_from_chat({}) == {}
    ov = bs.path_overrides_from_chat({
        "fallback_gguf": r"E:\models\q4.gguf",
        "fallback_llama_exe": r"E:\llama\server.exe",
        "fallback_convert_python": r"E:\env\py.exe",
        "fallback_model": "不管它",   # 非 path 键忽略
    })
    assert ov["gguf_q4"] == Path(r"E:\models\q4.gguf")
    assert ov["llama_exe"] == Path(r"E:\llama\server.exe")
    assert ov["convert_env_py"] == Path(r"E:\env\py.exe")
    assert len(ov) == 3


def test_refresh_paths_from_vl_toml_applies_and_rebuilds_args(tmp_path, bs):
    """⑤ 全链：vl.toml [chat] 覆盖 → 模块级路径重绑 + LLAMA_ARGS 随 GGUF 重建。"""
    toml = _write_toml(tmp_path, _CLOUD_TOML.replace(
        "[console]",
        'fallback_gguf = "E:\\\\models\\\\q4.gguf"\n'
        'fallback_llama_exe = "E:\\\\llama\\\\server.exe"\n'
        'fallback_convert_python = "E:\\\\env\\\\py.exe"\n\n[console]'))
    orig = (bs.GGUF_Q4, bs.LLAMA_EXE, bs.CONVERT_ENV_PY, bs.LLAMA_ARGS)
    try:
        ov = bs.refresh_paths_from_vl_toml(toml)
        assert set(ov) == {"gguf_q4", "llama_exe", "convert_env_py"}
        assert str(bs.GGUF_Q4) == r"E:\models\q4.gguf"
        assert str(bs.LLAMA_EXE) == r"E:\llama\server.exe"
        assert str(bs.CONVERT_ENV_PY) == r"E:\env\py.exe"
        assert bs.LLAMA_ARGS[bs.LLAMA_ARGS.index("-m") + 1] == r"E:\models\q4.gguf"
        assert bs.LLAMA_ARGS[bs.LLAMA_ARGS.index("--port") + 1] == str(bs.SERVER_PORT)
        # 缺文件/损坏 → 返回空且不动全局
        assert bs.refresh_paths_from_vl_toml(tmp_path / "no.toml") == {}
    finally:
        bs.GGUF_Q4, bs.LLAMA_EXE, bs.CONVERT_ENV_PY, bs.LLAMA_ARGS = orig
    assert ov  # 静默守卫：走到这里说明覆盖确实发生过


# ---------------------------------------------------------------------------
# ⑦ cmd_start 云端分支桩流（模拟云端新装，不碰真实进程/容器）
# ---------------------------------------------------------------------------

def _stub_cmd_start_env(svc, bs, monkeypatch, toml: Path, probed: list):
    """打桩 napcat/vl/HTTP 与文件定位：cmd_start 的全家桶外全替换。"""
    monkeypatch.setattr(bs, "VL_TOML", toml)
    monkeypatch.setattr(svc, "napcat_up", lambda *a, **k: None)
    monkeypatch.setattr(svc, "vl_start", lambda: None)
    monkeypatch.setattr(svc, "_http_alive",
                        lambda url, timeout=2.0: probed.append(url) or False)
    calls = {"gguf": 0, "server": 0}
    monkeypatch.setattr(bs, "ensure_gguf", lambda: calls.__setitem__("gguf", calls["gguf"] + 1))
    monkeypatch.setattr(bs, "start_llama_server",
                        lambda: calls.__setitem__("server", calls["server"] + 1))
    return calls


def test_cmd_start_cloud_fresh_install_never_touches_gguf(tmp_path, svc, bs,
                                                          monkeypatch, capsys):
    """审计①端到端桩流：云端新装（init 骨架）→ start 走云端分支，不炸。"""
    from virtuallover.tools.init_config import run_init
    assert run_init(tmp_path) == 0
    toml = tmp_path / "local" / "config" / "vl.toml"
    probed: list = []
    calls = _stub_cmd_start_env(svc, bs, monkeypatch, toml, probed)
    svc.cmd_start()
    out = capsys.readouterr().out
    assert calls == {"gguf": 0, "server": 0}          # 修前这里 ensure_gguf SystemExit
    assert "云端（不启动本地 llama-server）" in out
    assert any(url.startswith("http://127.0.0.1:8787/api/auth/whoami")
               for url in probed)                     # 就绪探测读 [console].port
    assert "控制台：http://127.0.0.1:8787" in out


def test_cmd_start_probe_follows_console_port(tmp_path, svc, bs, monkeypatch, capsys):
    """[console].port=9911 → 就绪探测与展示地址都跟 9911（修前硬编码 8787）。"""
    toml = _write_toml(tmp_path, _CLOUD_TOML.replace("port = 8787", "port = 9911"))
    probed: list = []
    calls = _stub_cmd_start_env(svc, bs, monkeypatch, toml, probed)
    svc.cmd_start()
    out = capsys.readouterr().out
    assert calls == {"gguf": 0, "server": 0}
    assert any(url.startswith("http://127.0.0.1:9911/api/auth/whoami")
               for url in probed)
    assert "控制台：http://127.0.0.1:9911" in out


def test_cmd_start_local_branch_still_brings_llama(tmp_path, svc, bs,
                                                   monkeypatch, capsys):
    """反向守护：真本地配置时照旧带起 llama-server（不因判定收紧而漏拉）。"""
    toml = _write_toml(tmp_path, _CLOUD_TOML.replace(
        "https://ark.cn-beijing.volces.com/api/v3", "http://127.0.0.1:8002/v1"))
    probed: list = []
    calls = _stub_cmd_start_env(svc, bs, monkeypatch, toml, probed)
    # v2 通用化：local_brain_configured() 需要路径非空且文件存在
    fake_gguf = tmp_path / "q4.gguf"
    fake_exe = tmp_path / "llama-server.exe"
    fake_gguf.write_bytes(b"fake")
    fake_exe.write_bytes(b"fake")
    monkeypatch.setattr(bs, "GGUF_Q4", fake_gguf)
    monkeypatch.setattr(bs, "LLAMA_EXE", fake_exe)
    svc.cmd_start()
    out = capsys.readouterr().out
    assert calls == {"gguf": 1, "server": 1}
    assert "检测到本地大脑配置" in out


def test_init_skeleton_whitelist_placeholder_present(tmp_path):
    """审计②前半实证：init 骨架 whitelist=["10000"] 占位（写回漏写即残留）。"""
    from virtuallover.tools.init_config import run_init
    assert run_init(tmp_path) == 0
    with open(tmp_path / "local" / "config" / "vl.toml", "rb") as fh:
        data = tomllib.load(fh)
    assert data["whitelist"] == ["10000"]
    assert data["admins"] == ["10000"]
    assert data["bridge"]["self_id"] == "10000"
    assert data["chat"]["api_base"].startswith("https://")   # 云端预设
    assert data["chat"]["fallback_api_base"] == "http://127.0.0.1:8002/v1"
