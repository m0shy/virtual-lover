"""记忆召回评测纯逻辑层 —— vl_eval/benchmark.py 原样并入（port-of-own）。

来源与改动声明（docs/provenance.md 口径）：本文件由 plugins/vl_eval/benchmark.py
**逐行并入**，属本项目自有代码（port-of-own），除模块 docstring 外**逻辑零改动**。
/eval memory 命令（SPEC §8.5）经 __main__._build_eval_runner 以本模块 + 进程内
MemoryStore 装配评测执行器（SPEC §7.4 验收：/eval 输出非空 recall 报告）。

本模块是评测的可测试核心，刻意保持与框架解耦：
- 与旧框架（astrbot 一系）零依赖，也不依赖 pymilvus；
- 检索动作以可注入函数（Retriever）传入，延时与进度回调同样可注入；
- 宿主机可直接执行 ``pytest virtuallover/tests -q`` 完成验证。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# mnemosyne 落库时会把内部元数据以标签形式附加在内容尾部，
# 做命中比对前先剥离，避免元数据干扰文本匹配。
MNEMO_META_PREFIX = "<MNEMO_META>"
MNEMO_META_SUFFIX = "</MNEMO_META>"

# 每条合成事实的编号标记格式（写进事实原文与提问变体，便于逐条核对）。
MARKER_TEMPLATE = "【评测#{:03d}】"

# ---------------------------------------------------------------------------
# 合成事实模板池：20 条多样化中文句型。
# fact/question 中的 {marker} 为编号占位，其余为槽位占位；
# slots 为各槽位的候选值池（全部为虚构示例值，循环取用）。
# ---------------------------------------------------------------------------
FACT_TEMPLATES: list[dict] = [
    {
        "fact": "{marker}我叫{name}，我最喜欢的食物是{food}。",
        "question": "{marker}我最喜欢的食物是什么？",
        "slots": {
            "name": ["小柚", "阿澈", "林晚", "苏合", "江离", "白栀", "陆遥", "青豆"],
            "food": ["糖醋排骨", "麻婆豆腐", "阳春面", "锅包肉", "番茄牛腩", "葱油拌面"],
        },
    },
    {
        "fact": "{marker}我的生日在{month}月，每年那时候我都会许愿。",
        "question": "{marker}我的生日是几月？",
        "slots": {"month": ["3", "5", "7", "9", "11", "1"]},
    },
    {
        "fact": "{marker}我养了一只{breed}，它的名字叫{pet_name}。",
        "question": "{marker}我的宠物叫什么名字？",
        "slots": {
            "breed": ["橘猫", "柯基", "布偶猫", "金毛", "垂耳兔", "虎皮鹦鹉"],
            "pet_name": ["团子", "煤球", "布丁", "年糕", "雪碧", "煤煤"],
        },
    },
    {
        "fact": "{marker}我的家乡在{city}，那边下雨天特别多。",
        "question": "{marker}我的家乡是哪里？",
        "slots": {"city": ["杭州", "成都", "长沙", "厦门", "昆明", "青岛"]},
    },
    {
        "fact": "{marker}我的职业是{job}，平时工作节奏还挺快的。",
        "question": "{marker}我是做什么工作的？",
        "slots": {"job": ["插画师", "中学老师", "程序员", "药剂师", "译者", "仓库管理员"]},
    },
    {
        "fact": "{marker}我最近的爱好是{hobby}，一有空就会折腾。",
        "question": "{marker}我最近的爱好是什么？",
        "slots": {"hobby": ["拼装高达模型", "烘焙面包", "钩织玩偶", "收集邮票", "拍立得摄影", "观鸟"]},
    },
    {
        "fact": "{marker}我最喜欢的颜色是{color}，买什么东西都先看这个色。",
        "question": "{marker}我最喜欢的颜色是什么？",
        "slots": {"color": ["雾霾蓝", "薄荷绿", "奶油黄", "酒红色", "藕荷色", "炭灰色"]},
    },
    {
        "fact": "{marker}我心目中最好看的电影是{movie}，看了不下三遍。",
        "question": "{marker}我最喜欢的电影是哪部？",
        "slots": {"movie": ["《海上钢琴师》", "《千与千寻》", "《布达佩斯大饭店》", "《星际穿越》", "《菊次郎的夏天》", "《楚门的世界》"]},
    },
    {
        "fact": "{marker}周末我最常去{place}打发时间。",
        "question": "{marker}我周末最常去哪里？",
        "slots": {"place": ["旧书店", "城市公园的湖边", "小区楼下的咖啡馆", "美术馆", "花鸟市场", "江边步道"]},
    },
    {
        "fact": "{marker}我喝咖啡只喝{coffee}，别的都不太碰。",
        "question": "{marker}我喝咖啡偏好什么口味？",
        "slots": {"coffee": ["燕麦拿铁", "手冲耶加雪菲", "冰美式", "澳白", "焦糖玛奇朵", "椰青冷萃"]},
    },
    {
        "fact": "{marker}我最讨厌吃的蔬菜是{vegetable}，从小吃到大都接受不了。",
        "question": "{marker}我最讨厌吃的蔬菜是什么？",
        "slots": {"vegetable": ["苦瓜", "香菜", "胡萝卜", "芹菜", "秋葵", "洋葱"]},
    },
    {
        "fact": "{marker}我最想去的旅行目的地是{destination}，攒钱也要去一次。",
        "question": "{marker}我最想去哪里旅行？",
        "slots": {"destination": ["冰岛看极光", "京都看红叶", "敦煌看莫高窟", "新西兰自驾", "圣托里尼看海", "喀纳斯看秋色"]},
    },
    {
        "fact": "{marker}我在阳台上养了一盆{plant}，长势还不错。",
        "question": "{marker}我在阳台上养了什么植物？",
        "slots": {"plant": ["龟背竹", "多肉桃蛋", "薄荷", "茉莉", "琴叶榕", "风信子"]},
    },
    {
        "fact": "{marker}一年四季里我最喜欢{season}，怎么都待不够。",
        "question": "{marker}我最喜欢哪个季节？",
        "slots": {"season": ["秋天", "春天", "初夏", "冬天", "深秋", "梅雨过后的初晴"]},
    },
    {
        "fact": "{marker}我的座右铭是「{motto}」，一直拿它提醒自己。",
        "question": "{marker}我的座右铭是什么？",
        "slots": {
            "motto": ["慢慢来，比较快", "先完成，再完美", "种一棵树最好的时间是十年前", "身体和灵魂总要有一个在路上", "不怕慢，只怕站", "认真生活，温柔待人"],
        },
    },
    {
        "fact": "{marker}我习惯在{bedtime}左右入睡，熬夜第二天会很难受。",
        "question": "{marker}我习惯几点睡觉？",
        "slots": {"bedtime": ["23 点", "22 点半", "零点", "23 点半", "22 点", "凌晨一点前"]},
    },
    {
        "fact": "{marker}我会弹{instrument}，水平一般但自娱自乐够了。",
        "question": "{marker}我会弹什么乐器？",
        "slots": {"instrument": ["尤克里里", "钢琴", "口琴", "吉他", "电子琴", "拇指琴"]},
    },
    {
        "fact": "{marker}我每周会去{sport}三次，锻炼完整个人都清爽了。",
        "question": "{marker}我平时做什么运动？",
        "slots": {"sport": ["游泳馆游泳", "江边夜跑", "羽毛球馆打球", "爬附近的小山", "跳操课", "骑行动感单车"]},
    },
    {
        "fact": "{marker}我收藏的第一件宝贝是{collectible}，一直摆在书架上。",
        "question": "{marker}我收藏的第一件东西是什么？",
        "slots": {"collectible": ["一套旧版连环画", "爷爷留下的怀表", "一块雨花石", "限量版手办", "第一张黑胶唱片", "一枚海螺"]},
    },
    {
        "fact": "{marker}我的手机壁纸一直是{wallpaper}，换手机也不换图。",
        "question": "{marker}我的手机壁纸是什么？",
        "slots": {"wallpaper": ["海边日落", "猫的背影", "一张星空照", "手绘的小房子", "雨后的窗户", "极简的绿色渐变"]},
    },
]

assert len(FACT_TEMPLATES) == 20, "模板池必须保持 20 条多样化句型"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FactCase:
    """一条合成事实及其提问变体。"""

    index: int  # 全局编号，从 1 开始
    marker: str  # 唯一编号标记，如 【评测#001】
    fact_text: str  # 完整事实文本（含标记），用于写入记忆
    question: str  # 提问变体（含标记），用于检索
    core_text: str  # 去掉标记后的事实正文，用于兜底匹配


@dataclass
class CaseResult:
    """单条事实的召回结果。"""

    case: FactCase
    recalled: list[tuple[str, float]] = field(default_factory=list)  # [(召回文本, 相似度)]
    hit_rank: int | None = None  # 首个命中的排名（1 起），未命中为 None
    top1_similarity: float | None = None  # 第 1 名结果的相似度
    best_similarity: float | None = None  # 全部返回中最高的相似度
    error: str | None = None  # 检索阶段异常信息；正常完成检索的条目为 None


@dataclass
class BenchmarkReport:
    """一轮评测的汇总结果。"""

    case_results: list[CaseResult] = field(default_factory=list)
    write_failures: list[str] = field(default_factory=list)  # 写入失败的事实编号及原因
    total_cases: int = 0
    recall_at_1: float = 0.0
    recall_at_3: float = 0.0
    avg_top1_similarity: float | None = None  # 无任何返回时为 None


# ---------------------------------------------------------------------------
# 检索函数约定：输入提问文本，返回按"最相似在前"排列的 (记忆内容, 相似度) 列表。
# 相似度统一为"越大越相似"（L2 距离由调用方换算，如 1/(1+d)）。
# ---------------------------------------------------------------------------
Retriever = Callable[[str], Awaitable[Sequence[tuple[str, float]]]]
SleepFn = Callable[[float], Awaitable[None]]
ProgressFn = Callable[[int, int, "CaseResult"], None]
CleanupFn = Callable[[str], Awaitable[Any]]  # 接收 session_id 执行实际删除


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------
def strip_memory_meta(text: str) -> str:
    """剥离 mnemosyne 附加在记忆内容尾部的 <MNEMO_META>...</MNEMO_META> 元数据。"""
    if not isinstance(text, str):
        return ""
    start = text.rfind(MNEMO_META_PREFIX)
    if start < 0:
        return text.strip()
    return text[:start].strip()


def _compact(text: str) -> str:
    """归一化文本用于比对：去掉全部空白字符。"""
    return "".join(text.split())


def build_fact_cases(n: int) -> list[FactCase]:
    """生成 n 条带编号的合成事实与提问变体。

    模板与槽位值均按下标循环取用，过程确定（无随机），便于人工核对：
    第 i 条（1 起）使用第 ``(i-1) % len(FACT_TEMPLATES)`` 号模板。
    编号标记保证事实全文互不重复。
    """
    n = max(int(n), 0)
    cases: list[FactCase] = []
    for i in range(1, n + 1):
        template = FACT_TEMPLATES[(i - 1) % len(FACT_TEMPLATES)]
        marker = MARKER_TEMPLATE.format(i)
        slots = {
            key: values[(i - 1) % len(values)] for key, values in template["slots"].items()
        }
        fact_text = template["fact"].format(marker=marker, **slots)
        question = template["question"].format(marker=marker)
        core_text = fact_text.replace(marker, "").strip()
        cases.append(
            FactCase(
                index=i,
                marker=marker,
                fact_text=fact_text,
                question=question,
                core_text=core_text,
            )
        )
    return cases


def is_hit(case: FactCase, recalled_text: str) -> bool:
    """判断一条召回内容是否命中指定事实。

    主判据：召回文本包含该事实的唯一编号标记；
    兜底判据：剥离元数据并归一化后包含事实正文（应对写入端改写标记的场景）。
    """
    if not isinstance(recalled_text, str) or not recalled_text:
        return False
    if case.marker in recalled_text:
        return True
    cleaned = _compact(strip_memory_meta(recalled_text))
    return bool(cleaned) and _compact(case.core_text) in cleaned


# ---------------------------------------------------------------------------
# 评测执行
# ---------------------------------------------------------------------------
def _evaluate_case(case: FactCase, recalled: Sequence[tuple[str, float]], top_k: int) -> CaseResult:
    """对单条事实的召回列表计算命中排名与相似度指标。"""
    considered = list(recalled)[:top_k]
    result = CaseResult(case=case, recalled=considered)
    if considered:
        result.top1_similarity = considered[0][1]
        result.best_similarity = max(score for _, score in considered)
    for rank, (text, _score) in enumerate(considered, start=1):
        if is_hit(case, text):
            result.hit_rank = rank
            break
    return result


def recall_at_k(case_results: Sequence[CaseResult], k: int) -> float:
    """计算 recall@k：命中排名不超过 k 的事实占比。"""
    if not case_results:
        return 0.0
    hits = sum(1 for r in case_results if r.hit_rank is not None and r.hit_rank <= k)
    return hits / len(case_results)


def summarize(
    case_results: Sequence[CaseResult],
    write_failures: Sequence[str] = (),
) -> BenchmarkReport:
    """把逐条结果汇总成评测报告数据。"""
    similarities = [r.top1_similarity for r in case_results if r.top1_similarity is not None]
    return BenchmarkReport(
        case_results=list(case_results),
        write_failures=list(write_failures),
        total_cases=len(case_results),
        recall_at_1=recall_at_k(case_results, 1),
        recall_at_3=recall_at_k(case_results, 3),
        avg_top1_similarity=(sum(similarities) / len(similarities)) if similarities else None,
    )


async def run_memory_benchmark(
    cases: Sequence[FactCase],
    retriever: Retriever,
    *,
    settle_seconds: float = 0.0,
    top_k: int = 3,
    write_failures: Sequence[str] = (),
    sleep: SleepFn | None = None,
    progress: ProgressFn | None = None,
) -> BenchmarkReport:
    """对一组事实执行召回评测。

    Args:
        cases: 待评测事实列表（通常由 :func:`build_fact_cases` 生成）。
        retriever: 可注入的检索函数，输入提问文本，返回按相似度降序的结果列表。
        settle_seconds: 检索前的等待秒数（等写入的记忆索引生效）。
        top_k: 每条提问最多纳入统计的召回条数（recall@1/@3 只取前 3 名以内）。
        write_failures: 写入阶段收集的失败清单，原样并入报告。
        sleep: 延时实现，可注入假实现避免单测真实等待。
        progress: 进度回调 ``progress(已完成数, 总数, CaseResult)``。
    """
    sleep_fn = sleep or asyncio.sleep
    if settle_seconds > 0:
        await sleep_fn(settle_seconds)

    results: list[CaseResult] = []
    total = len(cases)
    for i, case in enumerate(cases, start=1):
        try:
            recalled = await retriever(case.question)
        except Exception as e:
            # 单条检索失败不中断整轮评测：该条记为未命中并附错误标记，继续其余条目。
            result = CaseResult(case=case, error=f"检索异常：{e}")
        else:
            result = _evaluate_case(case, recalled, top_k)
        results.append(result)
        if progress is not None:
            progress(i, total, result)

    return summarize(results, write_failures)


# ---------------------------------------------------------------------------
# 评测数据清理
# ---------------------------------------------------------------------------
async def cleanup_session_data(session_id: str, deleter: CleanupFn) -> tuple[bool, str | None]:
    """按 session_id 清理本次评测写入的数据（deleter 由调用方注入，便于测试）。

    deleter 接收 session_id 并执行实际删除（如 Milvus ``coll.delete(expr=...)``）；
    其抛出的任何异常都被捕获并转为 ``(False, 失败原因)`` 返回，绝不向上传播，
    保证清理失败不影响评测报告的产出。
    """
    try:
        await deleter(session_id)
        return True, None
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------
def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _fmt_score(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "—"


def render_markdown_report(
    report: BenchmarkReport,
    *,
    generated_at: str | None = None,
    collection: str = "",
    session_id: str = "",
    write_mode: str = "",
    archive_note: str | None = None,
    cleanup_note: str | None = None,
    max_detail_rows: int = 30,
) -> str:
    """把评测报告渲染成中文 Markdown 文本。

    Args:
        report: :func:`run_memory_benchmark` 的汇总结果。
        generated_at: 报告生成时间（由调用方注入，保持本模块确定性）。
        collection: 记忆集合名。
        session_id: 评测专用会话 ID。
        write_mode: 实际采用的写入路径描述。
        archive_note: 完整报告存档位置说明。
        cleanup_note: 报告尾注记（评测数据自动清理结果说明）。
        max_detail_rows: 明细表最多展示的行数（防止消息过长）。
    """
    lines: list[str] = ["# 📊 记忆召回评测报告", ""]
    lines.append(f"- 事实条数：{report.total_cases}")
    if generated_at:
        lines.append(f"- 评测时间：{generated_at}")
    if collection:
        lines.append(f"- 记忆集合：`{collection}`")
    if session_id:
        lines.append(f"- 评测会话：`{session_id}`")
    if write_mode:
        lines.append(f"- 写入方式：{write_mode}")
    lines.append("")
    r1_hits = sum(1 for r in report.case_results if r.hit_rank == 1)
    lines.append(
        f"- **recall@1**：{_fmt_pct(report.recall_at_1)}（{r1_hits}/{report.total_cases}）"
    )
    hits3 = sum(1 for r in report.case_results if r.hit_rank is not None and r.hit_rank <= 3)
    lines.append(f"- **recall@3**：{_fmt_pct(report.recall_at_3)}（{hits3}/{report.total_cases}）")
    lines.append(f"- Top1 平均相似度：{_fmt_score(report.avg_top1_similarity)}")
    if report.write_failures:
        lines.append(f"- 写入失败：{len(report.write_failures)} 条")
    lines.append("")

    # 明细表
    rows = report.case_results[: max(int(max_detail_rows), 0)]
    if rows:
        lines.append(f"## 命中明细（前 {len(rows)} 条）")
        lines.append("")
        lines.append("| 编号 | 事实 | 命中排名 | Top1 相似度 |")
        lines.append("| --- | --- | --- | --- |")
        for r in rows:
            if r.error:
                rank = "检索异常"
            else:
                rank = str(r.hit_rank) if r.hit_rank is not None else "未命中"
            fact_preview = r.case.fact_text
            if len(fact_preview) > 40:
                fact_preview = fact_preview[:40] + "…"
            lines.append(
                f"| #{r.case.index:03d} | {fact_preview} | {rank} | {_fmt_score(r.top1_similarity)} |"
            )
        hidden = len(report.case_results) - len(rows)
        if hidden > 0:
            lines.append("")
            lines.append(f"（其余 {hidden} 条明细略，完整内容见存档报告）")
        lines.append("")

    # 未命中明细
    misses = [r for r in report.case_results if r.hit_rank is None]
    if misses:
        lines.append(f"## 未命中明细（{len(misses)} 条）")
        lines.append("")
        for r in misses:
            suffix = f"（{r.error}）" if r.error else ""
            lines.append(f"- {r.case.marker} 提问：{r.case.question}{suffix}")
        lines.append("")

    # 写入失败明细
    if report.write_failures:
        lines.append(f"## 写入失败明细（{len(report.write_failures)} 条）")
        lines.append("")
        for item in report.write_failures:
            lines.append(f"- {item}")
        lines.append("")

    lines.append("## 说明")
    lines.append("")
    lines.append(
        "- 命中判据：召回内容包含事实编号标记，或剥离元数据后包含事实原文；"
        "相似度由检索层换算为\"越大越相似\"（L2 距离按 1/(1+d) 折算）。"
    )
    lines.append(
        "- 合成事实写入独立的评测会话，不进入日常对话的记忆召回，"
        "如需清理可按会话 ID 自行删除。"
    )
    if archive_note:
        lines.append(f"- {archive_note}")
    if cleanup_note:
        lines.append(f"- {cleanup_note}")
    return "\n".join(lines)
