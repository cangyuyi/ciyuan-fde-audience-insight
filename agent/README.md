# Audience Insight Agent — evidence-first backend

可测试的人群洞察后端：直接导入 M1–M6 原始 XLSX/DOCX，按 workspace 与不可变数据集版本检索证据，经 LangGraph 编排、结构化模型输出和确定性引用校验后返回洞察；证据不足时拒答。模型不能生成 SQL，也没有任意网络、文件或系统工具。

> 本交付仍是生产化骨架，不宣称已完成生产验收。本地默认 `memory + MemorySaver + mock`，不会调用真实 OpenAI。真实模型质量、真实 Postgres 事务/恢复、多副本与企业身份链路仍需在获批环境验证。

## 已实现的关键保证

- **不可变快照：** 新 ingest 只能写 staged；active/retired 版本在 memory 锁和 Postgres 事务内均拒绝写入，API 返回 **409**。Postgres trigger 同时阻止绕过应用直接修改 active/retired 证据。只能显式激活 staged，旧 active 变 retired。
- **输出安全：** 所有 evidence 字符串字段、metadata、question、feedback 均做直接标识符检查；limitations/next_steps 去 PII、去重并限制条数，6 条模型 limitations 也不会触发 502。
- **总 deadline：** 同一个 `asyncio` loop 时钟覆盖 graph、semaphore 排队、模型 attempts/fallbacks 与 checkpoint 调用；SDK 隐式 retry 为 0，总尝试与 fallback 有硬上限，超时返回 **504** 并取消底层调用。
- **反馈 lineage：** 保持 `decision / notes` 契约；run/thread/dataset 必须匹配成功完成的 insight run，否则 422。所有 422 使用 `location / message / type` 数组，OpenAPI 与运行时一致。
- **readiness：** 检查 evidence store、独立 checkpoint DB 与模型 adapter 配置状态；不会为 readiness 调用外部模型。
- **跨域证据：** `cross_domain` 组合 M2/M3/M4/M5，保留各自窗口、粒度和限制；M5 只能是 `opinion/hypothesis`，不能把单域指标冒充整体人群或漾 YOUNG 结论。

## 原始数据导入（独立、可复现）

交付不要求复制原始 M1–M6。已生成的 `data/evidence.raw.json` 可直接启动 demo；需要重建时，将原始根目录通过命令行传入：

```bash
cd fde_optimized_delivery/agent
export UV_CACHE_DIR=/path/to/uv-cache
uv sync --locked --group dev
uv run audience-raw-import /path/to/M1-M6 --output-dir data
# 等价入口：uv run audience-agent raw-import /path/to/M1-M6 --output-dir data
```

导入器直接读取 86 份 XLSX 和 1 份 DOCX，输出：

- `data/evidence.raw.json`：内容哈希确定的数据集版本、可检索证据、复算指标、来源/窗口。
- `data/raw_manifest.json`：输入根目录和相对路径、逐文件 SHA256、sheet/字段/真实行号、清洗规则、时间窗和记录数。

内置规则包括 M1 加权 ROI 与低额残余、M2 **24 场/23 日期**及 18–30 岁 `B16+B17`、M3 `汇总!L13` 全文和 100 行标签分布、M4 支付主订单去重与两类补发父订单键、M5 观点等级、M4/M6 真实文件名。原始文件不可修改；重复导入同一目录生成字节一致的 JSON。

## 本地 demo（无密钥、无 Docker）

```bash
cd fde_optimized_delivery/agent
cp .env.example .env
uv sync --locked --group dev
uv run audience-agent serve
```

- 文档：`http://127.0.0.1:8000/docs`
- 健康：`GET /healthz`；依赖就绪：`GET /readyz`
- 本地请求头：`X-API-Key: local-query-token`，workspace `demo`
- 默认读取 `SEED_EVIDENCE_PATH=data/evidence.raw.json`，不是旧手工派生 seed。
- 若交付根存在 `prototype/`，挂载到 `/prototype`；`evidence/` 挂载到 `/evidence`。
- 若存在 `evidence/meeting_snapshot.json`，`/evidence/insights.json` 明确返回该轻量会前快照，不返回详细分析 schema。
- 静态挂载不会覆盖 `/docs`、`/openapi.json` 或 `/v1/*`。

示例：

```bash
curl -X POST http://127.0.0.1:8000/v1/workspaces/demo/insights \
  -H 'X-API-Key: local-query-token' -H 'Content-Type: application/json' \
  -d '{"question":"请综合M2/M3/M4/M5形成年轻人群洞察，说明场景、任务和验证动作。"}'
```

## API

- `POST /v1/workspaces/{workspace_id}/insights`：返回带 citations 的洞察或 abstain；总超时 504。
- `POST /v1/workspaces/{workspace_id}/feedback`：`accepted / edited / rejected / needs_validation`，字段仍为 `decision / notes`；必须显式携带返回的 `dataset_version`（未知主题可为 null）。
- `POST /v1/workspaces/{workspace_id}/evidence`：每批 1–200 条，只写 staged；active/retired 返回 409。
- `uv run audience-agent activate WORKSPACE DATASET_VERSION`：审校后激活新版本。
- `uv run audience-agent openapi`：从实际 FastAPI app 重建 `docs/openapi.json`。

## 测试与 eval

```bash
uv run pytest -q
uv run ruff check src tests evals
uv run python evals/run_eval.py

# 原始包可用时，取消跳过并执行真实导入/复算/哈希/逐字引文集成测试：
RAW_DATA_ROOT=/path/to/M1-M6 uv run pytest -q
```

测试覆盖 memory/Postgres 事务语义、409、PII 全字段、limitations 4/5/6 边界、总 deadline/uvloop ASGI、attempt/fallback 上限、feedback 关联、checkpoint readiness、静态挂载、cross_domain、原始复算/来源/窗口与可复现输出。无 `RAW_DATA_ROOT` 时，仅原始包集成用例跳过，其余测试仍独立运行。

## Postgres 与模型配置

执行 `migrate` 会依次应用初始 DDL 和 `002_evidence_integrity.sql`。`seed` 对相同内容哈希 active 版本幂等；内容变化会产生新版本，不能覆写旧快照。

```bash
uv run audience-agent migrate
uv run audience-agent seed        # 仅 demo；生产禁止题包样例
uv run audience-agent serve
```

生产要求 `STORAGE_BACKEND=postgres`、`LLM_MODE=openai`、显式 DSN 与模型配置。模型 ID 只从 `OPENAI_MODEL` 读取，fallback 由 `OPENAI_MODEL_FALLBACKS` 配置并需定期审计，不能硬编码进业务逻辑。上线前还需 OIDC/JWT、TLS、secret manager、备份/恢复、数据留存、模型数据政策与真实 eval 审批。

详见 `docs/ARCHITECTURE.md`、`docs/SECURITY.md`、`docs/OPERATIONS.md` 和实际生成的 `docs/openapi.json`。
