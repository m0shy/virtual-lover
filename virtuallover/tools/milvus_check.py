# -*- coding: utf-8 -*-
"""Milvus 空库证明（SPEC §11.3）—— 删除 milvus/etcd/minio 三容器的前置存档。

十参谋实测旧库 0 行数据；本脚本连接 Milvus → list_collections + 各集合行数 →
JSON 落 backup/milvus_check_<时间戳>.json，作为"无数据可迁"的书面证据。

已存档证据（O10，2026-10-11）：backup/milvus_check_20261011205548.json
（1 个集合 default，总行数 0）——三件套随后停机，日常无需再跑本脚本。

override 取证流程（容器已停且需重新取证时）：Milvus 停机后本脚本连不上，
此时**不要**把三件套加回默认组，用 profile override 临时拉起再取证：

    docker compose --profile legacy up -d etcd minio milvus   # 只起三件套
    python -m virtuallover.tools.milvus_check                 # 存档新的 JSON 证明
    docker stop -t 60 milvus-standalone milvus-etcd milvus-minio   # 优雅停回
    #（勿用整组 up：那会把 astrbot 一起拉起来；三件套数据卷在 local/milvus/
    #  volumes/ 原位不动，停回后与取证前状态一致）

注意：取证必须对"停机前的原库"执行——若 local/milvus/volumes/ 已被清空，
再拉起只会得到新的空库，证明不再具有"旧库无数据"的取证效力。pymilvus 非
框架运行依赖（SPEC §1），仅本一次性脚本临时使用（pip install pymilvus）。
"""

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    host = os.environ.get("MILVUS_HOST", "127.0.0.1")   # 容器映射 127.0.0.1:19530
    port = os.environ.get("MILVUS_PORT", "19530")
    out_dir = Path(os.environ.get("VL_BACKUP_DIR", "backup"))
    try:
        from pymilvus import MilvusClient
    except ImportError:
        print("说明：本脚本需临时安装 pymilvus（一次性工具，非框架依赖）：pip install pymilvus")
        return 2
    try:
        client = MilvusClient(uri=f"http://{host}:{port}", timeout=5)
        names = client.list_collections()
        rows = [{"collection": n, "num_entities": client.get_collection_stats(n).get("row_count")} for n in names]
    except Exception as exc:  # noqa: BLE001 —— 容器停/不可达：输出说明即可，不硬连
        print(f"说明：Milvus ({host}:{port}) 不可达（{exc.__class__.__name__}）——空库证明需在容器存活时执行；"
              "若三容器已删除且此前已存档 JSON，本检查不再必要。")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"milvus_check_{time.strftime('%Y%m%d%H%M%S')}.json"
    out.write_text(json.dumps({"uri": f"http://{host}:{port}", "collections": rows,
                               "total_entities": sum(r["num_entities"] or 0 for r in rows)},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"空库证明已存档：{out}（集合 {len(rows)} 个，总行数 {sum(r['num_entities'] or 0 for r in rows)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
