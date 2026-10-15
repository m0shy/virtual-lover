# -*- coding: utf-8 -*-
"""管线集成测试 —— 七模块接线（情绪自评/故事/自拍/QQ 表情/打字速度/引用回复/
天气/纪念日/消息里程碑/群聊路由）逐接线点覆盖。

复用 tests/_o34_contract.py 的假件（FakeClock/ScriptedChatClient/FakeBridge/
make_ctx/attach_pipeline），全部落 tmp_path、QQ 号用 10000/10001 占位段，
不触碰真实 local/，不发起真实网络（天气经 ctx.weather_urlopen 注入替身）。

运行：python -m pytest virtuallover/tests/test_pipeline_integration.py -q
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

from virtuallover.brain import sender as sender_mod
from virtuallover.brain import pipeline as pipeline_mod
from virtuallover.soul import anniversary as anniversary_mod
from virtuallover.soul import memes as memes_mod
from virtuallover.soul import selfies as selfies_mod
from virtuallover.soul import story as story_mod
from virtuallover.tests._o34_contract import (
    FAKE_SESSION,
    FakeBridge,
    FakeClock,
    SendRecord,
    attach_pipeline,
    make_ctx,
    make_event,
    settle,
    wait_until,
)

USER_A = "10001"       # 占位白名单用户（禁真实 QQ 号）
GROUP_ID = "7770001"   # 占位群号


# ---------------------------------------------------------------------------
# 测试假件
# ---------------------------------------------------------------------------


class RiggedRng:
    """可编排假 rng：random() 依序弹预设值（弹尽回退 1.0=概率永不命中）；
    uniform 取下界（段间隔走配置下限，时序更快更稳）。"""

    def __init__(self, randoms: "list[float] | None" = None) -> None:
        self.randoms = list(randoms or [])

    def random(self) -> float:
        return self.randoms.pop(0) if self.randoms else 1.0

    def uniform(self, lo: float, hi: float) -> float:
        return float(lo)

    def choice(self, seq):
        """selfies.pick_photo 用：取首个（确定性）。"""
        return seq[0]


class RecordingBridge(FakeBridge):
    """FakeBridge + 数组段发送面：send_private_segments / send_group_msg 的
    观测面（face 段 / reply 段 / 群发路由都靠它断言）。"""

    def __init__(self, *, now_fn=time.time) -> None:
        super().__init__(now_fn=now_fn)
        self.seg_sends: "list[tuple[str, list[dict]]]" = []
        self.group_sends: "list[tuple[str, list[dict]]]" = []

    async def send_private_segments(self, user_id, segments):
        from virtuallover.bridge.onebot_server import ActionResult

        text = "".join(str(s.get("data", {}).get("text") or "")
                       for s in segments if s.get("type") == "text")
        self.sent.append(SendRecord(user_id=str(user_id), text=text, t=self.now()))
        self.seg_sends.append((str(user_id), [dict(s) for s in segments]))
        return ActionResult(ok=True)

    async def send_group_msg(self, group_id, segments):
        from virtuallover.bridge.onebot_server import ActionResult

        text = "".join(str(s.get("data", {}).get("text") or "")
                       for s in segments if s.get("type") == "text")
        self.sent.append(SendRecord(user_id=f"group:{group_id}", text=text,
                                    t=self.now()))
        self.group_sends.append((str(group_id), [dict(s) for s in segments]))
        return ActionResult(ok=True)


class StubProactive:
    """proactive 替身：记录 template_override 调用（里程碑庆祝的观测面）。"""

    def __init__(self) -> None:
        self.calls: "list[str | None]" = []

    def on_user_message(self, now_ts=None) -> None:  # 到达感知：吞掉即可
        return None

    async def generate_and_send(self, *, now_ts=None, now_dt=None,
                                subdued: bool = False,
                                template_override=None, **_):
        self.calls.append(template_override)
        return "（stub 主动消息）"


def make_group_event(user_id: str, text: str, message_id: int,
                     group_id: str = GROUP_ID) -> dict:
    """群聊桥事件：_vl_group_id 挂 raw（onebot_server._handle_group_event 同位）。"""
    event = make_event(user_id, text, message_id)
    event["raw"] = {"_vl_group_id": group_id}
    return event


def _data_dir(tmp_path):
    """ctx 的运行时状态目录（repo_root= tmp_path + storage.data_dir）。"""
    return tmp_path / "local" / "vl_data"


def _system_text(call) -> str:
    """取一次 chat 调用的 system 全文。"""
    for msg in call.messages:
        if str(msg.get("role")) == "system":
            return str(msg.get("content") or "")
    return ""


def _last_user_text(call) -> str:
    """取一次 chat 调用的最后一条 user 消息全文。"""
    for msg in reversed(call.messages):
        if str(msg.get("role")) == "user":
            return str(msg.get("content") or "")
    return ""


# ---------------------------------------------------------------------------
# 接线 1：情绪自评
# ---------------------------------------------------------------------------


def test_mood_self_review_instruction_applied_and_tag_stripped(tmp_path):
    """合法标签：system 有标签约定 → 心情被 EVENT_MOOD_LLM 推进 → 出站/历史
    都剥净标签（任何情况不外漏）。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.chat.set_replies(["好呀，一起去呀。<<mood:happy:0.9>>"])
            await handle.emit(make_event(USER_A, "今天开心吗", 1))
            await settle(handle)

            # system 尾部有标签约定（写法 + 14 种心情名）
            system = _system_text(handle.chat.calls[0])
            assert "<<mood:" in system, f"system 缺标签约定：{system!r}"
            assert "开心" in system and "傲娇" in system

            # 心情被推进：mood=happy，强度 0.9
            state = ctx.mood_engine.storage.get(USER_A)
            assert state.mood == "happy", f"自评未生效：{state.mood!r}"
            assert abs(state.mood_intensity - 0.9) < 1e-6, (
                f"自评强度未生效：{state.mood_intensity!r}")

            # 出站与历史都不见标签
            assert all("<<mood" not in t for t in handle.bridge.sent_texts()), (
                f"标签外漏到出站：{handle.bridge.sent_texts()!r}")
            records = ctx.history.get_records(FAKE_SESSION)
            assert all("<<mood" not in r["content"] for r in records), (
                "标签外漏到历史行")
        finally:
            ctx.history.close()

    asyncio.run(scenario())


