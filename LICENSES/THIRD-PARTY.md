# Third-Party Components

This project incorporates the following third-party components. Each section lists the component name, license, and how it is used.

## NapCat (QQ Protocol Adapter)

- **License**: Custom Restricted License (non-open-source, non-commercial)
- **Usage**: Runs as a Docker container using the official unmodified image `docker.io/mlikiowa/napcat-docker:latest`. Provides QQ messaging protocol connectivity via OneBot v11 reverse WebSocket.
- **Modification**: None. The image is referenced as-is without any modification or redistribution.
- **Source**: https://github.com/NapNeko/NapCatQQ
- **Warning**: QQ is a trademark of Tencent. Use of unofficial protocol implementations carries risk of account restriction. This project is for personal, non-commercial use only.

## GPT-SoVITS / GSVI (Voice Engine)

- **License**: MIT License
- **Usage**: Voice synthesis engine running as a local process. The inference server (`tts_backend.py`) is started and managed by this project's lifecycle manager. Model weights and reference audio are user-provided.
- **Modification**: Directory renamed for integration. No source code modified.
- **Source**: https://github.com/RVC-Boss/GPT-SoVITS
- **License file**: Preserved in the voice engine directory.

## llama.cpp (Local LLM Inference)

- **License**: MIT License
- **Usage**: Local inference server for the backup/fallback brain (any GGUF model; tested with Qwen 2.5 7B). Runs `llama-server` with GGUF quantized models.
- **Modification**: Binary renamed for integration. No source code modified.
- **Source**: https://github.com/ggml-org/llama.cpp

## Qwen 2.5 7B Instruct (tested local-brain model)

- **License**: Apache License 2.0
- **Usage**: Local fallback brain model, quantized to Q4_K_M GGUF format.
- **Modification**: File renamed locally. GGUF metadata (architecture, tokenizer, license attribution) preserved unmodified.
- **Source**: https://huggingface.co/Qwen/Qwen2.5-7B-Instruct
- **Note**: Apache 2.0 requires license attribution preservation upon redistribution. This project does not redistribute the model; users obtain it independently.

## AstrBot (Historical Reference)

- **License**: GNU Affero General Public License v3.0
- **Usage**: This project's v1 architecture used AstrBot as the core framework. The v2 rewrite (`virtuallover/`) is a clean-room independent reimplementation with zero source code copied. AstrBot was used as a behavioral reference only.
- **Verification**: The `docs/provenance.md` file documents the clean-room implementation chain. A pre-commit hook scans for any AstrBot source code imports (which would indicate copying).
- **Source**: https://github.com/Soulter/AstrBot

## Volcano Ark / Doubao (Cloud Brain - Optional)

- **Service**: Volcano Engine Ark API (commercial service, no code dependency)
- **Usage**: Optional cloud-based LLM provider. Used via standard OpenAI-compatible HTTP API with user-provided API keys.
- **Note**: This is a commercial API service, not open-source software. No code from this service is included in this project.

---

*This file is provided in compliance with the applicable license terms of the above components. For the full text of each license, refer to the respective project repositories or the license files preserved within vendored directories.*
