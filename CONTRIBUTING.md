# 贡献指南

欢迎 issue 与 PR。这个项目是个人作品，审查节奏可能不快，但每个认真的提交都会被认真对待。

## 开发环境

```bash
git clone <repo>
pip install -r requirements.txt -r requirements-dev.txt
```

## 运行测试

```bash
python -m pytest virtuallover/tests -q
```

## 提交前检查

1. 测试全绿；
2. 不引入任何密钥、真实聊天数据（`local/` 与 `.env` 永不入库，`.githooks/pre-commit` 会拦截）；
3. 新模块请在 docs/provenance.md 登记来源标注（own / port-of-own / clean-room-of-X）。

## PR 流程

小而聚焦的 PR 优于大而全的重构；描述里写清"做了什么、为什么"。默认分支 main。
