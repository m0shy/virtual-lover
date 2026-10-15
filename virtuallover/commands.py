# -*- coding: utf-8 -*-
"""命令面（SPEC §8.5）—— / 前缀命令组 + 管理员判定 + 门禁。

命令组与行为等价性（对照旧栈实测）：
- /mood          ：任何人设内用户可查（render_user_reply 人设化模糊版）；
- /mood stats    ：管理员调试视图（render_stats_reply 数值版），拒绝文案
                   "只有管理员才能查看数值统计哦。"（旧 main.py:207-213）；
- /mood reset    ：管理员重置，成功/无数据文案同旧 main.py:194-205；
- /diary [date]  ：管理员查看日记存档（旧 vl_diary main.py:429-464，文案逐条等价）；
- /eval help     ：评测帮助（旧 vl_eval HELP_TEXT 语义）；
- /eval memory[n]：管理员触发记忆召回评测（n 默认 10、非法/超界提示等价旧栈），
                   评测实现由记忆组提供（注入 eval_runner，本模块只做路由与鉴权）；
- /story         ：激活故事模式（soul.story 开 2 轮窗口）并回
                   「好呀，想听什么类型的故事？」；
- /play [kind]   ：小游戏菜单 / 开局（guess/idiom/truth，本地判定见
                   soul.games）与 /play stop 收摊；对话内判定不经命令，
                   由管线侧调 games.handle_game_message；
- /sing [主题]    ：以与 /brain 同源的大脑客户端（ctx.chat_client，专用
                   prompt 放宽句长约束）生成一段歌词并「念」出——拿到
                   语音门面时走 deliver_proactive 双轨（成功只发语音、
                   失败发文字），拿不到只回文字；文案注明是念不是真唱；
- /poem [主题]    ：同 /sing，生成一首小情诗；
- 未知 / 命令     ：静默忽略（旧栈未注册命令同样不响应）。

管理员判定（SPEC §8.5）：user_id ∈ 配置 admins（等价旧 ADMIN 语义，真值由
迁移脚本从 local/ 带入，本模块不硬编码任何 QQ 号）。
门禁（SPEC §8.6）：仅白名单/管理员 user_id 触发对话与命令；未命中静默忽略
（返回 None，调用方据此决定不发任何消息）。

本模块不含网络与存储实现：mood 引擎 / diary 服务 / eval 运行器 / 大脑客户端 /
语音门面 / 游戏与故事状态目录全部依赖注入。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from .soul import games as games_mod
from .soul import mood as mood_mod
from .soul import story as story_mod
from .soul.diary import validate_date_str

logger = logging.getLogger("virtuallover.commands")

# /eval memory 的条数默认值与上限（旧 vl_eval main.py:104-119 实测行为）
EVAL_DEFAULT_N = 10
EVAL_MAX_FACTS = 50

# /eval help 文案（旧 vl_eval HELP_TEXT 等价语义，按新栈架构改写）
EVAL_HELP_TEXT = (
    "📖 /eval 评测命令组\n"
    "━━━━━━━━━━━━━━━━\n"
    "▪ /eval memory [n] —— 记忆召回评测（管理员）\n"
    "    生成 n 条带编号的合成事实写入长期记忆（默认 10），"
    "等待索引生效后用提问变体逐条检索，\n"
    "    输出 recall@1 / recall@3 与 Top1 平均相似度的评测报告。\n"
    "    合成事实写入独立的评测会话，不会混入日常对话记忆。\n"
    "▪ /eval help —— 显示本帮助"
)

MOOD_USAGE_TEXT = (
    "用法：/mood 查看状态；/mood stats 查看数值统计（管理员）；"
    "/mood reset 重置状态（管理员）。"
)

BRAIN_USAGE_TEXT = (
    "用法：/brain 查看大脑降级状态（管理员）；/brain local 手动切本地大脑（管理员）；"
    "/brain cloud 切回云端大脑（管理员）。"
)

# ---------------------------------------------------------------------------
# /sing /poem 专用 prompt 与文案（生成一段歌词/一首小情诗，单独放宽句长约束；
# prompt 只提「你的人设身份」，人设原文运行时经 persona_system_prompt 注入，
# 不把任何人设名写进代码）
# ---------------------------------------------------------------------------

SING_PROMPT_TEMPLATE = (
    "[系统任务：唱歌] 请以你的人设身份和口吻，为主题「{topic}」即兴写一小段歌词："
    "4-8 行，每行不超过 14 字，俏皮温柔、贴合你们的关系。"
    "本次临时放宽「1-3 句」与短句约束；直接输出歌词正文，"
    "不要任何解释、标题或舞台说明。"
)

POEM_PROMPT_TEMPLATE = (
    "[系统任务：念诗] 请以你的人设身份和口吻，为主题「{topic}」写一首小情诗："
    "4-6 行，每行不超过 14 字，真挚可爱、贴合你们的关系。"
    "本次临时放宽「1-3 句」与短句约束；直接输出诗的正文，不要任何解释或标题。"
)

SING_DEFAULT_TOPIC = "想你"
POEM_DEFAULT_TOPIC = "我们的日常"

# 文案注明这是『念』不是真唱（拼在生成正文之后）
SING_NOTE_TEXT = "（我不会真的唱歌啦，这是念给你听的哦～）"
POEM_NOTE_TEXT = "（是念给你听的小诗哦，可不是唱的～）"

# 大脑客户端缺失 / 生成失败的降级文案
BRAIN_MISSING_TEXT = "大脑还没连上，等我缓一缓再念给你听好不好？"
GENERATE_FAIL_TEXT = "（她小声）呜…现在嗓子有点哑，等下再让我念给你听好不好？"


# ---------------------------------------------------------------------------
# 管理员判定与门禁（SPEC §8.5 / §8.6）
# ---------------------------------------------------------------------------


def is_admin_user(user_id: str, admins: list[str] | tuple[str, ...] | set[str]) -> bool:
    """管理员判定：user_id ∈ 配置 admins（字符串精确比较，等价旧 ADMIN 语义）。"""
    if not user_id:
        return False
    return str(user_id) in {str(a) for a in (admins or [])}


def gate_user(
    user_id: str,
    admins: list[str] | tuple[str, ...] | set[str],
    whitelist: list[str] | tuple[str, ...] | set[str],
) -> bool:
    """§8.6 门禁：仅白名单/管理员 user_id 触发对话与主动消息，未命中静默忽略。"""
    if not user_id:
        return False
    if is_admin_user(user_id, admins):
        return True
    return str(user_id) in {str(w) for w in (whitelist or [])}


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """解析 / 前缀命令：返回 (命令名, 参数列表)；非命令文本返回 None。

    - 命令名取第一个 token 去掉前导 /（如 "/mood" → "mood"）；
    - 子命令作为普通参数保留（/mood stats → ("mood", ["stats"])，
      /eval memory 5 → ("eval", ["memory", "5"])），由各命令自行解释。
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    tokens = stripped[1:].split()
    if not tokens:
        return None
    return tokens[0].lower(), tokens[1:]


