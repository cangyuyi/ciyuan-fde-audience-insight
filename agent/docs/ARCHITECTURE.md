# 架构决策记录：Evidence-first Audience Insight Agent

**日期：2026-11-05**  
**状态：** 可本地验证的生产化骨架；真实模型、Postgres 实库与企业上线边界仍需外部验收。

## 目标与成功判据

系统要么返回每个主张都绑定到同 workspace、同 active snapshot 的来源记录，要么明确 abstain；不能“听起来合理但无法回源”。硬判据：

- 返回主张的 citation IDs 100% 属于本轮检索集，否则删除主张/整体拒答。
- SQL 全部参数化并限制 workspace；模型没有数据库、网络、shell 或文件工具。
- active/retired snapshot 不可变；反馈必须绑定真实 run/thread/dataset。
- M2 非嵌套、M1 极小消耗、M3 采样窗口、M4 订单粒度/右删失与 M5 观点等级均有运行时证据和回归。
- 总请求预算覆盖排队、图、全部模型尝试；超限受控取消，不返回未校验答案。

## 组件与数据流

```text
M1–M6 XLSX/DOCX (read-only, user-supplied root)
  └─ audience-raw-import
      ├─ evidence.raw.json (evidence + metrics + content-hashed version)
      └─ raw_manifest.json (SHA256 + sheet/field/row + cleaning + windows)

Client / prototype / meeting snapshot
  └─ TLS ingress + OIDC/JWT (production boundary)
      └─ FastAPI (workspace auth, schema/size limits, unified errors, deadline)
          └─ LangGraph durable graph
              ├─ route: live_roi / audience_profile / experience / fulfillment / cross_domain
              ├─ retrieve: workspace + active version + domain/material diversity
              ├─ synthesize: bounded structured-output model call, no tools
              └─ verify: citation allowlist + PII + M5 opinion gate + abstention
                  ├─ PostgreSQL evidence + immutable versions + runs + feedback
                  └─ independent LangGraph checkpoint DB
```

**One agent, no open-ended tool loop。** 业务流程可预先枚举；生成阶段只做受控结构化调用。Postgres 是业务 source of truth，checkpoint 不替代证据、run 或 feedback 表。

## 原始导入与可复现性

`raw_ingest.py` 直接读取全部 86 个 XLSX 和 1 个 DOCX，不从旧 seed 复制数值。逐文件 SHA256 的排序列表生成 `m1-m6-raw-<hash>` 数据集版本，因此内容变化必然产生新 snapshot。Manifest 记录输入根目录/相对路径、哈希、sheet、字段、真实行号、清洗规则、时间窗和证据记录数；原始文件不复制到交付包。

关键确定性规则：

- M1 以真实直播开始日期关联自然日投放，消耗门槛 ≥100 元只作演示筛选，低额原始行保留为 caveat；ROI 用总 GMV/总消耗。
- M2 三组各 24 场，按日期+起止时间对齐；23 个自然日，06-19 两场。Gen Z 取 `B5`，18–30 岁取 `B16+B17`，只做场次等权画像比较。
- M3 月报占比统一百分比格式；`汇总!L13` 等原声逐字保留。M3-02 标签统计覆盖全部 100 源行（含 1 条空原声），非空 quote 为 99 条。
- M4 三批商品行按主订单去重，支付分母要求支付完成时间非空。补发合并“线上订单号/原始线上订单号”关联；标签按支付主订单去重且非互斥。
- M5 所有来源记录强制 `opinion/hypothesis + low confidence`，不能成为 fact；M6 保留真实 `(1).xlsx` 文件名和口径警示。

## 数据集、事务与反馈

