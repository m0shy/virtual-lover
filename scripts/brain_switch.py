# -*- coding: utf-8 -*-
"""本地/云端大脑切换器（一键启动本地大脑 + 一键切回云端）。

用法：
  python scripts/brain_switch.py local   # 切本地：起 llama-server(8002) + 改 vl.toml + 重启 vl
  python scripts/brain_switch.py cloud   # 切回云端：恢复 vl.toml + 重启 vl（需 ark 账户可用）
  python scripts/brain_switch.py status  # 只看当前状态

本地大脑：任意 GGUF 模型（作者测试用 Qwen2.5-7B-Instruct；通用基座即可，不作人格）。
首次运行自动做 GGUF 转换+量化（本地转换环境，约 10-15 分钟）。
云端原配置备份在 vl.toml.cloud-backup，切回时恢复。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VL_TOML = REPO / "local" / "config" / "vl.toml"
BACKUP = VL_TOML.with_suffix(".toml.cloud-backup")

# 本地大脑路径：全部从 vl.toml [chat] 读取（fallback_gguf / fallback_llama_exe /
# fallback_convert_python），默认空——通用性设计：任何机器 clone 后不配置本地大脑
# 也能正常运行（纯云端模式）；要启用本地大脑需在 vl.toml 或控制台配置路径。
# 此处 Path("") 仅作占位，实际使用前 _check_local_brain_configured() 会验证。
GGUF_DIR = Path("")  # 已弃用（兼容旧引用），实际路径见 GGUF_Q4
GGUF_Q4 = Path("")   # vl.toml [chat].fallback_gguf
GGUF_F16 = Path("")  # 转换中间产物（由 GGUF_Q4 推导，量化完即删）
GGUF_Q4_LEGACY_NAME = "qwen2.5-7b-instruct-Q4_K_M.gguf"  # 旧名：存量迁移认领用
HF_MODEL = Path("")  # vl.toml [chat].fallback_hf_model（可选：自动转换 GGUF 用）
LLAMA_CPP = Path("") # 已弃用
LLAMA_EXE = Path("") # vl.toml [chat].fallback_llama_exe
CONVERT_ENV_PY = Path("")  # vl.toml [chat].fallback_convert_python（可选）
SYS_PY = Path(sys.executable)

SERVER_PORT = 8002          # 8001 预留给其他本地服务，不可占用
LOCAL_API_BASE = f"http://127.0.0.1:{SERVER_PORT}/v1"
# R7（FUSION_SPEC §3.3）：--alias 取中性进程标记——LLAMA_MARKER 击杀核验靠
# 命令行含 "local-brain"，本常量经 --alias 落进命令行，两处必须同源。
# 实测（llama.cpp b11040）：单模型 server 忽略请求体 model 字段，客户端带旧名
# "qwen2.5-7b-instruct" 也能对话，因此改名零破坏。
LOCAL_MODEL = "local-brain"
# fallback_api_key 强度下限：低于此长（含占位 "local"）视为弱 key，拉起前重生成
LOCAL_API_KEY_MIN_LEN = 24
# --api-key 值（模块级：refresh_paths_from_vl_toml 从 vl.toml [chat] 回填，
# ensure_local_api_key 生成后回填并重建 LLAMA_ARGS）
LLAMA_API_KEY = ""


def _build_llama_args() -> "list[str]":
    """llama-server 启动参数（引用 GGUF_Q4——路径覆盖重绑后须重建）。

    R7（FUSION_SPEC §3.3）：LLAMA_API_KEY 非空时追加 --api-key（值单源
    vl.toml [chat].fallback_api_key）——本地大脑不再是无鉴权端口，本机任意
    进程都能 127.0.0.1:8002 白嫖对话/读上下文。空 key=不武装（老 llama-server
    或 toml 不可写的兜底，探测面 /health 不受鉴权影响）。
    """
    args = [
        "-m", str(GGUF_Q4),
        "-ngl", "99",                 # 8GB 卡全载 ~4.4G；KV q8_0 ~1.4G，总 ~5.8G
        "-c", "6144",
        
        "-t", "8", "--host", "127.0.0.1", "--port", str(SERVER_PORT),
        "--alias", LOCAL_MODEL,
    ]
    if LLAMA_API_KEY:
        args += ["--api-key", LLAMA_API_KEY]
    return args


LLAMA_ARGS = _build_llama_args()


# ---------------------------------------------------------------------------
# vl.toml 结构化读取（五轮修缮 R4）：本地大脑判定 / 控制台端口 / 路径覆盖
# ---------------------------------------------------------------------------

def read_chat_api_base(vl_toml=None) -> "str | None":
    """vl.toml [chat].api_base（tomllib 结构化读）；文件缺失/损坏/键缺返回 None。

    R4 判定精确化的根：修前全文子串匹配 '127.0.0.1:8002'——而 dump_config
    会把 [chat].fallback_api_base 的默认值（恰为 http://127.0.0.1:8002/v1，
    config.py ChatConfig 字段默认）一并写进 vl.toml，云端新装被判成"指本地"。
    """
    path = Path(vl_toml) if vl_toml is not None else VL_TOML
    try:
        with open(path, "rb") as fh:
            chat = tomllib.load(fh).get("chat") or {}
    except (OSError, ValueError):   # 缺文件 / TOML 损坏
        return None
    base = str(chat.get("api_base") or "").strip()
    return base or None


def vl_toml_uses_local_brain(vl_toml=None) -> bool:
    """vl.toml 当前是否指向本地大脑：[chat].api_base 以 http://127.0.0.1:8002 开头。

    只认 [chat].api_base 本身——fallback_api_base 是降级预案端点，与「当前
    大脑在哪」无关（修前全文子串匹配被它污染：云端新装误判本地 → cmd_start
    走 ensure_gguf 在无模型的机器 SystemExit 死亡、cmd_cloud 后看护反复重拉
    8002）。读失败回退 False（按云端处理，不探 8002）——service 看护周期
    调用，文件瞬时不读不能炸。vl_toml 可注入（pytest 直测 tmp 配置）。
    """
    base = read_chat_api_base(vl_toml)
    return bool(base) and base.startswith(f"http://127.0.0.1:{SERVER_PORT}")


def read_console_port(vl_toml=None, default: int = 8787) -> int:
    """控制台端口：vl.toml [console].port（tomllib）；缺失/损坏/非法回退 default。

    R4：cmd_start 就绪探测与 restart_vl 就绪等待修前硬编码 8787——控制台改过
    端口的机器上 vl 明明就绪却永远等不到探测，白等 30-45s。
    """
    path = Path(vl_toml) if vl_toml is not None else VL_TOML
    try:
        with open(path, "rb") as fh:
            console = tomllib.load(fh).get("console") or {}
        port = int(console.get("port", default))
    except (OSError, ValueError, TypeError):
        return default
    return port if 0 < port < 65536 else default


def path_overrides_from_chat(chat: dict) -> dict:
    """从 [chat] 表提取三条可配置路径（键缺/空值忽略）：gguf_q4 / llama_exe /
    convert_env_py——R4 轻量可配置化，报错文案指引同款键名。"""
    out: dict = {}
    for key, name in (("fallback_gguf", "gguf_q4"),
                      ("fallback_llama_exe", "llama_exe"),
                      ("fallback_convert_python", "convert_env_py")):
        v = str(chat.get(key) or "").strip()
        if v:
            out[name] = Path(v)
    return out


def apply_path_overrides(overrides: dict) -> None:
    """把路径覆盖应用到模块级 GGUF_Q4 / LLAMA_EXE / CONVERT_ENV_PY / LLAMA_ARGS。

    LLAMA_ARGS 的 -m 引用 GGUF_Q4，路径重绑后必须重建，否则 server 仍按旧
    模型路径启动。service.py 经 bs.属性 运行时取值，重绑即全局生效。
    """
    global GGUF_Q4, LLAMA_EXE, CONVERT_ENV_PY, LLAMA_ARGS
    GGUF_Q4 = overrides.get("gguf_q4") or GGUF_Q4
    LLAMA_EXE = overrides.get("llama_exe") or LLAMA_EXE
    CONVERT_ENV_PY = overrides.get("convert_env_py") or CONVERT_ENV_PY
    LLAMA_ARGS = _build_llama_args()


def refresh_paths_from_vl_toml(vl_toml=None) -> dict:
    """读 vl.toml [chat] 的路径覆盖并应用；文件缺失/损坏保持默认（返回 {}）。

    模块加载时执行一次（bat/控制台动作子进程每次都是新解释器，即取即生效）；
    pytest 直测可传 vl_toml 注入 tmp 配置（记得恢复原值）。
    R7：同时回填 [chat].fallback_api_key → LLAMA_API_KEY 并重建 LLAMA_ARGS
    （--api-key 武装本地大脑），本进程内后续 ensure_local_api_key 覆盖同全局。
    """
    path = Path(vl_toml) if vl_toml is not None else VL_TOML
    try:
        with open(path, "rb") as fh:
            chat = tomllib.load(fh).get("chat") or {}
    except (OSError, ValueError):
        return {}
    overrides = path_overrides_from_chat(chat)
    if overrides:
        apply_path_overrides(overrides)
    global LLAMA_API_KEY, LLAMA_ARGS
    key = str(chat.get("fallback_api_key") or "").strip()
    if key:
        LLAMA_API_KEY = key
        LLAMA_ARGS = _build_llama_args()
    return overrides


refresh_paths_from_vl_toml()   # 模块加载即生效


def local_brain_configured() -> bool:
    """本地大脑是否已配置（GGUF 与 llama-server 路径均非空且存在）。"""
    return bool(str(GGUF_Q4).strip() and GGUF_Q4.is_file()
                and str(LLAMA_EXE).strip() and LLAMA_EXE.is_file())


def _local_brain_setup_guide() -> str:
    """本地大脑未配置时的用户指引（多行，含具体 vl.toml 键名）。"""
    return (
        "本地大脑未配置。要启用，在 vl.toml [chat] 段配置以下两键（或控制台→服务管理→路径设置）：\n"
        "  fallback_gguf = \"<GGUF 模型文件绝对路径>\"      # 例如 D:\\models\\local-brain-q4.gguf\n"
        "  fallback_llama_exe = \"<llama-server 绝对路径>\" # 例如 D:\\llama.cpp\\cuda\\llama-server.exe\n"
        "模型获取：从 HuggingFace 下载任意 GGUF 格式 LLM（推荐 Qwen2.5-7B-Instruct Q4_K_M）。\n"
        "llama-server 获取：https://github.com/ggml-org/llama.cpp/releases\n"
        "不配置本地大脑也完全可用——纯云端模式即可（当前就是）。"
    )

# ---------------------------------------------------------------------------
# 进程核验 / 看护探活 / 运行日志轮转（五轮修缮 R2）
# ---------------------------------------------------------------------------

VL_RUNTIME_LOG = REPO / "local" / "vl_data" / "vl_runtime.log"
SERVICE_MONITOR_PID = REPO / "local" / "vl_data" / "service_monitor.pid"
LLAMA_PID = REPO / "local" / "vl_data" / "brain-local.pid"   # R6 中性化（原 llama_server_8002.pid）
VL_PID = REPO / "local" / "vl_data" / "vl.pid"   # vl 主进程 pid（spawn 时写，stop 清）

# 命令行核验标记（照抄 voice/lifecycle._CMDLINE_MARKER 的防误杀语义）：
# pid 文件里的数字可能已被 OS 复用给无关进程——杀前命令行不含标记一律不杀。
DAEMON_MARKER = "_daemon"        # 看护：... \scripts\service.py _daemon
# R7（FUSION_SPEC §3.3）：进程标记中性化——改为 --alias 落进命令行的
# LOCAL_MODEL 常量（上游 exe 名 llama-server.exe 不改：外部工具不改名不重编译）。
# 两处必须同源：改 LOCAL_MODEL 时这里同步，否则击杀核验恒拒杀。
LLAMA_MARKER = "local-brain"
VL_MARKER = "virtuallover"       # vl 主进程：... python.exe -m virtuallover

LOG_MAX_BYTES = 5 * 1024 * 1024  # Popen 重定向日志轮转阈值（对齐 lifecycle 5MB×3）
LOG_BACKUP_COUNT = 3
LOCAL_CONTEXT_TOKENS = 4096  # server -c 6144，扣 system/记忆注入余量
CLOUD_CONTEXT_TOKENS = 32768
# （R7 移除：此处原有一份硬编码 LLAMA_ARGS 字面量会在模块加载末尾把
# refresh_paths_from_vl_toml 刚应用的路径/密钥覆盖静默冲回默认值——潜在 bug，
# 一并清理；LLAMA_ARGS 唯一权威 = _build_llama_args()。）


def _log(msg: str) -> None:
    """进度输出。调用方可能是控制台的 brain 动作子进程：restart_vl 杀掉 vl
    （含控制台父进程）后本脚本成为孤儿，管道读端随父进程关闭——继续 print
    会抛 OSError 把 local/cloud 流程打断在「拉起新 vl」之前（有看护时靠看护
    兜底，无看护时 vl 直接死）。吞掉断管类 OSError，保证切换流程走完。"""
    try:
        print(msg, flush=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# pid 击杀核验门 / 看护探活 / 日志轮转（五轮修缮 R2）
# ---------------------------------------------------------------------------

def _cmdline_of(pid: int) -> "str | None":
    """按 pid 读进程命令行（PowerShell CIM）；读不到（已退出/权限）返回 None。

    照抄 voice/lifecycle._default_cmdline_reader 的 Windows 支路：wmic 新系统
    已移除，统一走 CIM；返回 None = 进程不存在，是核验链「不杀」的一种。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    line = (out.stdout or "").strip()
    if not line or line.lower() == "null":
        return None
    return line