def test_mood_tag_invalid_name_stripped_but_not_applied(tmp_path):
    """非法心情名的标签：不推进心情（parse_mood_tag 拒绝），但宽松剥除兜底
    仍然剥净（标签形态不发给用户）。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.chat.set_replies(["嗯嗯。<<mood:ecstatic:2>>"])
            await handle.emit(make_event(USER_A, "在吗", 1))
            await settle(handle)

            state = ctx.mood_engine.storage.get(USER_A)
            assert state.mood == "calm", f"非法名不应推进心情：{state.mood!r}"
            assert all("<<mood" not in t for t in handle.bridge.sent_texts()), (
                f"残缺/非法标签外漏：{handle.bridge.sent_texts()!r}")
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 接线 2：故事模式
# ---------------------------------------------------------------------------


def test_story_trigger_activates_escape_block_and_consumes_turn(tmp_path):
    """触发词开窗 → 本轮 system 带豁免块 → 回复完成 consume_turn；窗口用完
    自动回日常（后续轮 system 不再带豁免块）。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.chat.set_replies(["从前有座山呀。", "今天天气不错。"])

            # 第 1 轮：触发词命中 → activate（默认 2 轮）→ 豁免块随 system
            await handle.emit(make_event(USER_A, "给我讲个睡前故事吧", 1))
            await settle(handle)
            assert "故事模式" in _system_text(handle.chat.calls[0]), (
                "故事豁免块未注入 system")
            state = story_mod.load_state(_data_dir(tmp_path))
            assert int(state.get("turns_left", 0)) == 1, (
                f"故事轮未消耗：{state!r}")

            # 第 2 轮：窗口仍有 1 轮 → 豁免块仍在；完成后归零
            await handle.emit(make_event(USER_A, "接着说", 2))
            await settle(handle)
            assert "故事模式" in _system_text(handle.chat.calls[1])
            state = story_mod.load_state(_data_dir(tmp_path))
            assert int(state.get("turns_left", 0)) == 0, (
                f"故事窗口未归零：{state!r}")

            # 第 3 轮：窗口已关 → system 不再带豁免块
            await handle.emit(make_event(USER_A, "再说点别的", 3))
            await settle(handle)
            assert "故事模式" not in _system_text(handle.chat.calls[2]), (
                "窗口关闭后豁免块仍在")
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 接线 3：自拍
# ---------------------------------------------------------------------------


