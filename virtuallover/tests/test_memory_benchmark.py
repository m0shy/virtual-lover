# -*- coding: utf-8 -*-
"""memory/benchmark.py 纯逻辑单元测试（O11① 迁移自 plugins/vl_eval/tests/test_benchmark.py）。

port-of-own：断言原样搬入（22 项），仅 import 从插件目录裸模块
``import benchmark`` 改为新家 ``virtuallover.memory.benchmark``（conftest 的
astrbot/pymilvus stub 依赖随 plugins/ 整删一并退役，本文件零 stub 需求——
benchmark 是纯逻辑模块）。

运行：python -m pytest virtuallover/tests -q -n auto（统一门禁入口，O11④）
"""

from __future__ import annotations

import asyncio

from virtuallover.memory import benchmark
from virtuallover.memory.benchmark import (
    BenchmarkReport,
    FactCase,
    MNEMO_META_PREFIX,
    MNEMO_META_SUFFIX,
    build_fact_cases,
    is_hit,
    recall_at_k,
    render_markdown_report,
    run_memory_benchmark,
    strip_memory_meta,
    summarize,
)

# ---------------------------------------------------------------------------
# 模板池与事实生成
# ---------------------------------------------------------------------------


def test_template_pool_has_20_unique_patterns():
    """模板池必须恰好 20 条，且事实/提问句型互不重复。"""
    assert len(benchmark.FACT_TEMPLATES) == 20
    facts = {t["fact"] for t in benchmark.FACT_TEMPLATES}
    questions = {t["question"] for t in benchmark.FACT_TEMPLATES}
    assert len(facts) == 20, "事实句型存在重复"
    assert len(questions) == 20, "提问句型存在重复"
    for t in benchmark.FACT_TEMPLATES:
        assert "{marker}" in t["fact"], "事实句型必须含编号占位"
        assert "{marker}" in t["question"], "提问句型必须含编号占位"
        assert t["slots"], "每个模板至少要有一个槽位池"
        for pool in t["slots"].values():
            assert len(pool) >= 4, "槽位候选值池过小，缺乏多样性"


def test_build_fact_cases_unique_markers_and_texts():
    """生成结果编号唯一、事实全文唯一，且跨模板循环也不重复。"""
    cases = build_fact_cases(45)  # 超过模板数，覆盖循环取用场景
    assert len(cases) == 45
    markers = [c.marker for c in cases]
    texts = [c.fact_text for c in cases]
    assert len(set(markers)) == 45
    assert len(set(texts)) == 45
    # 编号连续且格式可核对
    assert cases[0].marker == "【评测#001】"
    assert cases[44].marker == "【评测#045】"
    # 提问变体应含编号标记且以问号收尾
    for c in cases:
        assert c.marker in c.question
        assert c.question.rstrip().endswith("？")
        assert c.core_text in c.fact_text


def test_build_fact_cases_edge_values():
    """n<=0 应返回空列表；n=1 正常生成。"""
    assert build_fact_cases(0) == []
    assert build_fact_cases(-3) == []
    single = build_fact_cases(1)
    assert len(single) == 1
    assert single[0].index == 1


# ---------------------------------------------------------------------------
# 命中判定
# ---------------------------------------------------------------------------


def test_is_hit_by_marker():
    case = build_fact_cases(3)[2]
    assert is_hit(case, f"她说过：{case.fact_text} 这件事我记得。")
    # 记忆内容前后夹杂空白也可命中
    assert is_hit(case, f"\n {case.fact_text}\n")


def test_is_hit_strips_mnemo_meta_suffix():
    """召回内容带 mnemosyne 元数据尾巴时，剥离后仍能按原文兜底命中。"""
    case = build_fact_cases(5)[4]
    marked = case.fact_text.replace(case.marker, "")  # 模拟写入端去掉标记的场景
    stored = f"{marked}\n{MNEMO_META_PREFIX}{{\"source\":\"vl_eval\"}}{MNEMO_META_SUFFIX}"
    assert strip_memory_meta(stored) == marked
    assert is_hit(case, stored)