def kill_pid_verified(pid: str, marker: str, label: str, *,
                      reader=None, killer=None) -> str:
    """pid 击杀核验门（照抄 voice/lifecycle.kill_own_sync：宁可不杀不可误杀）。

    返回四种结果，调用方据以决定 pid 文件去留：
      killed   核验通过并已 taskkill；
      gone     命令行读不到（进程已死/权限不足）——不用杀，可清 pid 文件；
      refused  命令行不含 marker（疑似 pid 复用成无关进程）——拒绝击杀，
               调用方应保留 pid 文件供人工排查；
      failed   核验通过但 taskkill 失败——保留 pid 文件下次再试。
    reader/killer 可注入（pytest 用假件直测，不碰真实进程）。
    """
    reader = reader or _cmdline_of
    killer = killer or (lambda p: subprocess.run(
        ["taskkill", "/F", "/PID", str(p)], capture_output=True).returncode == 0)
    pid = str(pid).strip()
    if not pid.isdigit():
        _log(f"{label}：pid {pid!r} 非法，拒绝击杀")
        return "refused"
    cmdline = reader(int(pid))
    if cmdline is None:
        _log(f"{label}：pid {pid} 命令行不可读（进程可能已退出），跳过击杀")
        return "gone"
    if marker.lower() not in cmdline.lower():
        _log(f"{label}：命令行核验失败，拒绝击杀 pid {pid}"
             f"（cmdline 不含 {marker!r}）——请人工确认后处理")
        return "refused"
    ok = bool(killer(pid))
    _log(f"{label}已停止（pid {pid}）" if ok else f"{label}：taskkill 失败（pid {pid}）")
    return "killed" if ok else "failed"


