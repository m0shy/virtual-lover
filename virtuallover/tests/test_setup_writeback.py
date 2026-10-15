# -*- coding: utf-8 -*-
"""setup.bat v2 交互收集值写回测试（O12 §7.1-7 ①）。

核心断言：Key 同写 chat+embedding 两侧且不回显；QQ 号三处（session_id 尾段/
admins/bridge.self_id）同步替换；非法 QQ 拒绝且不落盘；往返 load 校验通过。
五轮修缮 R1 扩展：服务商预设写回（含 Key 前缀 ark- 自动猜测）、embedding
单独问法（skip=记忆暂闭写 enabled=false）、napcat 反向 WS 闭环（全新装代写
onebot11_<QQ>.json，残留文件改写指向并备份）。

安全规约：全部占位值；Key 断言只查等值存在性（测试内自比），不打印。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from virtuallover.tools.config_migrate import DEFAULT_NAPCAT_URL
from virtuallover.tools.init_config import build_fresh_config, run_init
from virtuallover.tools.setup_writeback import ensure_napcat_reverse_ws, writeback
from virtuallover.config import load_config

_TEST_KEY = "sk-TEST-WRITEBACK-KEY-000"  # 编造占位 Key
_TEST_QQ = "123456789"                    # 编造占位 QQ（非真实号段）
_ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
_ARK_MODEL = "doubao-seed-1-6-flash-250828"


@pytest.fixture()
def fresh_root(tmp_path):
    """跑一遍 init 的全新运行根。"""
    assert run_init(tmp_path) == 0
    return tmp_path


def test_writeback_writes_key_and_qq_three_places(fresh_root):
    vl_toml = fresh_root / "local" / "config" / "vl.toml"
    code = writeback(fresh_root, api_key=_TEST_KEY, qq=_TEST_QQ)
    assert code == 0
    cfg = load_config(vl_toml)
    assert cfg.chat.api_key == _TEST_KEY
    assert cfg.embedding.api_key == _TEST_KEY
    # session_id 保持 <人格名>:FriendMessage:<QQ> 形态，尾段换成新 QQ
    assert cfg.session_id.endswith(f":FriendMessage:{_TEST_QQ}")
    assert ":" in cfg.session_id and cfg.session_id.startswith(cfg.persona.name)
    assert cfg.admins == [_TEST_QQ]
    assert cfg.bridge.self_id == _TEST_QQ


def test_writeback_empty_args_leaves_config_alone(fresh_root):
    vl_toml = fresh_root / "local" / "config" / "vl.toml"
    before = vl_toml.read_text(encoding="utf-8")
    assert writeback(fresh_root) == 0
    assert vl_toml.read_text(encoding="utf-8") == before


def test_writeback_rejects_invalid_qq_without_touching_file(fresh_root):
    vl_toml = fresh_root / "local" / "config" / "vl.toml"
    before = vl_toml.read_text(encoding="utf-8")
    code = writeback(fresh_root, api_key=_TEST_KEY, qq="12a45")
    assert code == 1
    assert vl_toml.read_text(encoding="utf-8") == before  # 失败不落盘


def test_writeback_missing_config_reports(tmp_path):
    assert writeback(tmp_path, api_key=_TEST_KEY) == 1
    assert not (tmp_path / "local" / "config" / "vl.toml").exists()


def test_writeback_result_passes_load_roundtrip(fresh_root):
    """写回产物 dump→load 往返幂等（--smoke 就绪的前提）。"""
    from virtuallover.config import dump_config
    vl_toml = fresh_root / "local" / "config" / "vl.toml"
    assert writeback(fresh_root, api_key=_TEST_KEY, qq=_TEST_QQ) == 0
    cfg = load_config(vl_toml)
    assert dump_config(cfg) == vl_toml.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 服务商预设（五轮修缮 R1：选择优先，Key 前缀 ark- 兜底猜测）
# ---------------------------------------------------------------------------

def test_writeback_provider_ark_preset(fresh_root):
    assert writeback(fresh_root, provider="ark") == 0
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert cfg.chat.api_base == _ARK_BASE
    assert cfg.chat.model == _ARK_MODEL


def test_writeback_provider_deepseek_preset(fresh_root):
    assert writeback(fresh_root, provider="deepseek") == 0
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert cfg.chat.api_base == "https://api.deepseek.com"
    assert cfg.chat.model == "deepseek-chat"


def test_writeback_custom_ark_prefix_guesses_preset(fresh_root):
    """选了"其他"但 Key 以 ark- 开头 → 自动猜回方舟预设（api_base/model 全替换）。"""
    assert writeback(fresh_root, api_key="ark-TEST-PREFIX-000",
                     provider="custom") == 0
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert cfg.chat.api_base == _ARK_BASE
    assert cfg.chat.model == _ARK_MODEL
    assert cfg.chat.api_key == "ark-TEST-PREFIX-000"


def test_writeback_custom_base_and_model_written(fresh_root):
    base, model = "https://api.test.example/v1", "test-chat-model"
    assert writeback(fresh_root, provider="custom",
                     api_base=base, model=model) == 0
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert cfg.chat.api_base == base
    assert cfg.chat.model == model


def test_writeback_custom_without_inputs_keeps_init_defaults(fresh_root, capsys):
    """选"其他"但没给 api_base/model：不致命，保持 init 模板默认。"""
    assert writeback(fresh_root, provider="custom") == 0
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert cfg.chat.api_base == _ARK_BASE  # init_config 的默认形态
    captured = capsys.readouterr()
    assert "api_base" in captured.err + captured.out


# ---------------------------------------------------------------------------
# embedding 单独问法（skip = 记忆暂闭，写 enabled=false 降级不报错）
# ---------------------------------------------------------------------------

def test_writeback_embedding_skip_disables_memory(fresh_root):
    vl_toml = fresh_root / "local" / "config" / "vl.toml"
    assert writeback(fresh_root, embedding="skip") == 0
    cfg = load_config(vl_toml)  # enabled=false 时三个 embedding 必填豁免，能加载
    assert cfg.embedding.enabled is False
    assert cfg.embedding.api_base == ""
    assert cfg.embedding.api_key == ""
    assert cfg.embedding.model == ""
    # chat 侧不受影响（可独立启动）：模型保持 init 模板默认（占位符，与
    # vl.toml.example 同源；写回 preset 时才会换成具体型号）
    assert cfg.chat.model == build_fresh_config().chat.model


def test_writeback_embedding_ark_ep_model_sets_multimodal_style(fresh_root):
    assert writeback(fresh_root, api_key=_TEST_KEY,
                     embedding="ark", embed_model="ep-2024-TEST") == 0
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert cfg.embedding.enabled is True
    assert cfg.embedding.api_base == _ARK_BASE
    assert cfg.embedding.model == "ep-2024-TEST"
    assert cfg.embedding.style == "multimodal_single"  # 多模态嵌入端点同款启发式（R6 中性化）
    assert cfg.embedding.api_key == _TEST_KEY  # 缺省沿用 chat Key


def test_writeback_embedding_ark_with_separate_key(fresh_root):
    ark_key = "ark_TEST_EMB_FAKE_000"
    assert writeback(fresh_root, api_key=_TEST_KEY, provider="deepseek",
                     embedding="ark", embed_model="doubao-embedding",
                     embed_key=ark_key) == 0
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert cfg.chat.api_base == "https://api.deepseek.com"
    assert cfg.embedding.api_key == ark_key  # 单独提供的 embedding Key 优先
    assert cfg.embedding.style == "openai"  # 非 ep- 直连模型走标准端点


def test_writeback_embedding_custom_with_dim(fresh_root):
    base = "https://emb.test.example/v1"
    assert writeback(fresh_root, embedding="custom", embed_base=base,
                     embed_model="test-emb", embed_dim="1536") == 0
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert cfg.embedding.api_base == base
    assert cfg.embedding.model == "test-emb"
    assert cfg.embedding.dim == 1536


def test_writeback_embedding_ark_without_model_aborts_untouched(fresh_root):
    vl_toml = fresh_root / "local" / "config" / "vl.toml"
    before = vl_toml.read_text(encoding="utf-8")
    assert writeback(fresh_root, embedding="ark") == 1
    assert vl_toml.read_text(encoding="utf-8") == before  # 失败不落盘


# ---------------------------------------------------------------------------
# napcat 反向 WS 闭环（全新装代写 onebot11_<QQ>.json；残留文件改写指向）
# ---------------------------------------------------------------------------

def _napcat_dir(root: Path) -> Path:
    return root / "local" / "napcat_config"


def test_napcat_reverse_ws_created_when_missing(fresh_root):
    assert not list(_napcat_dir(fresh_root).glob("onebot11_*.json"))
    assert writeback(fresh_root, api_key=_TEST_KEY, qq=_TEST_QQ) == 0
    target = _napcat_dir(fresh_root) / f"onebot11_{_TEST_QQ}.json"
    assert target.is_file()
    data = json.loads(target.read_text(encoding="utf-8-sig"))
    client = data["network"]["websocketClients"][0]
    assert client["enable"] is True
    assert client["url"] == DEFAULT_NAPCAT_URL
    assert client["messagePostFormat"] == "array"
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert client["token"] == cfg.bridge.token  # 与 vl 侧桥 token 一字不差


def test_napcat_reverse_ws_rewrite_existing_with_backup(fresh_root):
    napcat_dir = _napcat_dir(fresh_root)
    napcat_dir.mkdir(parents=True, exist_ok=True)
    stale = napcat_dir / "onebot11_10000.json"  # init 占位 QQ 的残留指向
    stale.write_text(json.dumps({
        "network": {"websocketClients": [
            {"enable": True, "name": "旧栈", "url": "ws://old-stack:6199/ws",
             "token": "short-legacy-token"}]}}, ensure_ascii=False),
        encoding="utf-8")
    assert writeback(fresh_root, api_key=_TEST_KEY, qq=_TEST_QQ) == 0
    data = json.loads(stale.read_text(encoding="utf-8-sig"))
    client = data["network"]["websocketClients"][0]
    assert client["url"] == DEFAULT_NAPCAT_URL
    cfg = load_config(fresh_root / "local" / "config" / "vl.toml")
    assert client["token"] == cfg.bridge.token
    assert list(napcat_dir.glob("onebot11_10000.json.bak-*"))  # 写前备份


def test_ensure_napcat_reverse_ws_is_idempotent_on_rebuild(tmp_path):
    """残留文件第二次跑：url 已是目标值仍再写一次（幂等值 + 新备份），不报错。"""
    napcat_dir = _napcat_dir(tmp_path)
    note1 = ensure_napcat_reverse_ws(tmp_path, _TEST_QQ, "T" * 43, name="测试")
    assert "onebot11_123456789.json" in note1
    note2 = ensure_napcat_reverse_ws(tmp_path, _TEST_QQ, "T" * 43, name="测试")
    assert "已改写" in note2  # 第二次走 switch_napcat_target 改写分支