def test_is_miss_on_unrelated_content():
    case = build_fact_cases(7)[6]
    assert not is_hit(case, "我最喜欢的食物是红烧肉。")
    assert not is_hit(case, "")
    # 其他事实的编号不应造成误命中
    other = build_fact_cases(8)[7]
    assert not is_hit(case, other.fact_text)


# ---------------------------------------------------------------------------
# 召回指标计算
# ---------------------------------------------------------------------------


def _result_for(case: FactCase, recalled: list[tuple[str, float]]):
    return benchmark._evaluate_case(case, recalled, top_k=3)


def test_recall_computation_partial_hits():
    """构造 4 条事实：#1 命中第 1 名、#2 命中第 2 名、#3 未命中、#4 命中第 3 名。"""
    cases = build_fact_cases(4)
    results = [
        _result_for(cases[0], [(cases[0].fact_text, 0.9), ("无关内容", 0.5)]),
        _result_for(cases[1], [("无关内容", 0.8), (cases[1].fact_text, 0.7)]),
        _result_for(cases[2], [("无关内容A", 0.8), ("无关内容B", 0.6)]),
        _result_for(cases[3], [("无关内容", 0.7), ("无关内容", 0.6), (cases[3].fact_text, 0.5)]),
    ]
    assert recall_at_k(results, 1) == 0.25
    assert recall_at_k(results, 3) == 0.75
    report = summarize(results)
    assert report.total_cases == 4
    assert report.recall_at_1 == 0.25
    assert report.recall_at_3 == 0.75
    # Top1 相似度取第一名（无论是否命中）
    assert report.avg_top1_similarity is not None
    assert abs(report.avg_top1_similarity - (0.9 + 0.8 + 0.8 + 0.7) / 4) < 1e-9


def test_recall_empty_results_and_empty_cases():
    """检索无返回时该条计为未命中；空结果集指标为 0。"""
    case = build_fact_cases(1)[0]
    r = _result_for(case, [])
    assert r.hit_rank is None
    assert r.top1_similarity is None
    report = summarize([r])
    assert report.recall_at_1 == 0.0
    assert report.avg_top1_similarity is None
    assert recall_at_k([], 3) == 0.0


def test_top_k_truncates_candidates():
    """命中出现在第 4 名时，按 top_k=3 截断后不计入 recall@3。"""
    case = build_fact_cases(2)[1]
    recalled = [(f"干扰{i}", 0.9 - i * 0.01) for i in range(3)] + [(case.fact_text, 0.4)]
    r = _result_for(case, recalled)
    assert r.hit_rank is None
    assert len(r.recalled) == 3


# ---------------------------------------------------------------------------
# run_memory_benchmark：可注入检索与延时
# ---------------------------------------------------------------------------


def test_run_memory_benchmark_with_fake_retriever_and_sleep():
    """假检索函数 + 假 sleep 驱动完整评测流程。"""
    cases = build_fact_cases(6)
    calls: list[str] = []
    sleeps: list[float] = []

    async def fake_retriever(question: str) -> list[tuple[str, float]]:
        calls.append(question)
        for c in cases:
            if c.question == question:
                # 偶数条命中第 1 名，奇数条命中第 2 名
                head = [] if c.index % 2 == 0 else [("干扰项", 0.9)]
                return head + [(c.fact_text, 0.8)]
        return []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    progress_log: list[tuple[int, int]] = []

    def progress(done: int, total: int, _result) -> None:
        progress_log.append((done, total))

    report = asyncio.run(
        run_memory_benchmark(
            cases,
            fake_retriever,
            settle_seconds=5.0,
            top_k=3,
            sleep=fake_sleep,
            progress=progress,
        )
    )
    assert sleeps == [5.0], "应在检索前等待注入的 settle 时长"
    assert calls == [c.question for c in cases], "每条事实的提问变体都应被检索一次"
    assert progress_log == [(i, 6) for i in range(1, 7)]
    # 偶数（第1名）3 条 + 奇数（第2名）3 条
    assert report.recall_at_1 == 0.5
    assert report.recall_at_3 == 1.0
    assert report.write_failures == []


