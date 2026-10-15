# -*- coding: utf-8 -*-
"""控制台「我们的回忆」页（/api/memories）契约测试（R11）。

覆盖点：
- core.memories_payload：认识锚点（日期 + 认识天数；soul/anniversary
  relation_days_line 同口径——认识当天即第 1 天；锚点在未来一律 None 宁缺勿猜）、
  已发里程碑时间线（days:/yearly:/birthday:*:/holiday: 新键与旧档纯数字键都
  翻成中文标签，按发送时刻升序）、history 库消息计数（缺失/失败 → None）、
  first_voice.json（文件存在才显示；缺失/损坏/形态不对一律 None）、
  日记日期倒序 ≤30 条；
- /api/memories 路由：GET 走三重门（未登录 401），登录后 200 返回 payload，
  只读端点（POST → 404）；
- 前端接线（真实 index.html 静态断言）：导航项 / page-memories / JS 消费点 /
  PAGES 注册，新页文案零品牌字样。

数据全部 tmp_path 合成（anniversary.json / first_voice.json / 日记目录），
payload 测试注入 now 固定时钟，路由测试锚点相对 time.time() 播种；全程不发
真实请求、不碰 local/ 真数据；人设/会话一律占位值（敏感值绝不进测试）。
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from virtuallover.net import smart_urlopen  # noqa: E402  测试打 127.0.0.1：禁代理直连
from virtuallover.console import app as console_app  # noqa: E402
from virtuallover.console import auth as console_auth  # noqa: E402
from virtuallover.console import control as console_control  # noqa: E402
from virtuallover.console import core as console_core  # noqa: E402
from virtuallover.console import engine as console_engine  # noqa: E402

INDEX_HTML = console_core.CONSOLE_DIR / "static" / "index.html"

# 固定时钟基准（本机时区）：锚点 2026-08-14 20:00，now 2026-10-14 20:00
# → 2026-08-14 是认识第 1 天，2026-10-14 已认识 62 天
ANCHOR_DT = datetime(2026, 8, 14, 20, 0)
NOW_DT = datetime(2026, 10, 14, 20, 0)
ANCHOR_TS = ANCHOR_DT.timestamp()
NOW_TS = NOW_DT.timestamp()

# 新功能文案纪律（与 test_console_v2 同规）：新增页文案零品牌字样
_BANNED = re.compile(r"ark|napcat|gsvi|astrbot", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 合成数据帮手（全部落在 tmp_path，绝不碰 local/）
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_diary(data_dir: Path, dates: "list[str]") -> None:
    diary_dir = data_dir / "vl_diary" / "diary"
    diary_dir.mkdir(parents=True, exist_ok=True)
    for d in dates:
        (diary_dir / f"{d}.md").write_text("今天也想他啦。", encoding="utf-8")


class StubHistory:
    """HistoryStore 鸭类型替身：只有 count(session_id=None) 全表计数一条路。"""

    def __init__(self, total: int = 0, exc: "Exception | None" = None):
        self._total = total
        self._exc = exc
        self.calls: "list[tuple]" = []

    def count(self, session_id=None) -> int:
        self.calls.append((session_id,))
        if self._exc is not None:
            raise self._exc
        return self._total


# ---------------------------------------------------------------------------
# memories_payload：空世界降级
# ---------------------------------------------------------------------------


def test_payload_missing_everything_degrades(tmp_path):
    """anniversary/first_voice/日记全缺 + 无 history：全槽位降级，绝不抛异常。"""
    payload = console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)
    assert payload["ok"] is True
    assert payload["anchor"] is None
    assert payload["timeline"] == []
    assert payload["total_messages"] is None
    assert payload["first_voice"] is None
    assert payload["diary_dates"] == []


def test_payload_tolerates_none_dirs():
    """目录参数为 None（装配形态兜底）：等同空世界。"""
    payload = console_core.memories_payload(None, None, None, now=NOW_TS)
    assert payload["anchor"] is None
    assert payload["diary_dates"] == []
    assert payload["first_voice"] is None


# ---------------------------------------------------------------------------
# memories_payload：认识锚点
# ---------------------------------------------------------------------------


def test_anchor_date_and_days(tmp_path):
    """锚点在位：date 取 anchor_date 字段，days=62（认识当天即第 1 天口径）。"""
    _write_json(tmp_path / "anniversary.json", {
        "anchor_ts": ANCHOR_TS,
        "anchor_date": "2026-08-14",
        "milestones_sent": {},
    })
    payload = console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)
    anchor = payload["anchor"]
    assert anchor is not None
    assert anchor["date"] == "2026-08-14"
    assert anchor["days"] == 62
    assert anchor["ts"] == pytest.approx(ANCHOR_TS)


def test_anchor_date_falls_back_to_ts_when_field_missing(tmp_path):
    """旧档只有 anchor_ts 没有 anchor_date 字段：日期从时间戳现算。"""
    _write_json(tmp_path / "anniversary.json", {"anchor_ts": ANCHOR_TS})
    payload = console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)
    assert payload["anchor"]["date"] == ANCHOR_DT.strftime("%Y-%m-%d")
    assert payload["anchor"]["days"] == 62


def test_anchor_in_future_is_none(tmp_path):
    """锚点在未来（时钟回拨/脏数据）：宁缺勿猜，anchor=None。"""
    _write_json(tmp_path / "anniversary.json", {"anchor_ts": NOW_TS + 86400})
    payload = console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)
    assert payload["anchor"] is None


def test_anchor_bad_value_is_none(tmp_path):
    """anchor_ts 非 dict 形态/负数/字符串：一律 None，不抛异常。"""
    for bad in ({"anchor_ts": "not-a-number"}, {"anchor_ts": -5}, {"milestones_sent": {}}):
        _write_json(tmp_path / "anniversary.json", bad)
        payload = console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)
        assert payload["anchor"] is None


# ---------------------------------------------------------------------------
# memories_payload：里程碑时间线
# ---------------------------------------------------------------------------


def test_timeline_labels_and_order(tmp_path):
    """新键 + 旧档纯数字键都翻成中文标签；按发送时刻升序（叙事顺序）。"""
    ts = ANCHOR_TS
    _write_json(tmp_path / "anniversary.json", {
        "anchor_ts": ANCHOR_TS,
        "milestones_sent": {
            "7": ts + 100,                 # 旧档纯数字键（mark_sent 写法）
            "days:30": ts + 200,
            "yearly:365": ts + 300,
            "birthday:user:2026": ts + 400,
            "birthday:her:2027": ts + 500,
            "holiday:2027-02-14": ts + 600,
            "broken": "not-a-number",      # 脏值：ts 沉底 0，不炸整页
        },
    })
    items = console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)["timeline"]
    by_key = {it["key"]: it for it in items}
    assert by_key["7"]["label"] == "认识第 7 天"
    assert by_key["days:30"]["label"] == "认识第 30 天"
    assert by_key["yearly:365"]["label"] == "认识 1 周年"
    assert by_key["birthday:user:2026"]["label"] == "2026 年他的生日"
    assert by_key["birthday:her:2027"]["label"] == "2027 年她的生日"
    assert by_key["holiday:2027-02-14"]["label"] == "情人节纪念日"
    assert by_key["broken"]["ts"] == 0.0
    assert by_key["broken"]["label"] == "broken"  # 未知键原样显示，不编文案
    # 升序：broken(0) 沉底，其余按 ts
    order = [it["key"] for it in items]
    assert order == ["broken", "7", "days:30", "yearly:365",
                     "birthday:user:2026", "birthday:her:2027",
                     "holiday:2027-02-14"]
    assert by_key["7"]["date"] == datetime.fromtimestamp(ts + 100).strftime("%Y-%m-%d")


def test_timeline_unknown_holiday_and_bad_yearly(tmp_path):
    """holiday 日期不在 HOLIDAYS → 回退日期串；yearly 尾非数字 → 原样键名。"""
    _write_json(tmp_path / "anniversary.json", {
        "milestones_sent": {"holiday:2099-05-01": ANCHOR_TS, "yearly:x": ANCHOR_TS},
    })
    items = console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)["timeline"]
    by_key = {it["key"]: it for it in items}
    assert by_key["holiday:2099-05-01"]["label"] == "2099-05-01"
    assert by_key["yearly:x"]["label"] == "yearly:x"


def test_timeline_bad_shape_is_empty(tmp_path):
    """milestones_sent 缺失/非 dict（旧档/损坏）：时间线空数组。"""
    for state in ({}, {"milestones_sent": "nope"}, {"milestones_sent": [1, 2]}):
        _write_json(tmp_path / "anniversary.json", state)
        payload = console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)
        assert payload["timeline"] == []


# ---------------------------------------------------------------------------
# memories_payload：首次语音 / 消息计数 / 日记足迹
# ---------------------------------------------------------------------------


def test_first_voice_present(tmp_path):
    """first_voice.json 存在：ts→date，text 透传（前端悬停展示第一句话）。"""
    voice_ts = NOW_TS - 3600
    _write_json(tmp_path / "first_voice.json", {"ts": voice_ts, "text": "喂？听得到吗"})
    payload = console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)
    voice = payload["first_voice"]
    assert voice is not None
    assert voice["text"] == "喂？听得到吗"
    assert voice["date"] == datetime.fromtimestamp(voice_ts).strftime("%Y-%m-%d")


def test_first_voice_missing_or_bad_is_none(tmp_path):
    """文件缺失/损坏 JSON/形态不对/ts 非正：一律 None（文件存在才显示契约）。"""
    # 缺失
    assert console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)["first_voice"] is None
    # 损坏
    (tmp_path / "first_voice.json").write_text("{oops", encoding="utf-8")
    assert console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)["first_voice"] is None
    # 形态不对：ts 是字符串 / 顶层非 dict / ts 非正
    for bad in ({"ts": "x"}, [1, 2], {"ts": 0}, {"text": "没有 ts"}):
        _write_json(tmp_path / "first_voice.json", bad)
        assert console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)["first_voice"] is None


def test_total_messages_from_history(tmp_path):
    """history.count(None) 全表计数；调用失败 → None（前端显示 —）。"""
    history = StubHistory(total=1234)
    payload = console_core.memories_payload(tmp_path, tmp_path, history, now=NOW_TS)
    assert payload["total_messages"] == 1234
    assert history.calls == [(None,)]  # 全表计数（不分会话）
    payload = console_core.memories_payload(
        tmp_path, tmp_path, StubHistory(exc=RuntimeError("db locked")), now=NOW_TS)
    assert payload["total_messages"] is None


def test_diary_dates_desc_and_capped_at_30(tmp_path):
    """日记日期倒序、最多 30 条；非 YYYY-MM-DD.md 一律不收（list_diary 语义）。"""
    dates = [f"2026-09-{d:02d}" for d in range(1, 32)] + ["2026-10-01", "2026-10-02"]
    _write_diary(tmp_path, dates)
    (tmp_path / "vl_diary" / "diary" / "not-a-date.md").write_text("杂质", encoding="utf-8")
    payload = console_core.memories_payload(tmp_path, tmp_path, None, now=NOW_TS)
    got = payload["diary_dates"]
    assert len(got) == console_core.MEMORIES_DIARY_MAX == 30
    assert got == sorted(got, reverse=True)
    assert got[0] == "2026-10-02"
    assert "not-a-date" not in got


# ---------------------------------------------------------------------------
# /api/memories 路由（tmp HTTP 服务器，三重门真打）
# ---------------------------------------------------------------------------


class _MemoriesConsoleStub:
    """最小控制台桩：make_server 全依赖注入，静态页/napcat 配置均为合成文件。"""

    def __init__(self, tmp_path: Path, history=None):
        local = tmp_path / "local"
        static_dir = tmp_path / "static"
        static_dir.mkdir(parents=True)
        (static_dir / "index.html").write_text(
            "<!doctype html><html><body>面板 ok</body></html>", encoding="utf-8")
        napcat_config = local / "napcat_config" / "webui.json"
        napcat_config.parent.mkdir(parents=True, exist_ok=True)
        napcat_config.write_text(json.dumps({"token": "stub-token"}), encoding="utf-8")
        self.cfg = console_core.AppConfig(
            port=0,
            data_dir=local / "shared_plugin_data",
            local_dir=local,
            napcat_config=napcat_config,
            console_path=local / "console.json",
            backup_dir=tmp_path / "backup",
            static_dir=static_dir,
            vl_toml_path=local / "config" / "vl.toml",
            napcat_config_dir=local / "napcat_config",
            vl_data_dir=local / "vl_data",
            fetch_fn=None,
        )
        self.auth = console_auth.AuthManager(self.cfg.console_path)
        self.auth.load()
        self.password = self.auth.load()["password"]
        self.httpd = console_app.make_server(
            "127.0.0.1", 0, self.cfg,
            auth=self.auth,
            engine=console_engine.EngineProxy(napcat_config_path=napcat_config),
            control=console_control.ControlService(
                self.cfg.local_dir, self.cfg.backup_dir, tmp_path,
                runner=lambda *a, **k: None),
            status_provider=lambda: {"services": [], "generated_at": 0},
            runtime=SimpleNamespace(history=history),
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.base = f"http://{host}:{port}"
        self.cookie: "str | None" = None

    def request(self, path, method="GET", body=None, headers=None, *,
                use_cookie=True, csrf=True):
        h = dict(headers or {})
        if use_cookie and self.cookie:
            h["Cookie"] = self.cookie
        if csrf:
            h["X-Requested-With"] = "XMLHttpRequest"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, headers=h, method=method)
        try:
            with smart_urlopen(request, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def login(self):
        status, headers, _ = self.request(
            "/api/auth/login", method="POST",
            body={"password": self.password}, use_cookie=False)
        assert status == 200, (status, headers)
        self.cookie = headers.get("Set-Cookie").split(";")[0]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def memories_console(tmp_path):
    stub = _MemoriesConsoleStub(tmp_path)
    yield stub
    stub.close()


def test_memories_route_requires_login(memories_console):
    """三重门随 /api/* 继承：无会话 401，绝不泄露回忆数据。"""
    status, _, body = memories_console.request("/api/memories", use_cookie=False)
    assert status == 401
    assert "error" in json.loads(body.decode("utf-8"))


def test_memories_route_serves_payload(tmp_path, memories_console):
    """登录后 GET → 200：聚合锚点/时间线/计数/首次语音/日记足迹五个槽位。"""
    vl_data = memories_console.cfg.vl_data_dir
    anchor_ts = time.time() - 6 * 86400  # 6 天前 → 今天是认识第 7 天
    _write_json(vl_data / "anniversary.json", {
        "anchor_ts": anchor_ts,
        "milestones_sent": {"7": anchor_ts + 100},
    })
    _write_json(vl_data / "first_voice.json", {"ts": time.time(), "text": "占位第一句话"})
    _write_diary(memories_console.cfg.data_dir, ["2026-10-01", "2026-10-02"])
    memories_console.login()
    status, _, body = memories_console.request("/api/memories")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["ok"] is True
    assert payload["anchor"]["days"] == 7
    assert [it["label"] for it in payload["timeline"]] == ["认识第 7 天"]
    assert payload["first_voice"] is not None
    assert payload["diary_dates"] == ["2026-10-02", "2026-10-01"]


def test_memories_route_post_is_404(memories_console):
    """只读端点：POST（即使带 CSRF 头与会话）→ 404。"""
    memories_console.login()
    status, _, _ = memories_console.request(
        "/api/memories", method="POST", body={})
    assert status == 404


def test_memories_route_empty_world_still_200(memories_console):
    """什么数据都没种（history=None 槽位缺失）：200 + 全空槽位，绝不 500。"""
    memories_console.login()
    status, _, body = memories_console.request("/api/memories")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["anchor"] is None
    assert payload["timeline"] == []
    assert payload["total_messages"] is None
    assert payload["first_voice"] is None
    assert payload["diary_dates"] == []


# ---------------------------------------------------------------------------
# 前端接线（真实 index.html 静态断言，与路由/_serve_index 同源文件）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def html() -> str:
    assert INDEX_HTML.is_file(), f"控制台首页缺失：{INDEX_HTML}"
    return INDEX_HTML.read_text(encoding="utf-8")


def _section(source: str, marker: str) -> str:
    """截取 marker 起到其后第一个 </section> 的片段（卡级块提取）。"""
    start = source.index(marker)
    end = source.index("</section>", start)
    return source[start:end]


def test_nav_and_page_registered(html):
    """导航项在 memory 与 control 之间；页面 section + PAGES 注册齐备。"""
    assert (
        html.index('data-page="memory"')
        < html.index('data-page="memories"')
        < html.index('data-page="control"')
    )
    assert 'id="page-memories"' in html
    assert "memories: { title: '我们的回忆', onShow: function(){ memoriesEnter(); } }" in html
    assert "var EP_MEMORIES = '/api/memories'" in html


def test_memories_page_elements(html):
    """前端消费点：锚点/天数/消息数/首次语音/日记 chips/时间线/空态/错误位。"""
    for marker in ('id="card-memories"', 'id="memories-anchor"',
                   'id="memories-days"', 'id="memories-total"',
                   'id="memories-first-voice"', 'id="memories-diary-count"',
                   'id="memories-timeline"', 'id="memories-diary-chips"',
                   'id="memories-empty"', 'id="memories-diary-empty"',
                   'id="err-memories"'):
        assert marker in html, marker
    for fn in ("function memoriesEnter(", "function renderMemories("):
        assert fn in html, fn


def test_memories_page_copy_brand_clean(html):
    """新页文案纪律：零 ark|napcat|gsvi|astrbot 字样。"""
    block = _section(html, 'id="page-memories"')
    assert not _BANNED.search(block), _BANNED.search(block).group(0)


def test_memories_backend_wired_in_app_source():
    """后端接线：app.py 路由进 core.memories_payload（inspect 读真实模块源码）。"""
    import inspect

    source = inspect.getsource(console_app)
    assert '"/api/memories"' in source
    assert "memories_payload" in source
