# -*- coding: utf-8 -*-
"""virtuallover.console —— 管理控制台 v2（SPEC §10，workbench v2 整体移植）。

来源与删改声明（docs/provenance.md 口径）：本包由 workbench/ 整体移植
（**port-of-own**），改动仅三处：
1. **删除 AstrBot 代理段**：engine.py 的 ab_* 全部（登录/JWT 缓存/透传/探针）
   与 console.json 的 astrbot_password 托管一并移除；NapCat QR 代理（nc_*）
   原样保留；
2. **/api/status 改进程内探测**：不再探测六个上游容器，改为查询本进程各模块
   （桥/LLM/embedding/历史库/记忆库/情绪/日记/主动消息 + 语音后端 HTTP 探针）；
3. **新增 napcat 指向切换/回退动作**：/api/control/action 增 napcat_switch /
   napcat_rollback，改写 onebot11_*.json（调 tools/config_migrate 的公共改写
   函数，动态发现文件名）并联动 vl.toml bridge.token、docker compose restart heart。

三重门原样保留：会话认证 + X-Requested-With CSRF + 127.0.0.1 绑定。
"""

from virtuallover.console.app import setup as setup  # noqa: F401 —— 暴露给 __main__._SETUP_TARGETS 装配
