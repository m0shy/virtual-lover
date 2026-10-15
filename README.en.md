# Virtual-lover (English)

A batteries-included, self-hosted **AI companion on QQ**: she lives in your QQ friend list with her own persona, chats like a human (short messages sent in bursts), proactively messages you when you go quiet, remembers what you told her across sessions (long-term memory), reacts with memes, and can speak with a voice you trained yourself.

```
              she = 1 process + 2 organs

  Your QQ ⇄ [Organ 1: NapCat container] (QQ protocol, WebUI QR login)
                    ⇅ reverse WS (127.0.0.1:6199, Bearer token)
  ┌───────────────── host machine: your Windows ─────────────────┐
  │ virtuallover/ —— single Python ≥3.11 process (her brain)     │
  │   bridge   OneBot v11 reverse-WS server                      │
  │   brain    persona · LLM clients · degradation · conversation│
  │   memory   embedded vector store (numpy cosine, no Milvus)   │
  │   soul     mood engine · diary · proactive · memes           │
  │   console  management console (127.0.0.1:8787)               │
  └──────────────────────────────────────────────────────────────┘
                    ⇅ HTTP (optional voice)
     [Organ 2: GPT-SoVITS process] (127.0.0.1:5000, your own voice)
```

v1 orchestrated AstrBot as the brain with Milvus/etcd/minio as the memory backend. **v2 replaces that stack entirely with the independent implementation shown above**; the old five-container layout is preserved in the `legacy` profile of `docker-compose.yml` for the 14-day rollback window only (`docker compose --profile legacy up -d`).