# ---------------------------------------------------------------------------
# 命令路由
# ---------------------------------------------------------------------------


@dataclass
class CommandRouter:
    """命令路由器：/mood /diary /eval /brain /story /play /sing /poem 命令组
    + 管理员判定 + 门禁。

    依赖注入：
    - mood_engine：soul.mood.MoodEngine（状态查询/重置）；
    - diary_service：soul.diary.DiaryService（日记存档读取，/diary 用）；
    - eval_runner：async (n: int) -> list[str]，/eval memory 的评测执行器
      （记忆组实现；None 时返回"评测组件未装配"提示）；
    - brain_router：brain.router.BrainRouter（智能降级主路由，/brain 用；
      None 时返回"智能降级未启用"提示——[chat] fallback_enabled=false 的
      正常态，不是故障）；
    - today_fn：() -> str，今天日期（YYYY-MM-DD），测试可注入固定日期；
    - brain_client：与 /brain 同源的大脑客户端（ctx.chat_client，
      fallback_enabled=true 时即 BrainRouter 本体；/sing /poem 的文本生成用；
      None 时回"大脑还没连上"）；
    - voice_manager：voice.manager.VoiceManager（/sing /poem 双轨发送，
      deliver_proactive 同款「成功只发语音、失败回退文字」；None 时只回文字）；
    - persona_system_prompt：人设 system 原文（cfg.persona.system_prompt，
      /sing /poem 生成时在场；None 时仅靠专用 prompt）；
    - state_dir：运行时状态目录（默认 local/vl_data；/story 与 /play 的
      状态文件都落在这里；None 时对应命令回"未装配"提示）。
    """

    mood_engine: object | None = None
    diary_service: object | None = None
    eval_runner: Callable[[int], Awaitable[list[str]]] | None = None
    brain_router: object | None = None
    today_fn: Callable[[], str] | None = None
    brain_client: object | None = None
    voice_manager: object | None = None
    persona_system_prompt: str | None = None
    state_dir: "str | Path | None" = None

    async def handle(
        self,
        text: str,
        user_id: str,
        *,
        admins: list[str],
        whitelist: list[str],
    ) -> list[str] | None:
        """处理一条用户消息里的命令；返回回复消息列表（None=静默忽略，
        []=已处理完毕无需再发——/sing /poem 语音双轨成功时用）。

        门禁前置（§8.6）：非白名单/管理员的一切命令一律静默忽略。
        """
        parsed = parse_command(text)
        if parsed is None:
            return None
        if not gate_user(user_id, admins, whitelist):
            logger.info("命令来自白名单外用户，静默忽略。")
            return None
        name, args = parsed
        if name == "mood":
            return await self._handle_mood(args, user_id, admins=admins)
        if name == "diary":
            return await self._handle_diary(args, user_id, admins=admins)
        if name == "eval":
            return await self._handle_eval(args, user_id, admins=admins)
        if name == "brain":
            return await self._handle_brain(args, user_id, admins=admins)
        if name == "story":
            return await self._handle_story(args, user_id, admins=admins)
        if name == "play":
            return await self._handle_play(args, user_id, admins=admins)
        if name == "sing":
            return await self._handle_sing_poem(args, user_id, kind="sing", admins=admins)
        if name == "poem":
            return await self._handle_sing_poem(args, user_id, kind="poem", admins=admins)
        # 未注册命令：静默忽略（旧栈未注册命令同样不响应）
        return None

    # ---------------- /mood ----------------

    async def _handle_mood(self, args: list[str], user_id: str, *, admins: list[str]) -> list[str]:
        """/mood[/stats|/reset]：行为与文案等价旧 vl_mood_engine main.py:189-224。"""
        engine = self.mood_engine
        action = args[0] if args else ""

        if action == "reset":
            if not is_admin_user(user_id, admins):
                return ["只有管理员才能重置内心状态哦。"]
            if engine is None:
                return ["情绪引擎未装配，暂时无法重置哦。"]
            deleted = await engine.reset_user(user_id)
            if deleted:
                return ["已重置你这里的内心状态，我们重新认识一下吧～"]
            return ["当前还没有需要重置的状态哦。"]

        if action == "stats":
            if not is_admin_user(user_id, admins):
                return ["只有管理员才能查看数值统计哦。"]
            state = engine.storage.get(user_id) if engine is not None else mood_mod.MoodState()
            return [mood_mod.render_stats_reply(state)]

        if action:
            return [MOOD_USAGE_TEXT]

        state = engine.storage.get(user_id) if engine is not None else mood_mod.MoodState()
        return [mood_mod.render_user_reply(state)]

    # ---------------- /diary ----------------

    async def _handle_diary(self, args: list[str], user_id: str, *, admins: list[str]) -> list[str]:
        """/diary [date]：管理员查看日记存档，文案等价旧 vl_diary main.py:429-464。"""
        if not is_admin_user(user_id, admins):
            return ["只有管理员才能查看她的日记哦。"]
        if self.diary_service is None:
            return ["日记服务未装配，暂时无法查看哦。"]

        if not args:
            # /diary：看今天（没有则看最近一篇）
            today = self.today_fn() if self.today_fn else None
            today = today or _local_today()
            content = await self.diary_service.read_diary(today)
            if content is not None:
                return [f"📜 她的日记（{today}）：\n{content}"]
            latest = await self.diary_service.latest_diary_date()
            if not latest:
                return ["还没有日记存档，等今晚她写完再来翻吧～"]
            content = await self.diary_service.read_diary(latest)
            if content is None:
                return ["还没有找到任何日记存档。"]
            return [f"📜 她的日记（最近一篇，{latest}）：\n{content}"]

        # /diary <YYYY-MM-DD>：看指定日期
        canonical = validate_date_str(args[0])
        if not canonical:
            return ["日期格式不对哦，请用 YYYY-MM-DD，例如：/diary 2026-10-10"]
        content = await self.diary_service.read_diary(canonical)
        if content is None:
            return [f"{canonical} 这天没有日记存档。"]
        return [f"📜 她的日记（{canonical}）：\n{content}"]

    # ---------------- /brain ----------------

    async def _handle_brain(self, args: list[str], user_id: str, *, admins: list[str]) -> list[str]:
        """/brain [status|local|cloud]：管理员的大脑降级观测与手动切换。

        status：模式（云端/黏性本地）+ 降级计数 + 最近 LLM 错误（含 HTTP code）
        + 本地大脑进程状态；local/cloud 直接转 BrainRouter.force_local/cloud。
        """
        if not is_admin_user(user_id, admins):
            return ["只有管理员才能查看和切换大脑哦。"]
        router = self.brain_router
        sub = args[0].lower() if args else "status"

        if sub == "status":
            if router is None:
                return ["智能降级未启用（vl.toml [chat] fallback_enabled=false），"
                        "当前按云端大脑运行。"]
            return [_render_brain_status(router.status())]

        if sub == "local":
            if router is None:
                return ["智能降级未启用（vl.toml [chat] fallback_enabled=false），"
                        "无法切本地大脑。"]
            ok, message = router.force_local()
            if ok:
                return [f"✅ {message}"]
            return [f"❌ {message}"]

        if sub == "cloud":
            if router is None:
                return ["智能降级未启用（vl.toml [chat] fallback_enabled=false），"
                        "当前就是云端大脑。"]
            router.force_cloud()
            return ["✅ 已切回云端大脑（下一条消息起生效；本地 llama-server 进程保留，"
                    "可在管理控制台「引擎与人格」页用「只停本地引擎」停止）"]

        return [BRAIN_USAGE_TEXT]

    # ---------------- /eval ----------------

    async def _handle_eval(self, args: list[str], user_id: str, *, admins: list[str]) -> list[str]:
        """/eval help | /eval memory [n]：路由到记忆组评测器，本模块只做鉴权与参数。"""
        sub = args[0].lower() if args else "help"

        if sub == "help":
            return [EVAL_HELP_TEXT]

        if sub == "memory":
            if not is_admin_user(user_id, admins):
                return ["只有管理员才能运行记忆评测哦。"]

            # 条数解析与钳制（等价旧 vl_eval main.py:106-119）
            n = EVAL_DEFAULT_N
            prelude: list[str] = []
            raw = args[1] if len(args) > 1 else None
            if raw is not None and str(raw).strip():
                try:
                    n = int(str(raw).strip())
                except ValueError:
                    prelude.append(f"⚠️ 条数参数「{raw}」无效，已按默认 {EVAL_DEFAULT_N} 执行。")
                    n = EVAL_DEFAULT_N
            clamped = min(max(n, 1), EVAL_MAX_FACTS)
            if clamped != n:
                prelude.append(
                    f"⚠️ 条数已调整：n={n} 超出范围 [1, {EVAL_MAX_FACTS}]，按 {clamped} 执行。"
                )
                n = clamped

            if self.eval_runner is None:
                return prelude + ["❌ 评测组件未装配（eval_runner 为空），无法执行。"]
            results = list(await self.eval_runner(n) or [])
            return prelude + results

        # /eval <未知子命令>：给帮助（等价旧栈 command_group 的兜底体验）
        return [EVAL_HELP_TEXT]

    # ---------------- /story ----------------

    async def _handle_story(self, args: list[str], user_id: str, *,
                            admins: list[str]) -> list[str]:
        """/story：激活故事模式（soul.story 开默认 2 轮窗口）并回开场问句。

        故事讲几轮、豁免块注入由管线侧消费 soul.story（is_active/escape_block/
        consume_turn）；本命令只负责开窗与开场白。
        """
        if not self.state_dir:
            return ["故事模式还没装配好，稍后再来找我吧～"]
        story_mod.activate(self.state_dir)
        return ["好呀，想听什么类型的故事？"]

    # ---------------- /play ----------------

    async def _handle_play(self, args: list[str], user_id: str, *,
                           admins: list[str]) -> list[str]:
        """/play [guess|idiom|truth|stop]：小游戏菜单/开局/收摊（本地判定）。

        对话内的游戏输入不经命令：管线侧对每条非命令消息调
        games.handle_game_message（属于当前游戏则本地判定，否则交回聊天）。
        """
        if not self.state_dir:
            return ["游戏组件未装配，暂时玩不了哦～"]
        sub = str(args[0]).lower() if args else ""
        if sub in games_mod.GAME_KINDS:
            return games_mod.start_game(sub, self.state_dir)
        if sub == "stop":
            return games_mod.stop_game(self.state_dir)
        return [games_mod.game_menu()]

    # ---------------- /sing /poem ----------------

    async def _handle_sing_poem(self, args: list[str], user_id: str, *,
                                kind: str, admins: list[str]) -> list[str]:
        """/sing [主题] 与 /poem [主题]：生成一段歌词/一首小情诗并「念」出。

        - 生成：与 /brain 同源的大脑客户端（brain_client=ctx.chat_client）
          + 专用 prompt（单独放宽句长约束、保持她的口吻；人设原文经
          persona_system_prompt 注入 system 位），同步等结果；
        - 发送：拿得到语音门面走 deliver_proactive 同款双轨——合成成功只发
          语音（返回 []，命令路径不再补发文字），失败回退文字；拿不到只回
          文字；
        - 文案拼注「这是念不是真唱」；生成失败/客户端缺失回卖萌降级文案。
        """
        is_sing = kind == "sing"
        topic = " ".join(args).strip() or (
            SING_DEFAULT_TOPIC if is_sing else POEM_DEFAULT_TOPIC)
        template = SING_PROMPT_TEMPLATE if is_sing else POEM_PROMPT_TEMPLATE
        note = SING_NOTE_TEXT if is_sing else POEM_NOTE_TEXT

        client = self.brain_client
        if client is None:
            return [BRAIN_MISSING_TEXT]
        messages: "list[dict]" = []
        system_text = str(self.persona_system_prompt or "").strip()
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": template.format(topic=topic)})
        try:
            reply = await client.chat_async(messages)
        except Exception as exc:  # noqa: BLE001 —— 生成失败按卖萌降级，不抛
            logger.warning("/%s 文本生成失败：%s", kind, exc)
            return [GENERATE_FAIL_TEXT]
        body = str(getattr(reply, "content", "") or "").strip()
        if not body:
            return [GENERATE_FAIL_TEXT]
        text = f"{body}\n{note}"

        voice_manager = self.voice_manager
        if voice_manager is not None:
            try:
                sent_voice = bool(await voice_manager.deliver_proactive(user_id, text))
            except Exception as exc:  # noqa: BLE001 —— 双轨故障回退文字
                logger.warning("/%s 语音双轨失败（回退文字）：%s", kind, exc)
                sent_voice = False
            if sent_voice:
                return []  # 成功只发语音：命令路径无需再发
        return [text]


