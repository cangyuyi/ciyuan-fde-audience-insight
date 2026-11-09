# 技术参考（第一方 / 官方项目）

**核验日期：2026-11-04。** 以下来源只用于架构与库接口，不作为人群/市场证据。

| 来源 | 本项目用途 | 参考 URL |
|---|---|---|
| LangGraph GitHub + release notes | 状态图/持久化编排，版本基线 `langgraph==1.2.19` | https://github.com/langchain-ai/langgraph / https://github.com/langchain-ai/langgraph/releases |
| LangGraph Postgres checkpointer | Postgres durable checkpoint，版本基线 `langgraph-checkpoint-postgres==3.2.0` | https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres |
| LangGraph security advisory | Msgpack pickle deserialization hardening; this app uses strict msgpack and stores built-in state only | https://github.com/langchain-ai/langgraph/security/advisories/GHSA-4jq9-2xhw-jmxm |
| LangGraph durable execution docs | retry/idempotency/checkpoint semantics | https://docs.langchain.com/oss/python/langgraph/durable-execution |
| LangGraph interrupt docs | 未来需审批/人工修订时的 resume 模式；本版本不暂停无害只读答复 | https://docs.langchain.com/oss/python/langgraph/interrupts |
| FastAPI GitHub | ASGI API / dependency / validation，版本基线 `fastapi==0.162.0` | https://github.com/fastapi/fastapi / https://fastapi.tiangolo.com/ |
| OpenAI Python SDK | async Responses API + Pydantic structured parse，版本基线 `openai==3.45.0` | https://github.com/openai/openai-python |
| PostgreSQL `pg_trgm` | 词面相似检索和 GIN/GiST 索引；文档标为 contrib，容器显式安装 `postgresql-contrib` | https://www.postgresql.org/docs/18/pgtrgm.html / https://www.postgresql.org/docs/18/contrib.html |
| pgvector GitHub | 未来若中文语义召回 eval 证明必要，再考虑向量/混合检索；当前未引入 | https://github.com/pgvector/pgvector |
| OWASP GenAI Security Project | prompt injection 风险分类；引导模型将证据文本视为不可信数据 | https://genai.owasp.org/llmrisk/llm01-prompt-injection/ |
| Astral uv Docker guide | Dockerfile 中以锁文件同步依赖；构建时版本基线 `uv 0.12.21` | https://docs.astral.sh/uv/guides/integration/docker/ / https://github.com/astral-sh/uv/releases/tag/0.12.21 |

## 框架选择比较
- **LangGraph**：选用。显式 graph/state/checkpoint，适合证据检索→生成→校验的确定性流程并可恢复。
- **OpenAI Agents SDK**：值得用于 OpenAI-native tool/handoff 路线；本需求没有自主工具循环，且要持久线程/复核边界，因此把 OpenAI 留在 model adapter 而非编排层。
- **Google ADK / Microsoft Agent Framework / CrewAI**：当前客户栈未知，且无需多 agent；增加 provider/框架特异性没有足够收益。
- **直接 FastAPI + 单 LLM 调用**：若不需要持久线程/反馈恢复，这是更简单的替代；保留为轻量退化方案。

所有版本以 `pyproject.toml` 和生成的 `uv.lock` 为交付准据；升级前跑 `pytest`、`ruff` 和 eval 集，不以 README 中历史版本为准。
