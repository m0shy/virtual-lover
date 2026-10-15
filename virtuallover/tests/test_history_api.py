# -*- coding: utf-8 -*-
"""对话回放器契约测试（R9 功能融合）：brain/history.py 分页读取 + 控制台 HTTP 面。

两层覆盖（FUSION_SPEC §5-R9-3）：
- 存储面 HistoryStore.list_day / first_created_at：按 [start_ms, end_ms) 时间窗
  分页（升序、LIMIT/OFFSET、窗口总数与分页无关）、窗口边界左闭右开、
  量纲=毫秒（与写入精度一致）；first_created_at 取会话最早一条；
- HTTP 面 GET /api/history/list?date=YYYY-MM-DD&page=N：三重门继承
  （未登录 401；GET 无 CSRF 面），date/page 校验 400，历史库/会话未装配 503
  如实降级，≤200 条/页（page_size 固定 200），响应 {ok, items, total, page,
  page_size, pages}；全程只读（前后行数一致，绝不写库）。

HTTP 测试走本机回环 + 临时目录（smart_urlopen 直连禁代理），不发真实外网
请求；session_id 与内容全部占位值，敏感值绝不进测试。
"""

from __future__ import annotations

import datetime
import http.client
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from virtuallover.net import smart_urlopen  # noqa: E402  测试打 127.0.0.1：禁代理直连

from virtuallover.brain.history import HistoryStore  # noqa: E402
from virtuallover.console import app as console_app  # noqa: E402
from virtuallover.console import auth as console_auth  # noqa: E402
from virtuallover.console import control as console_control  # noqa: E402
from virtuallover.console import core as console_core  # noqa: E402

SESSION = "测试人格:FriendMessage:10000"
OTHER = "测试人格:FriendMessage:20000"

DAY_A = datetime.datetime(2026, 10, 10)
DAY_B = datetime.datetime(2026, 10, 11)


def _ms(day: "datetime.datetime", hour: int, minute: int = 0) -> int:
    """本机时区某日某时的毫秒时间戳（与 HistoryStore 写入量纲一致）。"""
    return int(datetime.datetime(day.year, day.month, day.day, hour, minute).timestamp() * 1000)


def _day_bounds(day: "datetime.datetime") -> "tuple[int, int]":
    start = datetime.datetime(day.year, day.month, day.day)
    end = start + datetime.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


@pytest.fixture
def store(tmp_path):
    """临时历史库（含镜像关闭）；测试自负责播种。"""
    s = HistoryStore(tmp_path / "history.db", mirror_path=None)
    yield s
    s.close()


def _seed_two_days(s: HistoryStore) -> None:
    """播种两天会话：A 日 3 条（user/assistant 交替）+ B 日 2 条 + 他会话 1 条。"""
    s.append(SESSION, "user", "早上好", created_at=_ms(DAY_A, 9, 0), mirror=False)
    s.append(SESSION, "assistant", "早安呀，今天也想你。", created_at=_ms(DAY_A, 9, 1), mirror=False)
    s.append(SESSION, "user", "晚上一起看电影吗", created_at=_ms(DAY_A, 19, 30), mirror=False)
    s.append(SESSION, "assistant", "好呀，我选爆米花口味。", created_at=_ms(DAY_B, 10, 0), mirror=False)
    s.append(SESSION, "user", "晚安", created_at=_ms(DAY_B, 22, 0), mirror=False)
    s.append(OTHER, "user", "别的会话不串台", created_at=_ms(DAY_A, 12, 0), mirror=False)


# ---------------------------------------------------------------------------
# 存储面：list_day 分页 / first_created_at
# ---------------------------------------------------------------------------

def test_list_day_returns_window_sorted_with_total(store):
    _seed_two_days(store)
    start, end = _day_bounds(DAY_A)
    records, total = store.list_day(SESSION, start_ms=start, end_ms=end)
    assert total == 3
    assert [r["role"] for r in records] == ["user", "assistant", "user"]
    assert all(start <= r["created_at"] < end for r in records)
    assert records[0]["content"] == "早上好"