def _local_today() -> str:
    """本地今天日期（YYYY-MM-DD）。"""
    return datetime.now().date().isoformat()


def _render_brain_status(status: dict) -> str:
    """把 BrainRouter.status() 快照渲染成人话（/brain status，含 HTTP code）。"""
    mode = str(status.get("mode") or "cloud")
    mode_label = ("🧠 黏性本地模式（云端故障保命中，不自动切回）"
                  if mode == "sticky_local" else "☁️ 云端模式")
    lines = [f"🧠 大脑状态：{mode_label}"]
    if status.get("cloud_recovered"):
        lines.append("✅ 半开探针：云端已恢复，可用 /brain cloud 切回")
    lines.append(
        f"▪ 本地兜底 {int(status.get('fallback_count') or 0)} 次，"
        f"黏性切换 {int(status.get('sticky_switches') or 0)} 次，"
        f"鉴权连败 {int(status.get('consec_auth_fails') or 0)} 轮")
    err = status.get("last_error")
    if err:
        lines.append(f"▪ 最近云端错误：HTTP {err.get('status')}（{err.get('kind')}）"
                     f" {err.get('message')}")
    else:
        lines.append("▪ 最近云端错误：无")
    local_err = status.get("last_local_error")
    if local_err:
        lines.append(f"▪ 最近本地错误：HTTP {local_err.get('status')} {local_err.get('message')}")
    local = status.get("local")
    if isinstance(local, dict):
        lines.append(
            f"▪ 本地大脑：{local.get('host')}:{local.get('port')} "
            f"state={local.get('state')} ownership={local.get('ownership')} "
            f"pid={local.get('pid')}")
        if local.get("last_error"):
            lines.append(f"▪ 本地告警：{local.get('last_error')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# __main__ 装配入口（_SETUP_TARGETS 约定：setup(ctx) -> cleanup | None）
# ---------------------------------------------------------------------------


def setup(ctx) -> None:
    """构造命令路由器挂 ctx.router（依赖经 ctx 就绪的各服务实例注入）。

    - eval_runner 允许为空（/eval memory 届时回"评测组件未装配"提示）；
      集成管线（__main__._wire_pipeline）会在装配后把基于 memory.benchmark
      的评测执行器补挂到 router.eval_runner；
    - admins/whitelist 不在此固化：handle() 每次由调用方传入门禁集合，
      桥侧门禁（cfg.whitelist ∪ cfg.admins）与本路由共享同一配置源；
    - brain_client 与 /brain 同源：fallback_enabled=true 时 ctx.chat_client
      即 BrainRouter 本体（llm._wire_brain_router 已换装），否则是云端
      ChatClient——/sing /poem 用它生成文本；
    - voice_manager 接 voice.manager 门面（装配序在本模块之前）：
      /sing /poem 的双轨发送（deliver_proactive 同款语义）；
    - state_dir 取 repo_root/storage.data_dir（local/vl_data，同
      proactive_state.json 的落盘口径）：/story 与 /play 的状态文件都在这里。
    """
    persona_cfg = getattr(ctx.cfg, "persona", None)
    persona_system_prompt = (
        str(getattr(persona_cfg, "system_prompt", "") or "").strip() or None)
    state_dir = Path(ctx.repo_root) / ctx.cfg.storage.data_dir
    ctx.router = CommandRouter(
        mood_engine=getattr(ctx, "mood_engine", None),
        diary_service=getattr(ctx, "diary_service", None),
        eval_runner=getattr(ctx, "eval_runner", None),
        brain_router=getattr(ctx, "brain_router", None),
        today_fn=None,
        brain_client=getattr(ctx, "chat_client", None),
        voice_manager=getattr(ctx, "voice_manager", None),
        persona_system_prompt=persona_system_prompt,
        state_dir=state_dir,
    )
    logger.info(
        "[commands] 命令路由器装配完成（mood=%s diary=%s eval=%s brain=%s "
        "story/play state_dir=%s sing/poem client=%s voice=%s）",
        ctx.router.mood_engine is not None,
        ctx.router.diary_service is not None,
        ctx.router.eval_runner is not None,
        ctx.router.brain_router is not None,
        state_dir,
        ctx.router.brain_client is not None,
        ctx.router.voice_manager is not None)