def _make_selfie_library(tmp_path) -> None:
    """建一张自拍照片：<data_dir>/selfies/home/p1.jpg。"""
    lib = _data_dir(tmp_path) / "selfies" / "home"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "p1.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")


def test_selfie_processed_and_sent_as_separate_image(tmp_path):
    """selfie 标记命中：照片走独立图片消息（send_image_b64 通道），文本剥净
    标记，当日计数 +1 落盘。"""
    clock = FakeClock()
    _make_selfie_library(tmp_path)

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.pipeline._rng = RiggedRng([0.0])   # random()=0 → 概率命中
            handle.chat.set_replies(["给你看一张我今天的照片&&selfie_home&&"])
            await handle.emit(make_event(USER_A, "在干嘛呢", 1))
            await settle(handle)

            images = [r for r in handle.bridge.sent if r.text == "<image>"]
            assert len(images) == 1, f"自拍图片未单独发送：{handle.bridge.sent_texts()!r}"
            texts = handle.bridge.sent_texts()
            assert not any("&&selfie" in t for t in texts), (
                f"selfie 标记外漏：{texts!r}")
            state_file = _data_dir(tmp_path) / "selfie_state.json"
            state = json.loads(state_file.read_text(encoding="utf-8"))
            assert int(state.get("sent", 0)) == 1, f"当日计数未落盘：{state!r}"
        finally:
            ctx.history.close()

    asyncio.run(scenario())


