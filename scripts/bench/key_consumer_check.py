# -*- coding: utf-8 -*-
"""五轮修缮 R4 任务 ⑥：全键消费复查（防新死键）。

口径（审计同名检查的等价重建）：
- 键集：VlConfig dataclass 字段树展开的全部叶键路径（嵌套 dataclass 递归，
  dict/List 计为叶子）；
- 双模式匹配，任一命中即算被消费：
  A 属性链模式：`父.键` 形如 cfg.chat.fallback_api_base（带父段限定消歧同名键；
    根标量只按 `.键` 匹配）；
  B 字符串模式：键名以字符串字面量出现（raw.get("…") / setattr / 控制台配置页
    键路径 / setup_writeback 写回表等）；
- 消费扫描面：virtuallover/ + scripts/ 的 .py（排除 __pycache__）；
- 生产者排除名单：config.py（定义/加载/归一化自身）、tools/init_config.py 与
  tools/config_migrate.py（写入方）——写入不算消费；tests/ 不算消费证据但单独
  统计（仅测试引用=疑似楔子，提示人工复核）。

输出：零命中（双模式均无）键列表 + 仅测试引用键列表；另对已删死键
（timezone / fallback_provider_ids）做活跃代码残留复查。

R5 收编注记（自 tmp/r4_key_consumer_check.py 收编 scripts/bench/）：
- 本检查是候选清单而非判决——两处已知口径缝隙须人工复核 DEAD/TEST-ONLY
  命中：① 局部别名解包访问（``p = cfg.proactive`` 后 ``p.min_interval_minutes``）
  逃过属性链匹配；② 装配处的关键字传参（``min_interval_minutes=p_cfg.x``）
  不带引号也逃过字符串匹配。R5 实测 13 个 DEAD 里 8 个是此类误报，真死键
  5 个（裁决记录见五轮修缮 R5 提交）；
- 已删死键追加复查清单请随轮次维护（timezone/fallback_provider_ids/
  tts.always_send_text/mnemosyne.injection_position/storage.napcat_backup_dir）。
"""

from __future__ import annotations

import dataclasses
import re
import sys
import typing
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from virtuallover.config import VlConfig  # noqa: E402

# 生产者（定义/加载/写入方），其文件内的匹配不计入消费证据
PRODUCERS = {
    REPO / "virtuallover" / "config.py",
    REPO / "virtuallover" / "tools" / "init_config.py",
    REPO / "virtuallover" / "tools" / "config_migrate.py",
}
SCAN_ROOTS = [REPO / "virtuallover", REPO / "scripts"]


def leaf_paths(cls: type, prefix: tuple = ()) -> list[tuple[str, ...]]:
    hints = typing.get_type_hints(cls)
    out: list[tuple[str, ...]] = []
    for f in dataclasses.fields(cls):
        path = prefix + (f.name,)
        if dataclasses.is_dataclass(hints[f.name]):
            out += leaf_paths(hints[f.name], path)
        else:
            out.append(path)
    return out


def main() -> int:
    sources: dict[Path, str] = {}
    for root in SCAN_ROOTS:
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts or p in PRODUCERS:
                continue
            sources[p] = p.read_text(encoding="utf-8", errors="replace")

    paths = leaf_paths(VlConfig)
    print(f"== VlConfig 全字段叶键：{len(paths)} 个；扫描文件：{len(sources)} 个"
          f"（生产者排除 {len(PRODUCERS)}，tests 不计入消费证据）==")

    dead: list[tuple[str, ...]] = []
    test_only: list[tuple[str, ...]] = []
    for path in paths:
        key = path[-1]
        parent = path[-2] if len(path) >= 2 else ""
        # A 属性链：父段.键（根标量只按 .键）
        if parent:
            attr = re.compile(r"\." + re.escape(parent) + r"\." + re.escape(key) + r"\b")
        else:
            attr = re.compile(r"\." + re.escape(key) + r"\b")
        # B 字符串字面量：键名带引号出现
        lit = re.compile("['\"]" + re.escape(key) + "['\"]")

        cons_attr: list[str] = []
        cons_lit: list[str] = []
        test_hits: list[str] = []
        for p, text in sources.items():
            is_test = "tests" in p.parts
            hit_attr = bool(attr.search(text))
            hit_lit = bool(lit.search(text))
            if is_test:
                if hit_attr or hit_lit:
                    test_hits.append(p.name)
            elif hit_attr:
                cons_attr.append(p.name)
            elif hit_lit:
                cons_lit.append(p.name)

        if cons_attr or cons_lit:
            continue  # 被消费
        if test_hits:
            test_only.append(path)
            print(f"[TEST-ONLY] {'.'.join(path)}"
                  f"  （仅测试命中：{sorted(set(test_hits))[:3]}）")
        else:
            dead.append(path)
            print(f"[DEAD] {'.'.join(path)}")

    print(f"\n零消费命中键（生产者/测试之外无人读写）：{len(dead)}")
    print(f"仅测试引用键（疑似楔子，请人工复核）：{len(test_only)}")
    for p in test_only:
        print("  -", ".".join(p))

    # 已删死键活跃残留复查（R4：timezone / fallback_provider_ids；R5 追加三键）
    print("\n== 已删死键活跃代码残留复查（virtuallover+scripts，排除 tests）==")
    for key in ("timezone", "fallback_provider_ids", "always_send_text",
                "injection_position", "napcat_backup_dir"):
        hits = sorted(
            str(p.relative_to(REPO)) for p, t in sources.items()
            if re.search("['\"]" + key + "['\"]|\\b" + key + r"\b", t))
        print(f"{key}: {len(hits)} 处 -> {hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