def daemon_alive(*, pid_path=None, reader=None) -> bool:
    """看护探活双门：pid 文件在 + 该 pid 命令行确为看护（含 service.py 与 _daemon）。

    修前 vl_start / cmd_status 只看 pid 文件在不在——看护死后遗留的 pid 文件、
    或 pid 被 OS 复用给无关进程，都会误报「含看护」，vl 从此裸奔无自愈（R2）。
    pid_path / reader 可注入（pytest 用 tmp_path + 假命令行直测）。
    """
    path = Path(pid_path) if pid_path is not None else SERVICE_MONITOR_PID
    reader = reader or _cmdline_of
    try:
        pid = path.read_text(encoding="ascii").strip()
    except OSError:
        return False
    if not pid.isdigit():
        return False
    cmdline = reader(int(pid))
    if not cmdline:
        return False
    low = cmdline.lower()
    return "service.py" in low and DAEMON_MARKER in low


def open_runtime_log(path: Path):
    """打开运行日志追加句柄；超 5MB 先按 .1/.2/.3 换名轮转（保留 3 代）。

    vl_runtime.log / brain-local.log 是 Popen stdout 重定向目标，需要
    真实文件句柄，不能走 logging.RotatingFileHandler（voice/lifecycle
    _open_log_with_rotation 同款裁定）——修前三处 open("ab") 裸追加无限涨。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            for i in range(LOG_BACKUP_COUNT - 1, 0, -1):
                src = Path(str(path) + f".{i}")
                dst = Path(str(path) + f".{i + 1}")
                if src.exists():
                    os.replace(src, dst)
            os.replace(path, Path(str(path) + ".1"))
    except OSError:
        pass  # 轮转失败不阻塞启动，继续追加
    return open(path, "ab")


# ---------------------------------------------------------------------------
# llama-server 生命周期
# ---------------------------------------------------------------------------

# 禁代理直连 opener（R2 集成修复）：本机系统代理（注册表 ProxyEnable=1 且
# ProxyOverride 不含 127.0.0.1）会把发往 127.0.0.1:8002 的探活/探话全部劫持
# 回 404——「活的大脑」被误判死、「死的云端」被误判活。回环流量一律直连。
# 与 virtuallover/net.py 同语义（scripts/ 独立运行，不能 import 包内模块）。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _direct_urlopen(target, timeout=None, **kwargs):
    return _DIRECT_OPENER.open(target, timeout=timeout, **kwargs)


def _port_listening(port: int, timeout: float = 2.0) -> bool:
    try:
        with _direct_urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return True  # 有响应即活
    except Exception:
        return False


def _llama_alive(port: int, timeout: float = 2.0) -> bool:
    """llama-server 探活（R7）：GET /health 有响应即活（模型加载期 503 也算活）。

    修前打 /v1/models——--api-key 武装后未带 Bearer 一律 401，探活从「语义
    判定」退化成「恰好 401 也是响应」的偶然正确；/health 是 llama.cpp 的
    健康端点且**免鉴权**（实测 b11040：armed 后 /health 带 Bearer wrong 仍
    200，加载期 503），语义恢复正交。仅供看护/状态「进程死活」判定；「模型
    加载完」的严格判定归 _server_ready。
    """
    try:
        with _direct_urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return True  # 加载期 503 也是响应=活
    except Exception:
        return False  # 连接拒绝/超时=未起


def _server_accepts_key(port: int, timeout: float = 2.0) -> bool:
    """当前 LLAMA_API_KEY 能否通过该实例的 /v1/models 鉴权（R7 复用前核验）。

    未武装 server（无 --api-key）忽略 Authorization 头 → 200 → True（复用）；
    armed 且 key 匹配 → 200 → True；armed 但 key 不匹配/未带 → 401 → False
    （调用方停旧重拉，否则 chat_probe 必 401）。连接失败按 False（调用方的
    复用分支本来就不会进）。
    """
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models")
        if LLAMA_API_KEY:
            req.add_header("Authorization", f"Bearer {LLAMA_API_KEY}")
        with _direct_urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def _server_ready(port: int, timeout: float = 2.0, urlopen=None) -> bool:
    """llama-server 真就绪判定：GET /health 必须 200（R7 改自 /v1/models）。

    与 _llama_alive 的差别：llama-server 先开监听、模型加载完才对外服务，
    加载期 /health 回 503——「有响应」≠「能对话」。start_llama_server 的
    就绪等待与 chat_probe 必须用本判定，否则探活提前放行、chat_probe 撞 503
    把整个 local 流程炸死在 switch/restart 之前（实测复现）。
    改 /health 的第二原因（R7）：--api-key 武装后 /v1/models 必须带 Bearer
    （实测未带 401），不带 key 的就绪探测恒 False=120s 等待必超时；/health
    免鉴权（armed 实测 200），就绪判定与密钥解耦。
    """
    urlopen = urlopen or _direct_urlopen  # 默认禁代理直连（回环流量不走系统代理）
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False  # 503=模型加载中；任何非 2xx 都不算就绪
    except Exception:
        return False  # 连接拒绝/超时=未起


def adopt_legacy_gguf() -> bool:
    """R6 品牌中性化的存量迁移小步：生效 GGUF_Q4 不在盘、其同目录旧名文件在
    → 就地改名认领 + vl.toml [chat].fallback_gguf 回写新路径。

    模型文件不复制不重下（4.7GB），只 rename；认领失败（文件被占用等）不阻塞
    调用方——报错文案已指引手工兜底。返回是否完成认领。
    """
    legacy = GGUF_Q4.with_name(GGUF_Q4_LEGACY_NAME)
    if GGUF_Q4.is_file() or not legacy.is_file() or legacy == GGUF_Q4:
        return False
    try:
        legacy.rename(GGUF_Q4)
    except OSError as exc:
        _log(f"旧名模型认领失败（{exc}）——请手动改名为 {GGUF_Q4.name}，"
             "或把 vl.toml [chat] fallback_gguf 指向实际文件")
        return False
    _log(f"已把旧名模型认领为 {GGUF_Q4.name}")
    try:
        _write(_patch_section(_read(), "chat", {"fallback_gguf": str(GGUF_Q4)}))
        _log("vl.toml [chat].fallback_gguf 已同步回写")
    except OSError:
        pass  # 回写失败不影响认领本体（下次 refresh 读到旧路径再认领一次也无害）
    return True


def ensure_local_api_key() -> str:
    """本地大脑 --api-key 单源保障（R7，FUSION_SPEC §3.3）：读 vl.toml
    [chat].fallback_api_key；缺失/占位 "local"/长度不足 LOCAL_API_KEY_MIN_LEN
    视为弱 key → secrets.token_urlsafe(32) 重生成并写回 vl.toml。

    返回生效 key（可能为空=vl.toml 不可读写，此时不武装、退回旧行为——
    探测面 /health 与未带 key 的客户端在无鉴权 server 上照常工作）。幂等：
    强 key 原样返回，绝不每次改写（客户端/服务端两侧同源才通）。
    cmd_local / service.cmd_start / service._spawn_llama_server 在拉起
    llama-server 前调用；本函数同时回填 LLAMA_API_KEY 全局并重建 LLAMA_ARGS，
    start_llama_server / chat_probe / service._spawn_llama_server 经模块全局
    取到同一把 key。
    """
    global LLAMA_API_KEY, LLAMA_ARGS
    current = LLAMA_API_KEY
    if not current:
        try:
            with open(VL_TOML, "rb") as fh:
                chat = tomllib.load(fh).get("chat") or {}
            current = str(chat.get("fallback_api_key") or "").strip()
        except (OSError, ValueError):
            current = ""
    if current and current != "local" and len(current) >= LOCAL_API_KEY_MIN_LEN:
        if LLAMA_API_KEY != current:
            LLAMA_API_KEY = current
            LLAMA_ARGS = _build_llama_args()
        return current
    new_key = secrets.token_urlsafe(32)
    try:
        _write(_patch_section(_read(), "chat", {"fallback_api_key": new_key}))
    except (OSError, SystemExit) as exc:
        # SystemExit=_patch_section 找不到 [chat].fallback_api_key 键（老结构
        # toml）——不武装不阻塞：返回空让调用方按无鉴权老路径走
        _log(f"[R7] fallback_api_key 写回失败（{exc}），本地大脑不武装 --api-key")
        return ""
    LLAMA_API_KEY = new_key
    LLAMA_ARGS = _build_llama_args()
    _log(f"[R7] 已生成强 api-key 并写回 vl.toml [chat].fallback_api_key"
         f"（长度 {len(new_key)}，不回显）——本地大脑将带 --api-key 武装")
    return new_key


def ensure_gguf() -> None:
    """GGUF 不存在则尝试旧名认领/自动转换（均需配置路径）。"""
    if not str(GGUF_Q4).strip():
        _log(_local_brain_setup_guide())
        raise SystemExit("本地大脑未配置（fallback_gguf 为空）")
    if not GGUF_Q4.is_file():
        adopt_legacy_gguf()
    if GGUF_Q4.is_file():
        return
    if not str(HF_MODEL).strip() or not HF_MODEL.is_dir():
        _log(f"未找到 GGUF 模型 {GGUF_Q4}")
        _log("提示：在 vl.toml [chat].fallback_gguf 配置正确路径，")
        _log("或在 [chat].fallback_hf_model 配置 HF 模型目录以启用自动转换。")
        raise SystemExit("GGUF 模型不存在且无法自动转换")
    if not CONVERT_ENV_PY.is_file():
        raise SystemExit(
            f"未找到本地转换环境 {CONVERT_ENV_PY}。"
            "自带转换环境时，把其 python.exe 完整路径写进 vl.toml [chat] "
            "fallback_convert_python 后重试")
    _log("[1/3] HF → GGUF F16 转换（约 5-15 分钟）…")
    r = subprocess.run([str(CONVERT_ENV_PY), str(LLAMA_CPP / "py" / "convert_hf_to_gguf.py"),
                        str(HF_MODEL), "--outfile", str(GGUF_F16), "--outtype", "f16"],
                       cwd=str(LLAMA_CPP))
    if r.returncode != 0:
        raise SystemExit("F16 转换失败（见上方输出）")
    _log("[2/3] 量化 Q4_K_M（约 3-8 分钟）…")
    r = subprocess.run([str(LLAMA_CPP / "cuda" / "llama-quantize.exe"),
                        str(GGUF_F16), str(GGUF_Q4), "Q4_K_M"], cwd=str(LLAMA_CPP))
    if r.returncode != 0:
        raise SystemExit("量化失败（见上方输出）")
    try:
        GGUF_F16.unlink()  # 中间产物 ~15GB，量化完即删
        _log("      已删除 F16 中间产物")
    except OSError:
        pass
    _log(f"[3/3] GGUF 就绪：{GGUF_Q4}")


def start_llama_server() -> None:
    """8002 有应答即复用；否则无窗口后台启动并等就绪。

    R7：复用探测走 _llama_alive（/health，免鉴权）——armed server 的
    /v1/models 未带 Bearer 恒 401，探活语义不再依赖「401 也是响应」的偶然。
    复用前再过一道 _server_accepts_key：在跑实例不认当前 key（旧武装/换 key）
    时停旧重拉，否则复用后 chat_probe 必 401。
    """
    if _llama_alive(SERVER_PORT):
        if _server_accepts_key(SERVER_PORT):
            _log(f"llama-server 已在 {SERVER_PORT} 端口运行（复用）")
            return
        _log("8002 已有 llama-server 但不认当前 api-key（旧实例/换过 key）→ 核验停止后重拉")
        stop_llama_server()
        if _llama_alive(SERVER_PORT):
            raise SystemExit(
                "8002 旧实例无法经 pid 文件核验停止（外部手工拉起？）——"
                "请手动停止该进程后重试")
        time.sleep(1)   # 端口释放窗口
    if not GGUF_Q4.is_file():
        raise SystemExit(
            f"GGUF 不存在：{GGUF_Q4}（先运行转换；模型放在别的盘/目录时，"
            "把实际路径写进 vl.toml [chat] fallback_gguf 后重试）")
    if not LLAMA_EXE.is_file():
        raise SystemExit(
            f"llama-server 不存在：{LLAMA_EXE}。自编译/绿色版用户把实际路径"
            "写进 vl.toml [chat] fallback_llama_exe 后重试")
    _log(f"启动 llama-server（127.0.0.1:{SERVER_PORT}，模型加载约 10-30s）…")
    log_file = REPO / "local" / "vl_data" / "logs" / "brain-local.log"   # R6 中性化（原 llama_server_8002.log）
    proc = subprocess.Popen(
        [str(LLAMA_EXE)] + LLAMA_ARGS,
        cwd=str(LLAMA_EXE.parent),
        stdout=open_runtime_log(log_file), stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    LLAMA_PID.write_text(str(proc.pid), encoding="ascii")
    for i in range(60):
        time.sleep(2)
        if _server_ready(SERVER_PORT):
            _log(f"llama-server 就绪（pid {proc.pid}，{2*(i+1)}s）")
            return
        if proc.poll() is not None:
            raise SystemExit(f"llama-server 启动失败退出码 {proc.returncode}，日志：{log_file}")
    raise SystemExit("llama-server 120s 未就绪，查看日志：" + str(log_file))


def stop_llama_server() -> None:
    """停 8002：pid 文件 + 命令行核验双门（修前裸 taskkill，pid 复用即误杀）。

    refused（核验不过）/failed（taskkill 失败）保留 pid 文件供人工排查与重试；
    killed/gone 才清文件。"""
    if not LLAMA_PID.is_file():
        return
    pid = LLAMA_PID.read_text(encoding="ascii").strip()
    if not pid.isdigit():
        LLAMA_PID.unlink(missing_ok=True)
        return
    outcome = kill_pid_verified(pid, LLAMA_MARKER, "llama-server")
    if outcome in ("killed", "gone"):
        LLAMA_PID.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# vl.toml 切换
# ---------------------------------------------------------------------------

def _read() -> str:
    return VL_TOML.read_text(encoding="utf-8")


def _write(s: str) -> None:
    VL_TOML.write_text(s, encoding="utf-8")


def _patch_section(text: str, section: str, updates: "dict[str, str]") -> str:
    """替换 [section] 下指定键值（保留其他键与注释结构尽量少动）。"""
    lines = text.splitlines(keepends=False)
    out, in_sec = [], False
    patched = set()
    for ln in lines:
        st = ln.strip()
        if st.startswith("[") and st.endswith("]"):
            in_sec = st == f"[{section}]"
        elif in_sec and "=" in st:
            key = st.split("=", 1)[0].strip()
            if key in updates:
                v = updates[key]
                literal = v if isinstance(v, (int, float)) and not isinstance(v, bool) else f'"{v}"'
                out.append(f"{key} = {literal}")
                patched.add(key)
                continue
        out.append(ln)
    if not patched and updates:
        # 段不存在则追加
        out.append("")
        out.append(f"[{section}]")
        for k, v in updates.items():
            out.append(f'{k} = "{v}"')
    missing = set(updates) - patched
    if missing:
        raise SystemExit(f"vl.toml [{section}] 未找到键：{sorted(missing)}（文件结构变化？）")
    return "\n".join(out) + "\n"


def switch_toml_local() -> None:
    """切本地大脑：[chat].api_base/model/api_key 三键补丁 + 上下文窗口键。

    R7（FUSION_SPEC §3.3）：弃写死常量 'local'——api_key 改取
    ensure_local_api_key() 的单源值（= llama-server --api-key 同一把），
    弱/缺则先强生成。arm 后本地客户端发占位 'local' 会被 401（server 要求
    Bearer），客户端与服务端必须同 key。key 写不进 toml（老结构）时回退
    'local'（此时 server 也不武装，两侧一致仍可对话）。
    """
    if not BACKUP.is_file():
        shutil.copy2(VL_TOML, BACKUP)
        _log(f"云端原配置已备份：{BACKUP.name}")
    key = ensure_local_api_key() or "local"
    text = _read()
    text = _patch_section(text, "chat", {
        "api_base": LOCAL_API_BASE, "model": LOCAL_MODEL, "api_key": key})
    text = _patch_section(text, "context", {"max_context_tokens": LOCAL_CONTEXT_TOKENS})
    _write(text)
    _log(f"vl.toml 已切本地大脑（api_base={LOCAL_API_BASE}，上下文 {LOCAL_CONTEXT_TOKENS}，"
         "api_key=本地大脑单源 key）")


def switch_toml_cloud() -> None:
    """切回云端：只从备份读回 chat 四键 + context 窗口键做补丁恢复。

    R5 终审修复：原实现整文件覆盖会静默回滚用户经控制台 PUT 写入的全部
    配置（人格/语音/情绪映射等）。改为 _patch_section 对称补丁——备份只
    提供 chat 与 context 两节的恢复源，其余节保持用户当前值。
    """
    if not BACKUP.is_file():
        _log("无云端备份（从未切过本地？），vl.toml 保持现状")
        return
    backup_text = BACKUP.read_text(encoding="utf-8")
    backup_chat = _read_section_values(backup_text, "chat",
        ("api_base", "model", "api_key", "thinking"))
    backup_ctx = _read_section_values(backup_text, "context",
        ("max_context_tokens",))
    if not backup_chat:
        _log("备份中未找到 [chat] 节，回退为提示（不覆盖）")
        return
    current = _read()
    patched = _patch_section(current, "chat", backup_chat)
    if backup_ctx:
        patched = _patch_section(patched, "context", backup_ctx)
    _write(patched)
    _log("vl.toml 已补丁式恢复云端大脑配置（仅 chat/context 键，其余用户配置保留）")


def _read_section_values(text: str, section: str, keys: "tuple[str, ...]") -> "dict[str, str]":
    """从 TOML 文本读取指定节的指定键值（简单行解析，值保留原字面量）。"""
    out: "dict[str, str]" = {}
    in_sec = False
    for ln in text.splitlines():
        st = ln.strip()
        if st.startswith("[") and st.endswith("]"):
            in_sec = st == f"[{section}]"
            continue
        if in_sec and "=" in st:
            key, _, val = st.partition("=")
            key = key.strip()
            if key in keys:
                out[key] = val.strip()
    return out


# ---------------------------------------------------------------------------
# vl 进程重启
# ---------------------------------------------------------------------------

def _cim_python_rows() -> "list[tuple[str, str]]":
    """CIM 查询：全部 python 进程 [(pid, commandline)]（谓词在 Python 侧判）。

    修前在 PowerShell 侧 -like 双过滤只回 pid——「仓库路径 + virtuallover」
    双匹配把无 .venv 部署形态（PATH/anaconda 解释器直拉 ``-m virtuallover``，
    命令行不含仓库路径）判空。现在拉全量行回 Python 侧分层认领；超时 30s
    ——CIM 偶发卡顿不能把看护循环拖死。
    """
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, timeout=30)
    out = out.stdout if hasattr(out, "stdout") else out
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    return [(str(r.get("ProcessId")), r.get("CommandLine") or "") for r in rows]


def _parse_port_owner_output(raw) -> "set[str]":
    """PowerShell 输出 → pid 字符串集合（str 元素）。

    R7 真机修复（R6-R8 验证链 a 实测复现）：修前直接对 subprocess 的
    ``stdout``（bytes）做 ``.split()``——``bytes.isdigit()`` 存在且恒真，
    集合元素成了 ``b'14436'``；_vl_pids 第 3 层拿 str ``in`` bytes 集合
    恒 False → 无 pid 文件的无 .venv 形态 vl 永远认领不到：stop 杀不到、
    status 假象（真机复现：stop 输出无 vl 行，8787/6199 仍监听）。
    统一在此 decode + str 化，入参兼容 bytes/str/None。
    """
    if raw is None:
        return set()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return {p for p in str(raw).split() if p.isdigit()}


def _port_owner_pids(ports: "list[int]") -> "set[str]":
    """监听指定 TCP 端口的进程 pid 集合（Get-NetTCPConnection；查询失败=空集）。

    只在 _vl_pids 的兜底层使用：vl 内嵌桥(6199)/控制台(8787)两监听，端口
    全机唯一——抢到端口的 vl 全机最多一个，按端口认领天然多副本安全。
    解析归 _parse_port_owner_output（bytes 缺陷回归测试锁在
    test_install_fixes.test_port_owner_output_bytes）。
    """
    if not ports:
        return set()
    where = " -or ".join(f"$_.LocalPort -eq {int(p)}" for p in ports)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
             f"Where-Object {{ {where} }} | "
             "Select-Object -ExpandProperty OwningProcess"],
            capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return _parse_port_owner_output(out)


_BARE_VL_MODULE_RE = re.compile(r"-m\s+['\"]?virtuallover['\"]?\s*$")


def _claims_repo_path(cmdline: str, repo_nc: str) -> bool:
    """命令行是否以本仓库路径开头（R7 路径边界匹配，FUSION_SPEC §3.4）。

    修前是裸子串 ``repo_low in cmd.lower()``——``C:\\repo\\Virtual-lover-v2``
    这类**前缀同名**副本的命令行会被本仓库误认领（跨副本误杀）。修后：
    normcase（Windows 大小写/斜杠归一）后必须以 ``repo + 分隔符`` 开头；
    先去引号（CIM 命令行的解释器路径常带引号，``"C:\\repo\\…\\python.exe"``
    以引号开头会直接破坏 startswith）。
    """
    return os.path.normcase(cmdline.replace('"', '')).startswith(repo_nc + os.sep)


def _vl_pids(repo=None, *, runner=None, port_owner=None) -> "list[str]":
    """本仓库的 vl 进程 pid 列表（两层认领；多副本隔离不回退）。

    第 1 层（强认领）：命令行以本仓库绝对路径**开头（含路径边界，R7）**且
    含 'virtuallover'——.venv 解释器 / 仓库脚本形态（命令行自带仓库路径）；
    他副本天然不匹配，隔离即达成（审计③）。修前裸子串匹配会把前缀同名副本
    （``<repo>-v2`` 等）误认领成本仓库进程，stop/restart 跨副本误杀。

    第 2 层（端口兜底）：命令行是裸 ``<解释器> -m virtuallover``（不含仓库
    路径）且该 pid 正监听 vl 内嵌端口（[bridge].port + [console].port，
    文件读失败按 6199/8787）。修前（审计③第一版）只留第 1 层，隐含「vl 由
    仓库 .venv 拉起」前提——生产机无 .venv 时 _host_python 回退 PATH python
    拉起，命令行不含仓库路径 → 她被判「未运行」：stop 杀不到、status 假象、
    看护每 10s 双拉新 vl 抢端口。端口全机唯一，兜底认领天然多副本安全。

    repo / runner / port_owner 可注入（pytest 假件直测，不碰真实进程）。
    """
    root = Path(repo).resolve() if repo is not None else REPO
    repo_nc = os.path.normcase(str(root))
    rows = runner(str(root)) if runner is not None else _cim_python_rows()
    vl_rows = [(pid, cmd) for pid, cmd in rows
               if pid.isdigit() and "virtuallover" in cmd.lower()]
    strong = [pid for pid, cmd in vl_rows if _claims_repo_path(cmd, repo_nc)]
    if strong:
        return strong
    bare = [pid for pid, cmd in vl_rows if _BARE_VL_MODULE_RE.search(cmd.strip())]
    # 第 2 层：pid 文件认领（spawn 即写，早于 vl 监听端口——封住「启动早期
    # 端口未监听 + 端口查询偶发空」的误判窗口；pid 必须仍在 CIM 行里=活且
    # 命令行含 virtuallover，防 pid 复用）。
    try:
        pf_pid = VL_PID.read_text(encoding="ascii").strip()
    except OSError:
        pf_pid = ""
    if pf_pid.isdigit() and pf_pid in bare:
        return [pf_pid]
    # 第 3 层：端口归属（存量/手工拉起、无 pid 文件时）——桥(6199)+控制台
    # ([console].port，读失败按 8787)；端口全机唯一，兜底天然多副本安全。
    ports = [6199, read_console_port()]
    if port_owner is None:
        port_owner = _port_owner_pids
    owners = port_owner(ports)
    return [pid for pid in bare if pid in owners]


def _host_python() -> str:
    """重启 vl 用的解释器：仓库根 .venv 优先（与 bat / service.py 同一口径），
    无 .venv 才退回 SYS_PY=当前解释器（R4：修前硬编码 anaconda 路径，换机必炸）。
    vl 的依赖只装在 .venv，部署机应先跑 setup.bat 建 .venv。"""
    venv = REPO / ".venv" / "Scripts" / "python.exe"
    return str(venv if venv.is_file() else SYS_PY)


def write_vl_pid(pid: int) -> None:
    """vl 主进程 pid 落盘（_vl_pids 兜底层第 2 认领锚；spawn 即写）。

    无 .venv 部署形态（PATH python 直拉 ``-m virtuallover``）命令行不含仓库
    路径，强认领不中；端口归属要等 vl 监听后才有效——pid 文件把认领窗口
    提前到 spawn 瞬间，看护 10s 巡检不再把「启动中」误判成「死了」。
    """
    try:
        VL_PID.parent.mkdir(parents=True, exist_ok=True)
        VL_PID.write_text(str(int(pid)), encoding="ascii")
    except OSError:
        pass   # pid 文件写失败只是退化到端口认领，不阻断拉起


def restart_vl() -> None:
    """重启 vl（切大脑后生效）。竞态消除（R2）：

    - 看护在岗（daemon_alive 双门判定）→ 只杀 vl，等看护按 10s 巡检拉起，
      **绝不自行 Popen**——修前双拉竞态：看护 10s 内拉起新 vl 的同时本函数又
      Popen 一个，两个 vl 抢 6199/8787 端口，先到者活后到者僵尸；
    - 看护死 → 杀完自行拉起兜底（与修前同路径）；
    - 杀前一律命令行核验（kill_pid_verified，宁可不杀不可误杀）。
    """
    for pid in _vl_pids():
        kill_pid_verified(pid, VL_MARKER, "vl 进程")
    if daemon_alive():
        _log("看护在岗：等待其拉起新 vl（10s 巡检周期）…")
        console_port = read_console_port()
        for i in range(45):  # 看护巡检 ≤10s + vl 启动就绪 15-30s
            time.sleep(1)
            if _vl_pids() and _port_listening(console_port):
                _log(f"vl 已由看护拉起就绪（{(i + 1)}s）")
                return
        _log("警告：看护 45s 未拉起就绪的 vl，检查 local/vl_data/vl_runtime.log")
        return
    _log("看护不在：自行拉起 vl（兜底路径）")
    proc = subprocess.Popen([_host_python(), "-m", "virtuallover"], cwd=str(REPO),
                            stdout=open_runtime_log(VL_RUNTIME_LOG), stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    write_vl_pid(proc.pid)
    console_port = read_console_port()
    for _ in range(30):
        time.sleep(1)
        if _port_listening(console_port):
            _log(f"vl 已重启就绪（{console_port}/6199）")
            return
    _log("警告：vl 30s 未就绪，检查 local/vl_data/vl_runtime.log")


def chat_probe(max_wait_seconds: int = 90, urlopen=None) -> str:
    """对当前生效的 chat provider 发一次真实对话（小句）验证整脑可用。

    503（模型仍在加载）在 max_wait_seconds 预算内每 2s 重试——就绪判定与
    首个请求之间仍有加载窗口；非 503 的 HTTP 错误照旧立即抛。
    R7：LLAMA_API_KEY 非空时带 Authorization: Bearer（--api-key 武装后的
    server 对 /v1/chat/completions 强制校验，不带必 401）。
    """
    urlopen = urlopen or _direct_urlopen  # 默认禁代理直连（chat_probe 打 127.0.0.1）
    deadline = time.time() + max_wait_seconds
    while True:
        headers = {"Content-Type": "application/json"}
        if LLAMA_API_KEY:
            headers["Authorization"] = f"Bearer {LLAMA_API_KEY}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{SERVER_PORT}/v1/chat/completions",
            data=json.dumps({"model": LOCAL_MODEL,
                             "messages": [{"role": "user", "content": "用五个字打个招呼"}],
                             "max_tokens": 24}).encode(),
            headers=headers, method="POST")
        t0 = time.time()
        try:
            with urlopen(req, timeout=120) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 503 and time.time() < deadline:
                time.sleep(2)
                continue
            raise
        reply = d["choices"][0]["message"].get("content", "")[:60]
        ntok = d.get("usage", {}).get("completion_tokens", "?")
        _log(f"本地大脑对话验证：{time.time()-t0:.1f}s {ntok}tok 「{reply}」")
        return reply


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def cmd_local() -> None:
    if not local_brain_configured():
        _log(_local_brain_setup_guide())
        return
    # R7：key 单源保障在最前——server 拉起（--api-key 进 argv）与 chat_probe
    # （Bearer 头）都靠 LLAMA_API_KEY 全局就位
    ensure_local_api_key()
    ensure_gguf()
    start_llama_server()
    chat_probe()
    switch_toml_local()
    restart_vl()
    _log("\n她现在用本地大脑说话（本地模型，127.0.0.1:8002，已带 --api-key 鉴权）。")
    _log("切回云端：管理控制台「引擎与人格」页「切回云端大脑」按钮，或 QQ 里发 /brain cloud（需云账户可用）")


def cmd_cloud() -> None:
    stop_llama_server()
    switch_toml_cloud()
    restart_vl()
    _log("\n已切回云端大脑（ark doubao）。本地 llama-server 已停止。")


def cmd_stop() -> None:
    """只停本地 llama-server（8002），不动 vl.toml（大脑配置保持现状）。"""
    stop_llama_server()
    _log("本地 llama-server 已停止（vl.toml 未改动；若她正在用本地大脑，"
         "重新启动用 一键启动.bat 或管理控制台「引擎与人格」页「切到本地大脑」按钮）")


def cmd_status() -> None:
    # R4 判定精确化：结构化读 [chat].api_base（修前全文子串匹配被 dump 必写的
    # fallback_api_base 默认值污染——云端配置也显示「本地」）。
    in_local = vl_toml_uses_local_brain()
    _log(f"vl.toml 大脑：{'本地' if in_local else '云端'}")
    _log(f"llama-server(8002)：{'运行中' if _llama_alive(SERVER_PORT) else '未运行'}")
    if local_brain_configured():
        _log(f"GGUF：{'已就绪' if GGUF_Q4.is_file() else '路径已配但文件不存在'}")
    else:
        _log("本地大脑：未配置（纯云端模式——要启用见 vl.toml [chat] fallback_gguf/fallback_llama_exe）")
    _log(f"vl 进程：{'运行中' if _vl_pids() else '未运行'}")


def cmd_restart() -> None:
    """只重启 vl 主进程（不动 llama-server/容器/语音）——控制台 vl_restart 用。"""
    restart_vl()
    _log("vl 主进程已重启（大脑/容器/语音引擎未动）")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="本地/云端大脑一键切换")
    p.add_argument("cmd", choices=["local", "cloud", "stop", "status", "restart"])
    args = p.parse_args(argv)
    {"local": cmd_local, "cloud": cmd_cloud, "stop": cmd_stop,
     "status": cmd_status, "restart": cmd_restart}[args.cmd]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