def test_list_day_session_isolation(store):
    _seed_two_days(store)
    start, end = _day_bounds(DAY_A)
    records, total = store.list_day(OTHER, start_ms=start, end_ms=end)
    assert total == 1 and records[0]["content"] == "别的会话不串台"


def test_list_day_paging_limit_offset(store):
    _seed_two_days(store)
    start, end = _day_bounds(DAY_A)
    page1, total = store.list_day(SESSION, start_ms=start, end_ms=end, limit=2, offset=0)
    page2, total2 = store.list_day(SESSION, start_ms=start, end_ms=end, limit=2, offset=2)
    assert (len(page1), total) == (2, 3)
    assert (len(page2), total2) == (1, 3)   # total 与分页参数无关
    assert [r["id"] for r in page1 + page2] == [r["id"] for r in page1 + page2]
    assert page1[0]["content"] == "早上好" and page2[0]["content"] == "晚上一起看电影吗"


def test_list_day_window_half_open_and_empty(store):
    _seed_two_days(store)
    start, end = _day_bounds(DAY_A)
    # 左闭右开：恰好等于 end 的消息不属于本窗（B 日 00:00 边界）
    records, total = store.list_day(SESSION, start_ms=start, end_ms=end)
    assert total == 3
    empty, empty_total = store.list_day(SESSION, start_ms=end, end_ms=end)
    assert empty == [] and empty_total == 0
    # start==end 恒空（防呆）
    assert store.list_day(SESSION, start_ms=start, end_ms=start) == ([], 0)


def test_first_created_at_picks_minimum_across_sessions(store):
    _seed_two_days(store)
    assert store.first_created_at(SESSION) == _ms(DAY_A, 9, 0)
    assert store.first_created_at(OTHER) == _ms(DAY_A, 12, 0)
    assert store.first_created_at("测试人格:FriendMessage:30000") is None


# ---------------------------------------------------------------------------
# 载荷层：history_day_payload 校验矩阵（不经 HTTP，直接驱动）
# ---------------------------------------------------------------------------

def _params(raw: str) -> dict:
    """query 串 → parse_qs 形态（与 app 层传参一致）。"""
    import urllib.parse
    return urllib.parse.parse_qs(raw)


def test_payload_rejects_bad_date_and_page():
    runtime = SimpleNamespace(history=object(), cfg=SimpleNamespace(session_id=SESSION))
    with pytest.raises(console_core.ApiError) as exc:
        console_control.history_day_payload(runtime, _params(""))
    assert exc.value.status == 400
    with pytest.raises(console_core.ApiError) as exc:
        console_control.history_day_payload(runtime, _params("date=2026/10/10"))
    assert exc.value.status == 400
    with pytest.raises(console_core.ApiError) as exc:
        console_control.history_day_payload(runtime, _params("date=2026-10-10&page=0"))
    assert exc.value.status == 400
    with pytest.raises(console_core.ApiError) as exc:
        console_control.history_day_payload(runtime, _params("date=2026-10-10&page=abc"))
    assert exc.value.status == 400


def test_payload_degrades_503_without_history_or_session():
    with pytest.raises(console_core.ApiError) as exc:
        console_control.history_day_payload(
            SimpleNamespace(history=None, cfg=SimpleNamespace(session_id=SESSION)),
            _params("date=2026-10-10"))
    assert exc.value.status == 503
    with pytest.raises(console_core.ApiError) as exc:
        console_control.history_day_payload(
            SimpleNamespace(history=object(), cfg=SimpleNamespace(session_id="")),
            _params("date=2026-10-10"))
    assert exc.value.status == 503


# ---------------------------------------------------------------------------
# HTTP 层：三重门继承 + 分页契约 + 只读
# ---------------------------------------------------------------------------