def test_run_memory_benchmark_without_settle():
    """settle_seconds=0 时不触发 sleep（sleep 未注入也不依赖事件循环真实延时）。"""
    case = build_fact_cases(1)[0]

    async def fake_retriever(_q: str) -> list[tuple[str, float]]:
        return [(case.fact_text, 0.99)]

    report = asyncio.run(run_memory_benchmark([case], fake_retriever))
    assert report.recall_at_1 == 1.0
    assert report.avg_top1_similarity == 0.99


def test_run_memory_benchmark_settle_zero_never_sleeps():
    """settle_seconds=0 时全流程不等额外秒：注入的假 sleep 一次都不该被调用。

    0 是合法配置（等索引生效的等待可显式关闭），不得被默认值兜底吞掉。
    """
    cases = build_fact_cases(3)
    sleeps: list[float] = []

    async def fake_retriever(_q: str) -> list[tuple[str, float]]:
        return []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    report = asyncio.run(
        run_memory_benchmark(cases, fake_retriever, settle_seconds=0, sleep=fake_sleep)
    )
    assert sleeps == [], "settle_seconds=0 时不得触发任何等待"
    assert report.total_cases == 3


def test_run_memory_benchmark_positive_settle_sleeps_exactly_once():
    """非 0 的 settle_seconds 仍应在检索前等待一次（对照用例）。"""
    case = build_fact_cases(1)[0]
    sleeps: list[float] = []

    async def fake_retriever(_q: str) -> list[tuple[str, float]]:
        return [(case.fact_text, 0.9)]

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    asyncio.run(run_memory_benchmark([case], fake_retriever, settle_seconds=4.5, sleep=fake_sleep))
    assert sleeps == [4.5]


# ---------------------------------------------------------------------------
# 逐条容错：单条检索异常不中断整轮评测
# ---------------------------------------------------------------------------


def test_run_memory_benchmark_survives_per_case_retriever_error():
    """注入第 3 条抛错的假检索器：报告仍生成，3 号标错，其余 4 条正常统计。"""
    cases = build_fact_cases(5)

    async def flaky_retriever(question: str) -> list[tuple[str, float]]:
        for c in cases:
            if c.question == question:
                if c.index == 3:
                    raise RuntimeError("embedding 临时不可用")
                return [(c.fact_text, 0.88)]
        return []

    progress_log: list[tuple[int, int]] = []

    def progress(done: int, total: int, _result) -> None:
        progress_log.append((done, total))

    report = asyncio.run(
        run_memory_benchmark(
            cases, flaky_retriever, settle_seconds=0, progress=progress
        )
    )
    assert report.total_cases == 5, "抛错条目仍应计入总条数"
    assert progress_log == [(i, 5) for i in range(1, 6)], "全部条目都应走完进度回调"
    third = report.case_results[2]
    assert third.hit_rank is None, "抛错条目应记为未命中"
    assert third.error is not None and "embedding 临时不可用" in third.error
    assert third.top1_similarity is None and third.best_similarity is None
    others = [r for i, r in enumerate(report.case_results) if i != 2]
    assert all(r.error is None and r.hit_rank == 1 for r in others)
    assert abs(report.recall_at_1 - 0.8) < 1e-9
    assert abs(report.recall_at_3 - 0.8) < 1e-9
    # 报告渲染：明细表标注检索异常，未命中明细附错误信息
    text = render_markdown_report(report)
    assert "检索异常" in text
    assert "embedding 临时不可用" in text


# ---------------------------------------------------------------------------
# 评测数据自动清理（benchmark 层纯逻辑）
# ---------------------------------------------------------------------------


def test_cleanup_session_data_calls_injected_deleter_once():
    """传入假删除函数：清理时恰好按 session_id 调用一次并返回成功。"""
    calls: list[str] = []

    async def fake_delete(session_id: str) -> None:
        calls.append(session_id)

    ok, err = asyncio.run(benchmark.cleanup_session_data("vl_eval_bench_demo", fake_delete))
    assert ok is True
    assert err is None
    assert calls == ["vl_eval_bench_demo"]


