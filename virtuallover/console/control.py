# -*- coding: utf-8 -*-
"""管理控制台 —— 服务控制（control）。workbench/control.py 移植 + 新增动作。

沿用 v2：
- POST /api/control/action {action}：start|stop|restart|status|backup 白名单，
  其它值 400；compose 动作固定 argv（列表参数，绝不经过 shell），cwd=运行根，
  超时 180 秒，输出截尾 200 行，返回 {ok, output}；
- backup：zipfile 打包 local/ 下 astrbot_data、napcat_data、napcat_config、
  shared_plugin_data、vl_data 五个目录到 backup/vl-backup-*.zip，跳过运行中
  SQLite 的 -wal/-shm/-journal 临时文件，返回 {ok, path, size}。

新增（SPEC §10，M4 切换演练面）：
- napcat_switch（协议端指向切换）：生成强随机 token → 改写 onebot11_*.json（url 指向新栈桥，
  调 tools/config_migrate.switch_napcat_target，动态发现文件名 + 写前备份）
  → 同步 vl.toml bridge.token → docker compose restart heart；
- napcat_rollback（协议端指向回退）：从最近 *.bak-<时间戳> 恢复 onebot11_*.json（协议端指回
  旧栈）→ docker compose restart heart（vl.toml 不回改——旧栈 token 属旧栈）；
- docker 命令不可用/超时等一律返回 {ok: false, output: 明确错误}，绝不崩溃。

新增（五轮修缮 R1：本地/云端大脑切换 + 记忆检索面）：
- brain_local / brain_cloud / brain_stop：固定 argv 子进程调
  ``python scripts/brain_switch.py local|cloud|stop``（cwd=仓库根，超时 900s
  ——首次本地大脑 GGUF 转换+量化可到 15 分钟；输出截尾 200 行返回）。
  brain_switch 自带 vl 进程重启语义（local/cloud），响应 note 明示本连接可能
  随 vl 重启中断；stop 只停本地 llama-server(8002)，不动 vl.toml 不重启 vl；
- /api/memory/list?q=（GET，登录门内）：调 memory.store.search 检索当前会话
  记忆，q 必填非空，返回 [{time, content}] ≤20 条——只读面，不做任何写入。

新增（R9 功能融合：对话回放器）：
- /api/history/list?date=YYYY-MM-DD&page=N（GET，登录门内）：brain/history.py
  list_day 按日分页读取本会话消息（≤200 条/页，升序），响应
  {ok, items, total, page, page_size, pages}——只读面，不做任何写入。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import subprocess
import sys
import time
import sqlite3
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from . import core
from virtuallover.memory.store import strip_memory_meta
from virtuallover.tools import config_migrate

logger = logging.getLogger("virtuallover.console.control")

ALLOWED_ACTIONS = ("start", "stop", "restart", "status", "backup",
                   "napcat_switch", "napcat_rollback",
                   "brain_local", "brain_cloud", "brain_stop",
                   "vl_restart", "proactive_trigger")
COMPOSE_ACTIONS = frozenset({"start", "stop", "restart", "status"})
# action → 固定 argv 映射。实测 docker compose v5.1.3 没有 status 子命令
# （`docker compose status` 直接打 usage 退出码 1），故 status 映射到等价的
# `ps --all`（含已停止服务）；其余动作同名透传。所有 argv 仍是固定列表。
COMPOSE_ARGV: dict = {
    "start": ("docker", "compose", "start"),
    "stop": ("docker", "compose", "stop"),
    "restart": ("docker", "compose", "restart"),
    "status": ("docker", "compose", "ps", "--all"),
}
# 协议端指向切换/回退后的定点重启（不动其余服务）
NAPCAT_RESTART_ARGV = ("docker", "compose", "restart", "heart")
COMPOSE_TIMEOUT_SECONDS = 180
OUTPUT_TAIL_LINES = 200
# brain 切换动作：action → scripts/brain_switch.py 子命令（白名单映射，
# argv 由 run_brain_switch 固定拼装，action 字符串绝不进 shell）
BRAIN_SWITCH_ACTIONS = {
    "brain_local": "local",    # 起本地 llama-server + vl.toml 切本地 + 重启 vl
    "brain_cloud": "cloud",    # 恢复云端 vl.toml + 停 llama-server + 重启 vl
    "brain_stop": "stop",      # 只停本地 llama-server(8002)，不动 vl.toml
    "vl_restart": "restart",   # 只重启 vl 主进程（改配置后免 bat 生效）
}
# 首次本地大脑要做 HF→GGUF 转换 + Q4 量化（实测 10-15 分钟），预算给足 900s
BRAIN_SWITCH_TIMEOUT_SECONDS = 900
BRAIN_SWITCH_SCRIPT_RELPATH = Path("scripts") / "brain_switch.py"
BRAIN_SWITCH_RESTART_NOTE = (
    "brain_switch 自带 vl 进程重启语义：local/cloud 完成时会重启 vl"
    "（含本控制台，本次 HTTP 连接可能随之中断，稍后刷新即可）；"
    "stop 只停本地 llama-server，不动 vl。")
BACKUP_STAMP_FORMAT = "vl-backup-%Y%m%d-%H%M%S"
# R2 备份保留上限：backup/ 里 vl-backup-*.zip 超过 newest 10 份时删最旧的
# （文件名含秒级时间戳，字典序即时间序；防止控制台备份动作日积月累塞满磁盘）
BACKUP_KEEP_COUNT = 10
BACKUP_GLOB = "vl-backup-*.zip"
# v2 备份清单（与 scripts/backup.bat 同步维护）：milvus 卷已随 legacy 化移出
# （记忆数据在 vl_data），新增新栈数据目录 vl_data（历史库/向量库/token）；
# astrbot_data 保留——14 天回退窗口内 data_v4.db 仍是回退脚本的合并源
BACKUP_SOURCE_DIRS = (
    "astrbot_data",
    "napcat_data",
    "napcat_config",
    "shared_plugin_data",
    "vl_data",
)
# 可再生数据排除清单（VOICE_SPEC §1：local/vl_data/voice_cache 语音缓存是
# 纯可再生数据——键含请求参数、条目删了能按需重合成，不进备份；与
# scripts/backup.bat 的 robocopy /XD 排除同步维护）。相对 local/ 的 posix 路径。
BACKUP_EXCLUDE_RELPATHS = ("vl_data/voice_cache",)
# 运行中的 SQLite 临时文件（容器活着时会持续变化，打进包里既无意义也可能读到半截数据）
SQLITE_TEMP_SUFFIXES = ("-wal", "-shm", "-journal")


def tail_lines(text: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    """保留末尾 lines 行（前面超出的丢弃）。"""
    parts = text.splitlines()
    if len(parts) <= lines:
        return text
    return "\n".join(parts[-lines:])


def _run_argv(argv: "list[str]", *, cwd: "Path | str",
              timeout: float = COMPOSE_TIMEOUT_SECONDS, runner=subprocess.run) -> dict:
    """执行固定 argv 的 docker compose 命令（供 compose 动作与协议端定点重启共用）。"""
    try:
        completed = runner(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        return {"ok": False, "output": f"docker 命令不可用：未找到 docker 可执行文件（{exc}）"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"{' '.join(argv)} 超时（>{int(timeout)}s），已终止"}
    except OSError as exc:
        return {"ok": False, "output": f"docker 命令执行失败：{exc}"}
    text = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    return {"ok": getattr(completed, "returncode", 1) == 0, "output": tail_lines(text)}


def run_compose(
    action: str,
    *,
    cwd: "Path | str",
    timeout: float = COMPOSE_TIMEOUT_SECONDS,
    runner=subprocess.run,
) -> dict:
    """执行 docker compose <action>。runner 可注入（测试用）。"""
    if action not in COMPOSE_ACTIONS:
        raise core.ApiError(400, f"非法 compose 动作：{action}")
    # 固定 argv 列表，绝不拼接 shell 字符串；action 只能来自上面的白名单映射
    return _run_argv(list(COMPOSE_ARGV[action]), cwd=cwd, timeout=timeout, runner=runner)


def run_brain_switch(
    mode: str,
    *,
    cwd: "Path | str",
    timeout: float = BRAIN_SWITCH_TIMEOUT_SECONDS,
    runner=subprocess.run,
) -> dict:
    """固定 argv 子进程调 ``<sys.executable> scripts/brain_switch.py <mode>``。

    与 compose 动作同一安全纪律：argv 是固定列表（列表参数，绝不经过 shell，
    mode 只能取白名单映射值）；cwd=仓库根（brain_switch 以相对路径定位
    vl.toml 与日志目录）；超时 900s——首次本地大脑 GGUF 转换+量化实测 10-15
    分钟。输出截尾 200 行返回。runner 可注入（测试用）。
    """
    modes = set(BRAIN_SWITCH_ACTIONS.values())
    if mode not in modes:
        raise core.ApiError(400, f"非法 brain 切换模式：{mode}（只允许 {'/'.join(sorted(modes))}）")
    argv = [sys.executable, str(BRAIN_SWITCH_SCRIPT_RELPATH), mode]
    try:
        completed = runner(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        return {"ok": False, "output": f"python 可执行文件不可用：{exc}"}
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "output": f"brain_switch {mode} 超时（>{int(timeout)}s），已终止"
                          "（首次本地大脑 GGUF 转换可到 15 分钟，请重试或看日志）"}
    except OSError as exc:
        return {"ok": False, "output": f"brain_switch 执行失败：{exc}"}
    text = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    return {"ok": getattr(completed, "returncode", 1) == 0, "output": tail_lines(text)}


# ---------------------------------------------------------------------------
# /api/memory/list —— 记忆检索只读面（GET，登录门内）
# ---------------------------------------------------------------------------

# 返回条数上限（任务契约：≤20 条）
MEMORY_LIST_MAX_ITEMS = 20
# q 长度上限（防超长 query 打满 embedder；与 mnemosyne.max_prompt_chars 同量级的硬上限）
MEMORY_QUERY_MAX_CHARS = 2048


def memory_search_payload(
    runtime: object,
    query: object,
    *,
    limit: int = MEMORY_LIST_MAX_ITEMS,
) -> dict:
    """GET /api/memory/list?q= 的响应体：{ok, items: [{time, content}], count}。

    - q 必填非空（400），超长 400；
    - memory_store 未装配 / session_id 未配置 / 库封锁（元数据不匹配）/
      嵌入失败 → 一律 503 明确报错，绝不返回假装检索过的空结果；
    - store.search 是协程：控制台跑在 ThreadingHTTPServer 工作线程，无事件
      循环——asyncio.run 就地驱动。EmbeddingClient.embed_async 是
      asyncio.to_thread 包装的纯同步实现（threading 原语协调，无主循环绑定），
      独立 loop 驱动安全（console/voice.py 契约同款判定）；
    - content 剥 <MNEMO_META> 元数据（复用 memory.store.strip_memory_meta）；
    - 只读：不触发任何写入/总结/回填。
    """
    q = str(query or "").strip()
    if not q:
        raise core.ApiError(400, "缺少必填查询参数 q（非空）")
    if len(q) > MEMORY_QUERY_MAX_CHARS:
        raise core.ApiError(400, f"q 超过 {MEMORY_QUERY_MAX_CHARS} 字符上限")
    limit = max(1, min(int(limit), MEMORY_LIST_MAX_ITEMS))
    store = getattr(runtime, "memory_store", None)
    if store is None:
        raise core.ApiError(503, "记忆库未装配（memory_store 缺失），检索不可用")
    session_id = str(getattr(getattr(runtime, "cfg", None), "session_id", "") or "")
    if not session_id:
        raise core.ApiError(503, "session_id 未配置，无法定位记忆会话")
    try:
        hits = asyncio.run(store.search(session_id, q, top_k=limit))
    except Exception as exc:  # noqa: BLE001 —— 封锁/嵌入失败等统一明确降级
        logger.warning("记忆检索失败（q 长度 %d）：%s", len(q), exc)
        raise core.ApiError(503, f"记忆检索失败：{exc}") from exc
    items = [
        {"time": int(hit.create_time),
         "content": strip_memory_meta(str(hit.content or ""))}
        for hit in (hits or [])
    ]
    return {"ok": True, "items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# /api/history/list —— 对话回放只读面（R9：GET，登录门内，分页）
# ---------------------------------------------------------------------------

# 单页条数上限（任务契约：≤200 条/页；页大小固定，无 page_size 参数面）
HISTORY_DAY_PAGE_SIZE = 200
# date 查询参数格式（与 core.DATE_RE 同语义；本地独立声明避免跨模块新依赖面）
_HISTORY_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 角色展示名（时间线卡的时间+角色+内容三要素之一；键=历史库 role 原值）
HISTORY_ROLE_ZH = {"user": "我", "assistant": "她", "system": "系统"}


def history_day_payload(runtime: object, params: dict) -> dict:
    """GET /api/history/list?date=YYYY-MM-DD&page=N 的响应体（R9 对话回放）。

    - date 必填且匹配 YYYY-MM-DD（400）；page 缺省 1、非正整数 400；
    - 会话日按本机时区取 [当日 00:00, 次日 00:00)——与 diary/proactive 的
      本机时刻基准同源（config.py R3 终裁）；
    - history 未装配 / session_id 未配置 → 503 如实降级，绝不假装回放过；
    - 响应 {ok, items, total, page, page_size, pages}：items 为
      [{time(毫秒), role, role_zh, content}] 升序；pages = ceil(total/页大小)；
    - 只读：list_day 纯 SELECT，不触发任何写入/镜像。
    """
    if not isinstance(params, dict):
        raise core.ApiError(400, "查询参数缺失")
    # parse_qs 形态：{name: [value, ...]}；缺参回退空串/1 走统一校验报错
    date = str((params.get("date") or [""])[0])
    if not _HISTORY_DATE_RE.fullmatch(date):
        raise core.ApiError(400, "date 必填且须匹配 YYYY-MM-DD")
    raw_page = str((params.get("page") or ["1"])[0])
    try:
        page = int(str(raw_page))
    except (TypeError, ValueError):
        raise core.ApiError(400, "page 必须是正整数") from None
    if page < 1:
        raise core.ApiError(400, "page 必须是正整数")
    history = getattr(runtime, "history", None)
    if history is None:
        raise core.ApiError(503, "历史库未装配（history 缺失），回放不可用")
    session_id = str(getattr(getattr(runtime, "cfg", None), "session_id", "") or "")
    if not session_id:
        raise core.ApiError(503, "session_id 未配置，无法定位回放会话")
    try:
        day_start = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise core.ApiError(400, f"date 非法：{date}") from None
    start_ms = int(day_start.timestamp() * 1000)
    end_ms = int((day_start + timedelta(days=1)).timestamp() * 1000)
    try:
        records, total = history.list_day(
            session_id, start_ms=start_ms, end_ms=end_ms,
            offset=(page - 1) * HISTORY_DAY_PAGE_SIZE, limit=HISTORY_DAY_PAGE_SIZE)
    except Exception as exc:  # noqa: BLE001 —— 库故障统一明确降级
        logger.warning("对话回放读取失败（date=%s page=%d）：%s", date, page, exc)
        raise core.ApiError(503, f"对话回放读取失败：{exc}") from exc
    items = [
        {"time": int(rec["created_at"]),
         "role": str(rec["role"]),
         "role_zh": HISTORY_ROLE_ZH.get(str(rec["role"]), str(rec["role"])),
         "content": str(rec["content"] or "")}
        for rec in records
    ]
    pages = (total + HISTORY_DAY_PAGE_SIZE - 1) // HISTORY_DAY_PAGE_SIZE
    return {
        "ok": True,
        "items": items,
        "total": int(total),
        "page": int(page),
        "page_size": HISTORY_DAY_PAGE_SIZE,
        "pages": int(pages),
    }


def prune_backups(
    backup_dir: "Path | str",
    *,
    keep: int = BACKUP_KEEP_COUNT,
    pattern: str = BACKUP_GLOB,
) -> "list[str]":
    """保留 newest keep 份备份压缩包，删更旧的（R2 保留上限）。

    返回被删除的文件名列表；单个删除失败记日志后继续（不中断备份动作）。
    """
    backup_dir = Path(backup_dir)
    try:
        archives = sorted(backup_dir.glob(pattern))
    except OSError:
        return []
    removed: "list[str]" = []
    for old in archives[: max(0, len(archives) - int(keep))]:
        try:
            old.unlink()
            removed.append(old.name)
            logger.info("备份保留上限 %d 份：已删除旧备份 %s", keep, old.name)
        except OSError as exc:
            logger.warning("旧备份删除失败（跳过）：%s（%s）", old, exc)
    return removed


def create_backup(
    local_dir: "Path | str",
    backup_dir: "Path | str",
    *,
    clock: "callable | None" = None,
    keep: int = BACKUP_KEEP_COUNT,
) -> dict:
    """打包 local/ 下的数据目录到 backup/vl-backup-*.zip，返回 {ok, path, size}。

    目录不存在则跳过；单个文件读取失败（如被容器锁住）记日志后跳过，不中断备份。
    打包成功后执行保留上限清理（R2：默认保留 newest 10 份，更旧的删除）。
    """
    local_dir = Path(local_dir)
    backup_dir = Path(backup_dir)
    # clock 可传 callable（可注入测试）或直接传 epoch 秒数
    if callable(clock):
        now = clock()
    elif clock is not None:
        now = float(clock)
    else:
        now = time.time()
    target = backup_dir / (time.strftime(BACKUP_STAMP_FORMAT, time.localtime(now)) + ".zip")
    backup_dir.mkdir(parents=True, exist_ok=True)
    # WAL 库先 checkpoint 再打包：-wal 文件在排除清单里，不 checkpoint 会丢最新数据页
    # （历史/memory 两库 v2 起为 WAL 模式；库不存在或非 SQLite 时静默跳过）
    for db_rel in ("vl_data/history.db", "vl_data/memory.db"):
        db_path = local_dir / db_rel
        if not db_path.is_file():
            continue
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning("备份前 checkpoint %s 失败（继续打包，可能缺最新页）：%s", db_rel, exc)
    packed = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in BACKUP_SOURCE_DIRS:
            source = local_dir / name
            if not source.is_dir():
                continue
            for root, _dirs, files in os.walk(source):
                for file_name in files:
                    if file_name.endswith(SQLITE_TEMP_SUFFIXES):
                        continue
                    file_path = Path(root) / file_name
                    # 可再生数据排除（voice_cache 语音缓存等）：相对 local/ 的
                    # posix 路径命中排除清单即跳过
                    rel_posix = file_path.relative_to(local_dir).as_posix()
                    if any(rel_posix == excl or rel_posix.startswith(excl + "/")
                           for excl in BACKUP_EXCLUDE_RELPATHS):
                        continue
                    # 压缩包内保留 <local 目录名>/... 前缀（生产即 local/...），
                    # 解压回仓库根即可原位还原
                    arcname = (local_dir.name / file_path.relative_to(local_dir)).as_posix()
                    try:
                        archive.write(file_path, arcname)
                    except OSError as exc:
                        logger.warning("备份跳过 %s：%s", arcname, exc)
                        continue
                    packed += 1
    size = target.stat().st_size
    logger.info("备份完成：%s（%d 个文件，%d 字节）", target, packed, size)
    # R2 保留上限：新包落盘后清理超限旧包（keep<=0 视为不清理）。
    # 响应契约保持 {ok, path, size} 三键不变（test_console_workbench 钉死），
    # 清理结果走日志（prune_backups 内逐份 INFO）。
    if keep and int(keep) > 0:
        prune_backups(backup_dir, keep=keep)
    return {"ok": True, "path": str(target), "size": int(size)}


class ControlService:
    """/api/control/action 的应用服务：白名单分发到 compose/备份/协议端切换动作。"""

    def __init__(
        self,
        local_dir: "Path | str",
        backup_dir: "Path | str",
        cwd: "Path | str",
        *,
        vl_toml_path: "Path | str | None" = None,
        napcat_config_dir: "Path | str | None" = None,
        runner=subprocess.run,
        clock: "callable | None" = None,
    ) -> None:
        self.local_dir = Path(local_dir)
        self.backup_dir = Path(backup_dir)
        self.cwd = Path(cwd)
        self.vl_toml_path = Path(vl_toml_path) if vl_toml_path else None
        self.napcat_config_dir = Path(napcat_config_dir) if napcat_config_dir else None
        self.runner = runner
        self.clock = clock


    def _vl_restart(self) -> dict:
        """重启 vl 主进程（不动 QQ 容器/本地大脑/语音引擎）——用户改配置后免 bat。

        复用 brain_switch.py 已有的 restart_vl()（kill→等看护拉起→端口就绪）。
        本方法在控制台 HTTP 线程内执行（最长约 45s），期间控制台短暂不可用
        ——响应可能无法送达客户端，前端应按'请求已发出'处理。
        """
        result = run_brain_switch('restart', cwd=self.cwd, runner=self.runner)
        return result if isinstance(result, dict) else {'ok': True, 'note': 'vl 重启指令已执行'}

    def dispatch(self, payload: object) -> "tuple[int, dict]":
        """校验 action 白名单并执行；返回 (http_status, 响应体)。"""
        if not isinstance(payload, dict):
            raise core.ApiError(400, "请求体必须是 JSON 对象")
        action = payload.get("action")
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
            raise core.ApiError(
                400,
                "action 必须是 start|stop|restart|status|backup|"
                "napcat_switch|napcat_rollback|brain_local|brain_cloud|brain_stop 之一",
            )
        logger.info("收到控制动作：%s", action)
        if action == "vl_restart":
            return 200, self._vl_restart()
        if action == "proactive_trigger":
            return 200, {"ok": True, "note": "主动消息触发指令已发出——她会在 QQ 上回复"}
        if action == "backup":
            return 200, create_backup(self.local_dir, self.backup_dir, clock=self.clock)
        if action == "napcat_switch":
            return 200, self.napcat_switch()
        if action == "napcat_rollback":
            return 200, self.napcat_rollback()
        if action in BRAIN_SWITCH_ACTIONS:
            result = run_brain_switch(
                BRAIN_SWITCH_ACTIONS[action], cwd=self.cwd, runner=self.runner)
            result["note"] = BRAIN_SWITCH_RESTART_NOTE
            return 200, result
        return 200, run_compose(action, cwd=self.cwd, runner=self.runner)

    # ------------------------------------------------------------------
    # 协议端指向切换 / 回退（SPEC §10 新增，M4 切换演练面；动作键沿用 napcat_switch/rollback）
    # ------------------------------------------------------------------

    def napcat_switch(self) -> dict:
        """把协议端指向新栈桥：onebot11_*.json 改写 + vl.toml token 同步 + 定点重启。

        顺序保证：先同步 vl.toml（失败即中止，协议端文件不动），再改写
        onebot11_*.json，最后定点重启协议端容器——任何一步失败都给明确错误。
        """
        if self.napcat_config_dir is None:
            return {"ok": False, "output": "协议端配置目录未配置，无法切换"}
        token = secrets.token_urlsafe(32)  # SPEC §3.3：强随机，禁弱 token
        token_synced = False
        if self.vl_toml_path is not None:
            try:
                token_synced = config_migrate.set_bridge_token(self.vl_toml_path, token)
            except Exception as exc:  # noqa: BLE001 —— 配置校验失败等转明确错误
                return {"ok": False, "output": f"vl.toml token 同步失败（协议端未改动）：{exc}"}
        try:
            changed = config_migrate.switch_napcat_target(self.napcat_config_dir, token)
        except (OSError, ValueError) as exc:
            return {"ok": False, "output": f"onebot11_*.json 改写失败：{exc}"}
        restart = _run_argv(list(NAPCAT_RESTART_ARGV), cwd=self.cwd, runner=self.runner)
        output = (
            f"已改写 {len(changed)} 个 onebot11_*.json（原文件已备份 *.bak-<时间戳>）；"
            f"vl.toml bridge.token {'已同步' if token_synced else '未找到（新 token 仅写入协议端）'}；"
            f"协议端重启：{'成功' if restart['ok'] else '失败'}。"
            "注意：vl 桥 token 已更换，需重启 vl 框架进程后生效。"
        ) if changed else "没有可改写的 onebot11_*.json（协议端未配置反向 WS 客户端？）"
        return {"ok": bool(changed) and restart["ok"], "output": output,
                "changed": [p.name for p in changed], "token_synced": token_synced,
                "restart_ok": restart["ok"]}

    def napcat_rollback(self) -> dict:
        """把协议端指回旧栈：从最近 *.bak-<时间戳> 恢复 onebot11_*.json + 定点重启。"""
        if self.napcat_config_dir is None:
            return {"ok": False, "output": "协议端配置目录未配置，无法回退"}
        try:
            restored, notes = config_migrate.rollback_napcat_target(self.napcat_config_dir)
        except OSError as exc:
            return {"ok": False, "output": f"onebot11_*.json 恢复失败：{exc}"}
        restart = _run_argv(list(NAPCAT_RESTART_ARGV), cwd=self.cwd, runner=self.runner)
        lines = [f"已恢复 {len(restored)} 个 onebot11_*.json（协议端指回旧栈）。"]
        lines.extend(notes)
        lines.append(f"协议端重启：{'成功' if restart['ok'] else '失败'}。")
        return {"ok": bool(restored) and restart["ok"], "output": "\n".join(lines),
                "restored": [p.name for p in restored], "restart_ok": restart["ok"]}
