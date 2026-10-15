# -*- coding: utf-8 -*-
"""virtuallover.memory —— 长期记忆子系统（SPEC §7）。

子模块分工：
- store      内嵌向量库：schema 全字段、库元数据 fail-fast、numpy 余弦检索、
             /remember 手动 API（§7.1 / §7.3）；
- service    写入触发：消息计数达 num_pairs*2 触发总结入库、失败持久化待重试、
             时间阈值兜底可配默认禁用（§7.2）；
- backfill   历史回填：data_v4.db 对话分窗 → LLM 第一人称总结 → 入库，幂等可重跑（§7.4）；
- benchmark  记忆召回评测纯逻辑（vl_eval port-of-own 并入）：/eval memory 命令
             经 __main__._build_eval_runner 以本模块 + 进程内向量库装配（§8.5）。

嵌入/总结调用统一经 brain.llm 客户端：本包内所有 LLM 能力都以可注入回调实现，
由 __main__ 装配期经 service.chat_fn_from_client / embedder_from_client 接线。
"""