def test_cleanup_session_data_swallows_deleter_failure():
    """假删除函数抛异常：错误被捕获转为 (False, 原因)，不向调用方传播。"""
    async def bad_delete(session_id: str) -> None:
        raise RuntimeError("connection refused")

    ok, err = asyncio.run(benchmark.cleanup_session_data("vl_eval_bench_demo", bad_delete))
    assert ok is False
    assert err is not None and "connection refused" in err


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


def _sample_report() -> BenchmarkReport:
    cases = build_fact_cases(4)
    results = [
        _result_for(cases[0], [(cases[0].fact_text, 0.91)]),
        _result_for(cases[1], [("干扰", 0.8), (cases[1].fact_text, 0.72)]),
        _result_for(cases[2], [("干扰A", 0.6), ("干扰B", 0.5)]),
        _result_for(cases[3], []),
    ]
    return summarize(results, write_failures=["【评测#004】写入失败：embedding 不可用"])


def test_render_markdown_report_contains_key_sections():
    report = _sample_report()
    text = render_markdown_report(
        report,
        generated_at="2026-10-10 12:00:00",
        collection="default",
        session_id="vl_eval_bench_20261010_120000",
        write_mode="mnemosyne 内部 API",
        archive_note="完整报告已存档：reports/eval_demo.md",
    )
    # 汇总区
    assert "# 📊 记忆召回评测报告" in text
    assert "事实条数：4" in text
    assert "**recall@1**：25.0%（1/4）" in text
    assert "**recall@3**：50.0%（2/4）" in text
    assert "Top1 平均相似度：0.7700" in text  # (0.91+0.8+0.6)/3，空返回条目不参与平均
    # 明细表与未命中区
    assert "| 编号 | 事实 | 命中排名 | Top1 相似度 |" in text
    assert "未命中明细（2 条）" in text
    assert "【评测#003】" in text
    assert "【评测#004】" in text
    assert "写入失败明细（1 条）" in text
    assert "embedding 不可用" in text
    # 注入的元信息
    assert "2026-10-10 12:00:00" in text
    assert "`default`" in text
    assert "vl_eval_bench_20261010_120000" in text
    assert "mnemosyne 内部 API" in text
    assert "reports/eval_demo.md" in text


def test_render_markdown_report_avg_similarity_math():
    """Top1 平均相似度只统计有返回的条目。"""
    cases = build_fact_cases(2)
    results = [
        _result_for(cases[0], [(cases[0].fact_text, 0.8)]),
        _result_for(cases[1], []),
    ]
    report = summarize(results)
    assert report.avg_top1_similarity == 0.8
    text = render_markdown_report(report)
    assert "Top1 平均相似度：0.8000" in text


def test_render_markdown_report_caps_detail_rows():
    """max_detail_rows 限制明细行数并提示省略数量。"""
    cases = build_fact_cases(10)
    results = [_result_for(c, [(c.fact_text, 0.9)]) for c in cases]
    report = summarize(results)
    text = render_markdown_report(report, max_detail_rows=3)
    assert "命中明细（前 3 条）" in text
    assert "其余 7 条明细略" in text
    # 明细表只出现前 3 个编号
    assert "【评测#003】" in text
    assert "| #004 |" not in text


def test_render_markdown_report_deterministic():
    """同样的输入渲染结果完全一致（不含时钟等隐式状态）。"""
    report = _sample_report()
    kwargs = {"generated_at": "2026-10-10 12:00:00", "collection": "default"}
    assert render_markdown_report(report, **kwargs) == render_markdown_report(report, **kwargs)


def test_render_markdown_report_cleanup_note_at_tail():
    """cleanup_note 注明在报告尾；不传时不出现清理字样。"""
    report = _sample_report()
    text = render_markdown_report(report, cleanup_note="评测数据已自动清理")
    assert "- 评测数据已自动清理" in text
    assert text.rstrip().endswith("评测数据已自动清理")
    text_without = render_markdown_report(report)
    assert "已自动清理" not in text_without


def test_render_markdown_report_no_misses_section_when_all_hit():
    cases = build_fact_cases(2)
    results = [_result_for(c, [(c.fact_text, 0.9)]) for c in cases]
    text = render_markdown_report(summarize(results))
    assert "**recall@1**：100.0%（2/2）" in text
    assert "未命中明细" not in text
    assert "写入失败明细" not in text
