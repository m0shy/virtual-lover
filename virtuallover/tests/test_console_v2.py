# -*- coding: utf-8 -*-
"""控制台页面重组契约测试（R9 功能融合：7→6 页 / 常驻卡片 / brain_stop / 回放卡）。

覆盖点（FUSION_SPEC §5-R9-1/2/3 控制台侧）：
- 页面重组：侧栏 6 项（总览/QQ 登录/引擎与人格/记忆与日记/服务管理/语音），
  page-persona 独立页删除、人格卡迁入引擎页（同一 ConfigPageService 后端，
  SECTIONS 三节不变）；
- 日记/评测两卡常驻「记忆与日记」页：placeListCards DOM 搬家 hack 清零，
  总览页不再引用日记/评测；
- 引擎页补「只停本地引擎」（brain_stop）ghost 按钮：前端按钮 + JS 动作体 +
  后端白名单（brain_stop 本就在 ALLOWED_ACTIONS）；
- 对话回放卡：/api/history/list 路由接线 + 前端 timeline 元素齐备；
- 新功能文案纪律：回放卡与时间线新文案零 ark|napcat|gsvi|astrbot 字样
  （存量文案的中性化归 R6 轮，不在本检查范围）。

静态结构断言读的是控制台真实 index.html（路由/_serve_index 同源文件）；
后端接线用 inspect 读真实模块源码，不造副本。

R11 增量：第 9 页「我们的回忆」（memories）插入 memory 之后——
EXPECTED_PAGES 扩一位、侧栏顺序断言改名 test_sidenav_page_order；
该页的 payload/路由/前端接线断言在 test_console_memories.py。
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from virtuallover.console import app as console_app  # noqa: E402
from virtuallover.console import config_page as console_config_page  # noqa: E402
from virtuallover.console import control as console_control  # noqa: E402
from virtuallover.console import core as console_core  # noqa: E402

INDEX_HTML = console_core.CONSOLE_DIR / "static" / "index.html"

EXPECTED_PAGES = ("overview", "chat", "qq", "engine", "memory", "memories",
                  "control", "voice")  # R11：新增第 9 页 memories（我们的回忆）；
                                      # 正则不含连字符，qq-ops 不入此列（顺序仍全覆盖）

# 新功能文案纪律（FUSION_SPEC §7）：R9 新增 UI 文案零上游品牌字样
_BANNED = re.compile(r"ark|napcat|gsvi|astrbot", re.IGNORECASE)


@pytest.fixture(scope="module")
def html() -> str:
    assert INDEX_HTML.is_file(), f"控制台首页缺失：{INDEX_HTML}"
    return INDEX_HTML.read_text(encoding="utf-8")


def _section(html: str, marker: str) -> str:
    """截取 marker 起到其后第一个 </section> 的片段（卡级块提取）。"""
    start = html.index(marker)
    end = html.index("</section>", start)
    return html[start:end]


# ---------------------------------------------------------------------------
# 页面重组：7→6 侧栏 / 引擎与人格合并 / 人格页删除
# ---------------------------------------------------------------------------

def test_sidenav_page_order(html):
    """侧栏顺序：R9 的 7 页序保持相对不变，R11 在 memory 之后插入 memories。"""
    pages = re.findall(r'data-page="([a-z]+)"', html)
    assert pages == list(EXPECTED_PAGES)
    assert "persona" not in pages


def test_persona_page_merged_into_engine_page(html):
    # 独立人格页删除；人格卡与引擎卡同住 page-engine
    assert 'id="page-persona"' not in html
    assert 'data-page="persona"' not in html
    engine_start = html.index('id="page-engine"')
    # 人格卡在 page-engine 的开闭区间内（合并后同一 section）
    engine_block_end = html.index('id="page-memory"')
    assert engine_start < html.index('id="card-persona-edit"') < engine_block_end
    assert 'id="card-engine"' in html
    # 三大引擎要素都在引擎卡内（chat/embedding 变卡内小节，id 不变保 JS 契约）
    for marker in ('id="chat-form"', 'id="emb-form"', 'id="brain-chips"'):
        assert engine_start < html.index(marker) < engine_block_end


def test_merged_page_still_uses_config_page_service_backend():
    """前端合并但后端不动：同一 ConfigPageService /api/config/{chat,embedding,persona}。"""
    assert console_config_page.SECTIONS == ("chat", "embedding", "persona", "tts")


def test_pages_js_map_titles(html):
    assert "title: '引擎与人格'" in html
    assert "title: '记忆与日记'" in html
    # persona 不再是独立页面入口
    assert not re.search(r"persona:\s*\{\s*title", html)


# ---------------------------------------------------------------------------
# 常驻卡片：placeListCards 搬家 hack 清零
# ---------------------------------------------------------------------------

def test_place_list_cards_hack_removed(html):
    assert "placeListCards" not in html


def test_diary_eval_cards_reside_in_memory_page(html):
    memory_start = html.index('id="page-memory"')
    control_start = html.index('id="page-control"')
    for card in ('id="card-diary"', 'id="card-eval"', 'id="card-memory-search"',
                 'id="card-history-replay"'):
        pos = html.index(card)
        assert memory_start < pos < control_start, card
    # 总览页不再承载日记/评测（总览段内不得出现两卡）
    overview_block = html[html.index('id="page-overview"'):memory_start]
    assert 'id="card-diary"' not in overview_block
    assert 'id="card-eval"' not in overview_block


# ---------------------------------------------------------------------------
# 只停本地引擎（brain_stop）ghost 按钮
# ---------------------------------------------------------------------------

def test_brain_stop_button_present(html):
    assert 'id="btn-brain-stop"' in html
    assert "只停本地引擎" in html
    # JS 动作体：POST /api/control/action {action:'brain_stop'}
    assert "action: 'brain_stop'" in html
    # 后端白名单本就含 brain_stop（撤 bat 后控制台是唯一等价入口）
    assert "brain_stop" in console_control.ALLOWED_ACTIONS
    assert console_control.BRAIN_SWITCH_ACTIONS["brain_stop"] == "stop"


def test_engine_reload_button_consolidated(html):
    """两卡合一后页级刷新一枚：btn-engine-reload 在，旧单卡刷新按钮清零。"""
    assert 'id="btn-engine-reload"' in html
    assert "btn-chat-reload" not in html
    assert "btn-emb-reload" not in html


# ---------------------------------------------------------------------------
# 对话回放卡：路由接线 + timeline 元素
# ---------------------------------------------------------------------------

def test_history_replay_route_wired():
    source = inspect.getsource(console_app)
    assert '"/api/history/list"' in source
    assert "history_day_payload" in source


def test_history_replay_card_elements(html):
    for marker in ("card-history-replay", "history-date", "btn-history-load",
                   "btn-history-prev", "btn-history-next", "history-timeline",
                   "EP_HISTORY_LIST = '/api/history/list'"):
        assert marker in html, marker


def test_new_feature_copy_brand_clean(html):
    """R9 新增回放卡文案零 ark|napcat|gsvi|astrbot（新功能纪律；存量归 R6 轮）。"""
    block = _section(html, 'id="card-history-replay"')
    assert not _BANNED.search(block), _BANNED.search(block).group(0)


def test_static_index_served_from_same_file(tmp_path):
    """_serve_index 读的正是上面断言的文件（同源，防测试测到副本）。"""
    assert console_core.AppConfig().static_dir == console_core.CONSOLE_DIR / "static"
    assert (console_core.AppConfig().static_dir / "index.html") == INDEX_HTML