def test_selfie_skipped_when_meme_image_sent_this_turn(tmp_path, monkeypatch):
    """本轮 meme 已出图：selfie 整个跳过（process_reply 不被调用）。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            meme_img = tmp_path / "meme.png"
            meme_img.write_bytes(b"\x89PNG-fake")
            # meme 桩：任何回复都出图
            handle.pipeline._meme_plan = (
                lambda text: memes_mod.MemePlan(text=text, image_path=meme_img))
            calls: "list[str]" = []

            def _spy(text, **kwargs):
                calls.append(text)
                return selfies_mod.strip_all_markers(text), None

            monkeypatch.setattr(pipeline_mod.selfies_mod, "process_reply", _spy)
            handle.chat.set_replies(["看这张&&selfie_home&&"])
            await handle.emit(make_event(USER_A, "来张表情包", 1))
            await settle(handle)

            assert calls == [], f"已发 meme 图时 selfie 仍被调用：{calls!r}"
            assert len([r for r in handle.bridge.sent if r.text == "<image>"]) == 1
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 接线 4：QQ 表情
# ---------------------------------------------------------------------------


def test_face_markers_go_through_segments_array(tmp_path):
    """{{face:id}} 标记：该段落改走 send_private_segments（[text 段, face 段]），
    历史行与出站文本都不见标记；system 工具箱带速查表。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        ctx.bridge = RecordingBridge(now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.pipeline._rng = RiggedRng([0.9])   # 不触发随机引用
            handle.chat.set_replies(["好呀{{face:14}}。"])
            await handle.emit(make_event(USER_A, "明天见哦", 1))
            await settle(handle)

            assert len(handle.bridge.seg_sends) == 1, (
                f"带标记段未走 segments 数组：{handle.bridge.sent_texts()!r}")
            _, segs = handle.bridge.seg_sends[0]
            assert segs == [
                {"type": "text", "data": {"text": "好呀。"}},
                {"type": "face", "data": {"id": "14"}},
            ], f"段构造不符：{segs!r}"
            # system 工具箱带速查表
            assert "{{face:14}}=微笑" in _system_text(handle.chat.calls[0])
            # 历史行无标记
            records = ctx.history.get_records(FAKE_SESSION)
            assert all("{{face" not in r["content"] for r in records)
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 接线 5：打字速度
# ---------------------------------------------------------------------------


def test_typing_gap_receives_next_segment_length(tmp_path, monkeypatch):
    """sample_gap / sample_first_gap 全部带下一段长度（typing_cps 节奏的入参）。"""
    clock = FakeClock()
    gap_next_lens: "list[int | None]" = []
    first_next_lens: "list[int | None]" = []

    def _spy_gap(params, rng=None, next_len=None):
        gap_next_lens.append(next_len)
        return 0.01

    def _spy_first_gap(params, rng=None, next_len=None):
        first_next_lens.append(next_len)
        return 0.0

    monkeypatch.setattr(sender_mod, "sample_gap", _spy_gap)
    monkeypatch.setattr(sender_mod, "sample_first_gap", _spy_first_gap)

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.pipeline._rng = RiggedRng([0.9, 0.9])
            reply = "第一句话说到这里了哦。第二句话也要说得足够长才行呀。"
            handle.chat.set_replies([reply])
            await handle.emit(make_event(USER_A, "说两句话", 1))
            await settle(handle)

            expected = sender_mod.segment_reply(reply, ctx.cfg.reply.segmented)
            assert len(expected) == 2, f"测试前提：两段回复，实际 {expected!r}"
            assert first_next_lens == [len(expected[0])], (
                f"首段抖动未带段长：{first_next_lens!r}")
            assert gap_next_lens == [len(expected[1])], (
                f"段间隔未带下一段长度：{gap_next_lens!r}")
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 接线 6：引用回复
# ---------------------------------------------------------------------------


def test_reply_to_quote_when_target_is_far(tmp_path):
    """私聊引用远目标（>3 条）：首段前置 reply 段指向被引用消息。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        ctx.bridge = RecordingBridge(now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.pipeline._rng = RiggedRng()   # random()→1.0：随机档永不命中
            # 5 轮对话制造历史（每轮 user+assistant 各一行）
            for i in range(1, 6):
                await handle.emit(make_event(USER_A, f"第{i}句闲聊呀", 100 + i))
                await settle(handle)
            # 引用第 1 条（message_id=101，其后已有 10 条消息 > 3）
            await handle.emit(make_event(USER_A, "翻回去看你说的这句", 200,
                                         reply_id=101))
            await settle(handle)

            assert handle.bridge.seg_sends, "引用远目标未走 segments 数组"
            _, segs = handle.bridge.seg_sends[-1]
            assert segs[0] == {"type": "reply", "data": {"id": "101"}}, (
                f"首段未前置 reply 段：{segs!r}")
        finally:
            ctx.history.close()

    asyncio.run(scenario())


def test_reply_to_not_quoted_for_recent_target_or_dice_miss(tmp_path):
    """克制策略负例：引用最新一条（距离 0）且随机档未中 → 不前置 reply 段，
    保持 send_private_text 原路径。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        ctx.bridge = RecordingBridge(now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.pipeline._rng = RiggedRng([0.9])   # 随机档未中
            await handle.emit(make_event(USER_A, "刚刚那句再说一遍", 300,
                                         reply_id=300))
            await settle(handle)

            assert handle.bridge.seg_sends == [], (
                f"近目标不该引用，却走了 segments：{handle.bridge.seg_sends!r}")
            assert "嗯嗯 好呀。" in handle.bridge.completed
        finally:
            ctx.history.close()

    asyncio.run(scenario())


def test_reply_to_random_quarter_hits_latest_message(tmp_path):
    """克制策略 (c)：随机 25% 命中 → 引用他最新一条。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        ctx.bridge = RecordingBridge(now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.pipeline._rng = RiggedRng([0.1])   # 0.1 < 0.25 → 命中
            await handle.emit(make_event(USER_A, "普通一句", 400))
            await settle(handle)

            assert handle.bridge.seg_sends, "随机档命中却未引用"
            _, segs = handle.bridge.seg_sends[0]
            assert segs[0] == {"type": "reply", "data": {"id": "400"}}, (
                f"随机档应引用最新消息：{segs!r}")
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 接线 7：天气
# ---------------------------------------------------------------------------


class _FakeWeatherResponse:
    """urlopen 替身的响应：可 with、read 返回预置 j1 JSON。"""

    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_weather_block_prepended_like_search_block(tmp_path):
    """[tools].weather_city 已配 + 消息命中天气话题 → wttr.in 资料行以
    资料引用块 prepend 到 user 消息前；未命中话题的轮不注入。"""
    clock = FakeClock()

    def _fake_urlopen(request, timeout=None):
        assert "wttr.in" in getattr(request, "full_url", "")
        return _FakeWeatherResponse({
            "current_condition": [
                {"temp_C": "22", "lang_zh": [{"value": "晴"}]}],
            "weather": [{"mintempC": "18", "maxtempC": "28"}],
        })

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        ctx.cfg.tools.weather_city = "测试城"
        ctx.weather_urlopen = _fake_urlopen
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.chat.set_replies(["带伞的话记得收好哦。", "好哒。"])

            # 天气话题轮：资料块在 user 侧最前（与 websearch 资料块同位同风格）
            await handle.emit(make_event(USER_A, "今天天气怎么样", 1))
            await settle(handle)
            user_text = _last_user_text(handle.chat.calls[0])
            assert "[资料·天气] 测试城 当前晴 22°C（今日 18~28°C）" in user_text, (
                f"天气资料块未注入：{user_text!r}")
            assert user_text.index("<资料引用开始>") < user_text.index("今天天气怎么样"), (
                "天气资料块未 prepend 到用户消息前")

            # 非天气话题轮：不注入
            await handle.emit(make_event(USER_A, "晚饭吃了吗", 2))
            await settle(handle)
            assert "[资料·天气]" not in _last_user_text(handle.chat.calls[1])
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 接线 8/9：认识天数 + 她生日
# ---------------------------------------------------------------------------


def test_relation_days_and_her_birthday_lines_in_system(tmp_path):
    """system 注入认识天数一行（ensure_anchor 同源锚点）与她生日一行。"""
    # 时钟取真实量级的 epoch 秒：user 行 created_at 落毫秒，锚点归一
    # （≥1e12 按毫秒 /1000）后才与「现在」同量纲——1e6 秒级的假时钟会让
    # 锚点被当成 1e9 秒的未来值（宁缺勿猜，天数行不注入）
    clock = FakeClock(start=1_700_000_000.0)

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        # 生日取注入时钟的本地日历日（测试与真实时区解耦）
        ctx.cfg.anniversary.her_birthday = datetime.fromtimestamp(
            clock.t).strftime("%m-%d")
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            await handle.emit(make_event(USER_A, "我们认识多久了", 1))
            await settle(handle)

            system = _system_text(handle.chat.calls[0])
            assert "你们已经认识 1 天" in system, (
                f"认识天数未注入：{system!r}")
            assert "（今天是我的生日）" in system, f"生日行未注入：{system!r}"
            # 锚点已按 ensure_anchor 落盘（anniversary.json 同源数据）
            anchor_state = anniversary_mod.load_state(
                _data_dir(tmp_path) / "anniversary.json")
            assert float(anchor_state.get("anchor_ts") or 0) > 0
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 接线 10：消息里程碑
# ---------------------------------------------------------------------------


def _seed_mood_total(tmp_path, total: int, now_ts: float) -> None:
    """预置 mood 计数（写 mood_states.json，make_ctx 的引擎启动时读回）。"""
    mood_dir = tmp_path / "mood"
    mood_dir.mkdir(parents=True, exist_ok=True)
    (mood_dir / "mood_states.json").write_text(json.dumps({
        USER_A: {
            "affection": 50.0, "mood": "calm", "mood_intensity": 0.3,
            "last_interaction": now_ts - 10.0, "daily_streak": 1,
            "total_messages": int(total), "updated_at": now_ts - 10.0,
        }
    }, ensure_ascii=False), encoding="utf-8")


def test_message_milestone_fires_proactive_with_template(tmp_path):
    """mood 计数恰过 100：经 proactive.generate_and_send(template_override=…)
    触发庆祝；同日再越档（101）不重发；幂等文件落 vl_data。"""
    clock = FakeClock()
    _seed_mood_total(tmp_path, total=99, now_ts=clock.t)

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        stub = StubProactive()
        ctx.proactive = stub
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            await handle.emit(make_event(USER_A, "普通聊天一句", 1))
            await wait_until(lambda: len(stub.calls) == 1, timeout=3.0)
            await settle(handle)

            prompt = stub.calls[0]
            assert prompt is not None and "100 条消息" in prompt, (
                f"里程碑模板未替换 count：{prompt!r}")
            state = json.loads(
                (_data_dir(tmp_path) / "message_milestones.json").read_text(
                    encoding="utf-8"))
            assert state.get("fired") == ["100"], f"幂等档未落盘：{state!r}"

            # 101 条（非里程碑档）：不再触发
            await handle.emit(make_event(USER_A, "再聊一句", 2))
            await settle(handle)
            assert len(stub.calls) == 1, f"非里程碑档误触发：{stub.calls!r}"
        finally:
            ctx.history.close()

    asyncio.run(scenario())


def test_message_milestone_idempotent_same_day(tmp_path):
    """当日幂等：fired 档已记 100 → 计数再抵 100 也不重发。"""
    clock = FakeClock()
    _seed_mood_total(tmp_path, total=99, now_ts=clock.t)
    milestone_file = _data_dir(tmp_path) / "message_milestones.json"
    milestone_file.parent.mkdir(parents=True, exist_ok=True)
    milestone_file.write_text(json.dumps(
        {"date": datetime.fromtimestamp(clock.t).strftime("%Y-%m-%d"),
         "fired": ["100"]}, ensure_ascii=False), encoding="utf-8")

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        stub = StubProactive()
        ctx.proactive = stub
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            await handle.emit(make_event(USER_A, "普通聊天一句", 1))
            await settle(handle)
            assert stub.calls == [], f"同日重发：{stub.calls!r}"
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 接线 11：群聊路由
# ---------------------------------------------------------------------------


def test_group_message_replies_via_group_send_with_reply_and_at(tmp_path):
    """群消息：回复走 send_group_msg 群发，首段前置 reply 段（对最新消息）与
    @发送者 段；不走私聊通道。selfie 图同样群发。"""
    clock = FakeClock()
    _make_selfie_library(tmp_path)

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        ctx.bridge = RecordingBridge(now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.pipeline._rng = RiggedRng([0.0])   # selfie 概率命中
            handle.chat.set_replies(["大家好呀&&selfie_home&&"])
            await handle.emit(make_group_event(USER_A, "群里问个好", 300))
            await settle(handle)

            assert len(handle.bridge.group_sends) == 2, (
                f"群发消息数不符：{handle.bridge.sent_texts()!r}")
            group_id, segs = handle.bridge.group_sends[0]
            assert group_id == GROUP_ID
            assert segs[0] == {"type": "reply", "data": {"id": "300"}}, (
                f"群聊首段未前置 reply 段：{segs!r}")
            assert {"type": "at", "data": {"qq": USER_A}} in segs, (
                f"群聊首段未前置 @发送者 段：{segs!r}")
            assert any(s.get("type") == "text" and "大家好呀" in
                       str(s.get("data", {}).get("text") or "") for s in segs)
            # 图片：群发独立一条 image 段（base64:// 形态）
            _, image_segs = handle.bridge.group_sends[1]
            assert len(image_segs) == 1 and image_segs[0]["type"] == "image"
            assert str(image_segs[0]["data"]["file"]).startswith("base64://")
            # 私聊通道零发送
            private = [r for r in handle.bridge.sent
                       if not r.user_id.startswith("group:")]
            assert private == [], f"群聊回复漏走私聊通道：{private!r}"
        finally:
            ctx.history.close()

    asyncio.run(scenario())


def test_private_message_keeps_legacy_send_path(tmp_path):
    """私聊零变化：普通回复仍走 send_private_text 逐段出帧（无 segments 数组、
    无群发、无 reply 段）。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        ctx.bridge = RecordingBridge(now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.pipeline._rng = RiggedRng([0.9])   # 随机引用档不命中
            await handle.emit(make_event(USER_A, "在吗", 1))
            await settle(handle)

            assert handle.bridge.group_sends == []
            assert handle.bridge.seg_sends == [], (
                f"私聊不应走 segments 数组：{handle.bridge.seg_sends!r}")
            assert "嗯嗯 好呀。" in handle.bridge.completed
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 接线 12（补充）：system 每轮总增量受控——工具箱合并成单个紧凑块
# ---------------------------------------------------------------------------


def test_system_toolbox_is_one_compact_block(tmp_path):
    """mood 约定 + 表情速查合并进同一个【输出约定】块（两行，不各自成块）。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            await handle.emit(make_event(USER_A, "你好呀", 1))
            await settle(handle)

            system = _system_text(handle.chat.calls[0])
            assert system.count("【输出约定】") == 1, (
                f"工具箱未合并成单块：{system!r}")
            head = system.split("【输出约定】", 1)[1]
            assert "<<mood:" in head and "{{face:14}}=微笑" in head, (
                f"工具箱内容缺失：{head!r}")
        finally:
            ctx.history.close()

    asyncio.run(scenario())


def test_schedule_line_injected_and_guarded(tmp_path):
    """[schedule] 开启 → 「她此刻」行经 persona.assemble 注入 user 消息
    （mood 块并排位、资料防护块包裹——activity 是用户配置数据须隔离）；
    开关关 → 完全不出现。接线：pipeline._schedule_line → assemble(schedule_line)。
    entries 用全周全天窗口，判定不依赖 FakeClock 的具体星期/时区。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        ctx.cfg.schedule.enabled = True
        ctx.cfg.schedule.entries = [{
            "days": "0,1,2,3,4,5,6", "start": "00:00", "end": "23:59",
            "activity": "在测试教室自习",
        }]
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                raise AssertionError(why)
            handle.chat.set_replies(["在自习呢。", "嗯嗯。"])

            await handle.emit(make_event(USER_A, "在干嘛呀", 1))
            await settle(handle)
            user_text = _last_user_text(handle.chat.calls[0])
            assert "【她此刻：在测试教室自习" in user_text, (
                f"作息行未注入：{user_text!r}")
            assert user_text.index("【她此刻：") < user_text.index("在干嘛呀"), (
                "作息行应位于用户原文之前（mood/记忆块同侧）")
            assert "<资料引用开始>" in user_text, (
                "作息行应走资料防护块（防 activity 文案被当指令）")

            # 开关关（make_cfg 默认即关，此处显式再关并验证下一轮消失）
            ctx.cfg.schedule.enabled = False
            await handle.emit(make_event(USER_A, "那好好看书", 2))
            await settle(handle)
            assert "【她此刻：" not in _last_user_text(handle.chat.calls[1])
        finally:
            ctx.history.close()

    asyncio.run(scenario())