Everything runs on your own machine. Chat data, memories and API keys stay in the local `local/` directory — never in git, never on any server of ours. (Chat content is sent to whichever LLM / embedding provider *you* configure — that's inherent to using cloud APIs; point it at a local model if that matters to you.)

> This is a condensed English overview. The full documentation (setup details, tuning notes, FAQ) is in Chinese — see [README.md](README.md).

## Original modules (v1 as AstrBot plugins, merged into the v2 core)

Beyond orchestration, this repo ships three original, unit-tested modules developed for this project. In v1 they lived in `plugins/` as AstrBot plugins; v2 merges them into `virtuallover/` (`soul/mood.py`, `soul/diary.py`, and the `/eval` command surface) — the v1 plugin tree and its tests were retired and deleted once v2 acceptance landed, see git history; per-module provenance is documented in [docs/provenance.md](docs/provenance.md).

- **vl_mood_engine** — an event-driven affection & mood state machine: affection/mood evolve with your conversations (sweet words warm her up, rude words cool her down, long silence makes her miss you) and get injected into the LLM system prompt to steer her tone. Pure-logic layer is framework-free and fully unit-tested.
- **vl_diary** — she writes a first-person diary of your day, nightly, and sends it to you.
- **vl_eval** — `/eval memory` benchmarks the long-term memory with recall@1/@3 reports (RAG quality you can quote).

## Five hardening rounds (post-v2)

Beyond the three ported originals, five rounds of hardening added their own modules (all unit-tested, per-module provenance in [docs/provenance.md](docs/provenance.md)):

- **Local brain, one click** — the Console's "引擎与人格" page switches her to a local model served by llama.cpp on your own GPU (any GGUF model; tested with Qwen2.5-7B) (`scripts/brain_switch.py`: GGUF conversion on first run, chat probe, config switch with cloud backup, vl restart); `scripts/service.py` orchestrates napcat + local brain + vl + voice as one start/stop unit (`一键启动/停止/状态.bat`) with dual watchdogs (vl 10s patrol auto-restart; llama-server 8002 30s probe, death confirmation, circuit breaker).
- **Smart degradation routing** (`brain/router.py`) — when the cloud key fails non-retriably (401/403, e.g. arrears), she goes sticky-local after two consecutive rounds instead of going mute for hours; transient 5xx/timeouts get per-request fallback only. Recovery is detected by a 30-minute half-open probe that *notifies* — it never silently switches you back (billing is a human decision). `/brain [status|local|cloud]` in chat, and a console brain panel, keep it observable.
- **Console grew real management teeth** — live config editing (chat/embedding/persona, keys never echoed back, timestamped backup + atomic write, explicit "needs restart"), a recent-events panel (in-process log ring buffer + log-tail fallback, so an arrears 403 can no longer hide behind an all-green status page), and memory search over the vector store.
- **Soul quality gate** (`soul/textclean.py` + proactive quality gate) — regex fragments, emoji garbage and leaked function-call debris are stripped before anything is sent or spoken; memory writes get near-duplicate detection and a persona anchor so she stops mixing up who is who.

## What this is (and isn't)

This is not another deployment tutorial. The repo ships three things tutorials can't:

1. **A battle-tested, sanitized config from months of real usage** ([cmd_config.example.json](deploy/cmd_config.example.json)) — rate limiting against QQ risk-control, human-like segmented replies with randomized 1.5–3.5s delays, a "only my owner can talk to me" whitelist strategy, fail-safe voice wiring.
2. **Cross-project wiring knowledge** (deposited during the v1 era; it is exactly the evidence base the v2 migration was built from) — persisting NapCat config against anonymous-volume loss, container health gating, the correct way for containers to reach a host-side TTS server.
3. **Safety rails** — personal data structurally isolated in `local/`, pre-commit gates (secret scan + import hygiene + upstream probe sentences), ports bound to localhost by default.

## Architecture (v2)

| Component | Access | Role |
|---|---|---|
| virtuallover core | host process (`python -m virtuallover`, `.venv`-isolated) | Brain: OneBot bridge, persona assembly, LLM clients + smart-degradation routing, embedded memory, mood/diary/proactive/memes |
| Console | [127.0.0.1:8787](http://127.0.0.1:8787) | Single management entry: after login you can edit config (chat/embedding/persona), scan the QQ QR code, start/stop services, and back up — plus recent events, diary, memory search and eval reports at a glance |
| NapCat | 127.0.0.1:61990 (managed by the Console — no need to open; neutral port, host 61990 → container 6099) | Organ 1: QQ protocol endpoint |
| Local brain (optional) | host, 127.0.0.1:8002 (Console "引擎与人格" page; `scripts/service.py` auto-starts it when configured) | Any GGUF model via llama.cpp (tested: Qwen2.5-7B) on llama.cpp — offline fallback brain; smart degradation switches to it when the cloud key fails non-retriably, `/brain` in chat to inspect/switch |
| GPT-SoVITS (optional) | host :5000 (auto-started by the core on first utterance) | Organ 2: voice synthesis |
| legacy group (AstrBot + Milvus/etcd/minio) | rollback mode only | The v1 five-container layout kept in the compose `legacy` profile for the 14-day rollback window |

One host process + one container by default, minimal port exposure (localhost-only), log rotation — plus the management console embedded in the host process (`virtuallover.console`, 127.0.0.1:8787, same lifecycle as the core). The optional local brain (llama-server, bound to 127.0.0.1:8002) and the voice engine live on the host too; `一键启动.bat` brings the whole family up and watches it.

## Quick start (Windows, three steps)

1. Requirements: Windows 10/11 x64 + [Docker Desktop](https://www.docker.com/products/docker-desktop/), a **burner QQ account** for the bot (risk control is real — never use your main account), and one LLM API key — pick a provider during install (Volcengine Ark Doubao / DeepSeek / any OpenAI-compatible) and the wizard writes the endpoint & model preset for you. Long-term memory additionally wants an embedding model (optional: without one she still chats, she just doesn't remember; an Ark key works as-is, other providers follow the prompt).
2. Double-click `setup.bat` — a two-minute interactive wizard: paste the key, pick the provider, enter the bot's QQ number and (optionally) your own QQ — she only replies to whitelisted owners; press Enter to skip and the burner doubles as the owner (embedding is asked separately; skipping it just turns memory off). The wizard then automates everything else: Docker check, napcat container up, `.venv` + dependencies, config generation & write-back, **automatic napcat reverse-WS wiring** (no WebUI hand-editing), and finally brings her up and waits for the Console to be ready.
3. Open the Console at [http://127.0.0.1:8787](http://127.0.0.1:8787) (password in `local/console.json`), scan the QR code on the "QQ Login" page **with the burner account** — key, persona and embedding can all be reviewed/edited later on the Console config page. Then add the burner as a friend from your main account and say hi — she's alive.

Daily use: `一键启动.bat` / `一键停止.bat` / `一键状态.bat` — one click starts/stops/checks the whole family (napcat + local brain if configured + vl core + voice; `scripts/service.py` orchestration with dual watchdogs: vl crash auto-restart, llama-server 8002 probe/confirm/breaker). `scripts/backup.bat` (backup — run before upgrades), `scripts/update.bat` (safe upgrade: backup → git pull → refresh deps → restart → verify, with rollback hints), `scripts/check-secrets.bat` (pre-publish secret scan). The Console at `http://127.0.0.1:8787` is the single management entry. Want her to speak offline on your own GPU? Use the Console's "引擎与人格" page (Qwen2.5-7B via llama.cpp; the same page switches back to the cloud brain). Auto-start on login is a one-line scheduled task — see the Chinese README. Voice training/integration: see [docs/语音.md](docs/语音.md) (Chinese); the default persona ("小语") can be replaced via the Console's "引擎与人格" page — template at [persona-template.txt](deploy/persona-template.txt). To hand-place a config before tweaking: copy [vl.toml.example](deploy/config-samples/vl.toml.example) to `local/config/vl.toml` (kept key-for-key in sync with the config tree, see `virtuallover/tests/test_config_example.py`).

## Misuse clause

This project is provided **for personal study and single-user companionship only**. Using it for romance scams, impersonating a real human, soliciting money transfers, mass-operating accounts (account farming), or any other unlawful or harmful purpose is strictly forbidden — users bear full legal responsibility for their own use. The "never claims to be an AI" setting in the persona templates exists solely for the immersive companion experience; it does **not** constitute authorization to conceal the AI's identity from any third party.

## License notes

MIT for this repo's code, scripts and docs. **Provenance statement (v1 vs v2)**: v1 orchestrated AstrBot (referencing its official image only, without modifying or redistributing its code); v2 is an **independent clean-room reimplementation** — every line is written from the behavioral spec plus black-box observation at runtime, with no source copied from AstrBot or its AGPL plugins. Per-module provenance: [docs/provenance.md](docs/provenance.md). Upstream components keep their own licenses — notably **NapCat uses a custom restrictive license that forbids commercial use**.