- `workspace_datasets`：每 workspace 最多一个 active。Ingest 在同一事务内 advisory lock → `SELECT ... FOR UPDATE` → 检查 staged → 写 batch/evidence → 完成 batch；任一步失败全部回滚。
- `evidence_records`：复合主键 `(workspace_id,dataset_version,id)`；DB trigger 对 insert/update/delete 再检查 staged。UPDATE 同时校验 OLD 与 NEW workspace/version，active/retired 记录不能被迁移到 staged 键来绕过不可变性，也不能被删除或污染。生命周期 trigger 防止 retired 重新激活。
- `insight_runs`：仅成功生成响应后记录 workspace、内部 thread、run UUID 与 dataset version。
- `insight_feedback`：保持 `decision/notes`；保存前精确比对 run/thread/dataset，失败不写审计记录。
- Memory 实现用同一把 async lock 保护 ingest/activate/run/feedback，提供可测试的等价语义；不是生产存储。

## 检索与跨域约束

单域路由沿用确定性关键词。出现“跨域/跨材料/人群洞察/人群假设/综合证据”、两个以上域的“联合/结合”表达、三个以上域信号或“场景+任务”时，优先进入 `cross_domain`，避免被某个单域词（如“履约”）抢先命中。检索 `audience_profile + experience + fulfillment + cross_domain`；合并算法优先保证 M2/M3/M4/M5 材料多样性，再填充剩余槽位，不把单一域检索结果冒充综合人群结论。

“反例/替代解释/证据不足/验证动作”等元追问在同 thread 中继承 checkpoint 的上一 intent；没有可用上一状态时才回退到 `cross_domain`。因此单域追问仍保持单域，跨域追问继续返回多域证据，不因省略业务关键词而丢失 dataset。

M5 若被模型写成 observation，或缺少“观点/假设/待验证”标记，verify 节点会丢弃该主张。引用安全、PII、M5 和跨域 limitation 优先于模型文本保留；即使四类问题同时发生，也会在 6 条容量内保留全部强制边界。citation ID 合法仍不等于语义忠实，真实模型上线前必须做人工抽查和标注 eval。

## Deadline、并发与 readiness

API 使用 `asyncio.get_running_loop().time()` 计算总 deadline；模型层也使用相同 loop 时钟，避免 uvloop 下与 `time.monotonic()` 原点不同造成立即超时。

```text
request deadline
  ├─ graph routing / retrieval / checkpoint
  ├─ semaphore queue
  └─ at most LLM_MAX_ATTEMPTS total calls
      └─ at most LLM_MAX_FALLBACKS alternate model IDs
          └─ each call <= LLM_ATTEMPT_TIMEOUT_SECONDS and remaining total budget
```

SDK `max_retries=0`，防止应用 attempts 与 SDK retry 相乘。`asyncio.timeout_at` 取消底层 coroutine；API 将总超时映射为 504，其他生成错误 fail closed 为 502。

Readiness 分别探测 evidence DB、独立 checkpoint DB 和模型 adapter 配置状态。模型 adapter ready 不表示已调用供应商，也不做昂贵/有副作用的外部模型健康请求。

## 静态交付与 OpenAPI

API routes 注册后再挂载 `/prototype` 与 `/evidence`，因此 `/docs`、`/openapi.json`、`/v1/*` 优先。`/evidence/insights.json` 在 meeting snapshot 存在时显式返回轻量 `meeting_snapshot.json`，避免把详细分析 schema 错给会前视图。静态 OpenAPI 由实际 app 生成，统一 422 `location/message/type`。

## 技术取舍

| 选择 | 代价 | 重新评估条件 |
|---|---|---|
| LangGraph 单图 | checkpoint 运维成本 | 若无需持久线程，可退化为 FastAPI + 单次调用 |
| Postgres + pg_trgm/受控字段 | 中文深层语义召回有限 | 标注检索 eval 不达标时评估 pgvector/hybrid |
| 内容哈希 snapshot | 原始包变化产生全新版本 | 大文件场景可增加分块/内容寻址存储 |
| 确定性跨域材料优先 | 可解释但非学习式排序 | 用真实问题集评估召回和材料覆盖率 |
| mock 本地模式 | 不证明真实生成质量 | 配置批准模型后运行语义忠实度 eval |

不在本版本做：多 Agent、模型写 SQL、自动确定 Persona、自动调投放/改商品、未授权抓取、把访谈意见自动升级为业务规则。
