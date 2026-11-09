# 运维、安全与发布手册

## 本地运行与原始重建

```bash
cd fde_optimized_delivery/agent
cp .env.example .env
export UV_CACHE_DIR=/path/to/uv-cache
uv sync --locked --group dev

# 可选：从原始包重建；交付启动不依赖原始包仍存在。
uv run audience-raw-import /path/to/M1-M6 --output-dir data
uv run audience-agent serve
curl -fsS http://127.0.0.1:8000/readyz
```

默认 `data/evidence.raw.json` 已可直接启动。导入失败会非零退出，不应拿旧 JSON 冒充本次导入结果。重建后检查 `raw_manifest.json` 的输入文件数、SHA256、真实行号、窗口和记录数，再运行原始集成测试。

## 配置规则

- 真实环境禁止 `LLM_MODE=mock`、`STORAGE_BACKEND=memory`；生产强制显式 `DATABASE_URL`、Postgres、OpenAI adapter 和 workspace key。
- `SEED_EVIDENCE_PATH=data/evidence.raw.json`。旧 `evidence.seed.json` 仅保留向后兼容，不是默认业务输入。
- `OPENAI_MODEL` 与 `OPENAI_MODEL_FALLBACKS` 不写死在代码中；仅配置经批准的 ID，定期审计可用性和数据政策。
- `REQUEST_TIMEOUT_SECONDS=30` 是 graph + semaphore + 全部模型调用的总预算；`LLM_ATTEMPT_TIMEOUT_SECONDS=20` 是单次上限且不能超过总预算。
- `LLM_MAX_ATTEMPTS=2` 是包含 primary/fallback 的总调用上限；`LLM_MAX_FALLBACKS=1` 是备用模型数上限，必须小于总 attempts。SDK `max_retries=0`，不要重新开启隐藏重试。
- 所有 deadline 使用 event-loop 时钟；更换 ASGI loop（asyncio/uvloop）后必须跑真实 ASGI 回归，不能用 `time.monotonic()` 代替 `loop.time()`。
- `API_KEYS_JSON` 使用 workspace/role scoped token；密钥由 Secret Manager 注入，不进入 Git、镜像、CI artifact 或浏览器 JS。

## 数据集发布与回滚

1. 原始导入生成内容哈希版本，或通过 evidence API 写新 staged version。每批 1–200 条，不能混合 workspace/version。
2. 核对 manifest、schema、单位、粒度、去重、PII、时间窗和 eval。Active/retired ingest 返回 409 是预期保护，不要通过直接 SQL 绕过。
3. `uv run audience-agent activate WORKSPACE VERSION` 原子激活 staged；旧 active 自动 retired。Retired 不能重新激活或覆写。
4. 回滚需要把历史内容作为**新 staged snapshot** 导入、验证、激活，而不是修改/重激活 retired。这样保留每次回答的 dataset lineage。
5. `seed` 对同内容哈希 active 版本幂等；若 active 内容不同会拒绝，必须重新生成内容哈希版本。

Postgres migration 顺序为 `001_initial.sql`、`002_evidence_integrity.sql`。后者新增 cross_domain/opinion/hypothesis、insight_runs 和不可变 trigger。上线前先在备份/恢复环境验证迁移，运行角色不得拥有绕过 trigger 的权限。

## 反馈、checkpoint 与健康检查

- 只有成功完成的 insight 才写入 `insight_runs`。Feedback 保持 `decision/notes`，run/thread/dataset 任一不匹配返回统一 422 且不写记录。
- `/healthz` 只证明进程存活；`/readyz` 检查 evidence store、独立 checkpoint DB 与模型 adapter 配置。任何依赖异常或 health probe 返回 False 均为 503，不泄露 DSN 或供应商错误。
- Model ready 不调用外部模型，也不证明真实生成质量。需要单独受控 smoke/eval，不得把 readiness 当模型验收。
- LangGraph checkpoint 可能保存问题与主张；定义保留期、删除请求、workspace 清理与恢复流程。业务反馈和证据表不依赖 checkpoint 作为 source of truth。

## 验证命令

```bash
uv sync --locked --group dev
uv run pytest -q
uv run ruff check src tests evals
uv run python evals/run_eval.py
uv run audience-agent openapi

# 有原始包时必须额外跑：
RAW_DATA_ROOT=/path/to/M1-M6 uv run pytest -q
uv run audience-raw-import /path/to/M1-M6 --output-dir data
```

测试数量只是下限；上线应把真实失败扩展到 20–50 条以上并做 citation precision/coverage、语义忠实度、abstention、跨 tenant、prompt injection、并发和恢复门禁。

## 故障处置

- **409 ingest conflict：** 版本已 active/retired。创建新版本，不要重试同一写入或手动改状态。
- **422 feedback mismatch：** 使用响应原始 run_id/thread_id/dataset_version；不要伪造历史关联。
- **504 insight deadline：** 检查 semaphore 等待、模型延迟和 fallback 配置；不要盲目增大预算，先记录 run_id、elapsed 和 adapter 指标。
- **502 generation failed：** 未返回未校验答案。按 run_id 查结构化日志中的错误类型，不回传供应商正文。
- **503 readiness：** 分别检查 evidence/checkpoint DB 与 adapter 配置；恢复后再接流量。
- **原始导入失败：** 检查 87 文件完整性、sheet/字段/真实单元格与权限。失败时不覆盖为“已验证”状态。

生产还需 OIDC/JWT、TLS ingress、IP/用户限流、egress allowlist、加密/PITR/恢复演练、审计与模型数据政策审批。Docker/Compose、真实 CI、真实 OpenAI 和 Postgres 多副本必须单独验收，本地 memory/mock 通过不能替代。