@pytest.fixture
def replay_server(tmp_path, store):
    """动态端口控制台：runtime 带 history + 会话占位（登录后可回放）。"""
    cfg = console_core.AppConfig.for_root(tmp_path, port=0)
    auth_mgr = console_auth.AuthManager(cfg.console_path)
    auth_mgr.load()
    runtime = SimpleNamespace(history=store, cfg=SimpleNamespace(session_id=SESSION))
    httpd = console_app.make_server(
        "127.0.0.1", 0, cfg, auth=auth_mgr, runtime=runtime,
        status_provider=lambda: {"services": [], "generated_at": 1},
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, httpd.server_address[1], auth_mgr, store
    httpd.shutdown()
    httpd.server_close()


def _request(port: int, method: str, path: str, *, headers: "dict | None" = None) -> "tuple[int, dict]":
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


def _login_cookie(port: int, password: str) -> str:
    """真实走 /api/auth/login（POST JSON 体 + CSRF 头）拿会话 Cookie。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        body = json.dumps({"password": password}).encode("utf-8")
        conn.request("POST", "/api/auth/login", body=body,
                     headers={"Content-Type": "application/json",
                              "X-Requested-With": "XMLHttpRequest"})
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
        cookie = resp.getheader("Set-Cookie", "").split(";")[0]
    finally:
        conn.close()
    assert cookie
    return cookie


def test_http_history_gates_and_contract(replay_server):
    _httpd, port, auth_mgr, store = replay_server
    _seed_two_days(store)
    headers = {"Cookie": _login_cookie(port, auth_mgr.load()["password"]),
               "X-Requested-With": "XMLHttpRequest"}

    # 门 1：未登录 → 401（三重门随 /api/* 继承，回放面无旁路）
    status, _ = _request(port, "GET", "/api/history/list?date=2026-10-10")
    assert status == 401

    # date/page 校验：400（登录后才校验参数，与记忆检索面同序）
    for query in ("", "date=2026/10/10", "date=2026-13-99", "date=2026-10-10&page=0",
                  "date=2026-10-10&page=-1", "date=2026-10-10&page=x"):
        status, _ = _request(port, "GET", f"/api/history/list?{query}", headers=headers)
        assert status == 400, query

    # 正常回放：契约键面 + 内容 + 角色中文名
    status, payload = _request(port, "GET", "/api/history/list?date=2026-10-10", headers=headers)
    assert status == 200
    assert payload["ok"] is True
    assert {"items", "total", "page", "page_size", "pages"} <= set(payload)
    assert payload["total"] == 3 and payload["page"] == 1
    assert payload["page_size"] == console_control.HISTORY_DAY_PAGE_SIZE == 200
    assert payload["pages"] == 1
    assert [it["role_zh"] for it in payload["items"]] == ["我", "她", "我"]
    assert payload["items"][0]["content"] == "早上好"
    assert isinstance(payload["items"][0]["time"], int)

    # 空日子：200 + 空集（不是错误）
    status, payload = _request(port, "GET", "/api/history/list?date=2026-10-12", headers=headers)
    assert status == 200 and payload["items"] == [] and payload["total"] == 0


def test_http_history_readonly_and_paging(replay_server):
    _httpd, port, auth_mgr, store = replay_server
    # 播种 250 条：单页 200 上限 + 翻页
    for i in range(250):
        store.append(SESSION, "user", f"占位消息 {i}",
                     created_at=_ms(DAY_A, 8) + i * 1000, mirror=False)
    before = store.count(SESSION)
    headers = {"Cookie": _login_cookie(port, auth_mgr.load()["password"]),
               "X-Requested-With": "XMLHttpRequest"}

    status, page1 = _request(port, "GET", "/api/history/list?date=2026-10-10&page=1", headers=headers)
    assert status == 200
    assert (page1["total"], page1["pages"]) == (250, 2)
    assert len(page1["items"]) == 200                    # ≤200 条/页（契约上限）
    status, page2 = _request(port, "GET", "/api/history/list?date=2026-10-10&page=2", headers=headers)
    assert status == 200
    assert len(page2["items"]) == 50
    assert page2["items"][0]["content"] == "占位消息 200"
    # 升序且两页无缝拼接（无重无漏）
    all_items = page1["items"] + page2["items"]
    times = [it["time"] for it in all_items]
    assert times == sorted(times) and len(set(times)) == 250
    # 只读：两次回放后行数不变
    assert store.count(SESSION) == before == 250
