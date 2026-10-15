# -*- coding: utf-8 -*-
"""brain 包：LLM 客户端、人格装配与会话存储（SPEC §4/§5/§6）。

- llm.py      OpenAI 兼容 chat + embedding 双客户端；
- persona.py  system/user 装配管道 + 记忆/时间注入 + truncate 上下文管理；
- history.py  SQLite 会话存储 + mirror_v4.json 双写镜像；
- sender.py   分段回复与发送速率限制（SPEC §5 发送侧行为）。

本包零上游框架依赖（纯标准库 + numpy），可在宿主机直接 pytest。
"""
