# 安全边界与残余风险

## 已实现的防护

- Workspace-scoped service keys 分离 `insight:read`、`feedback:write`、`evidence:write`；所有证据读取都使用认证后的 workspace 和 active dataset version。Grant 配置和每次 workspace path 均拒绝手机号、邮箱、身份证等直接标识符，URL 不能绕过。
- Ingest 只写 staged；active/retired 在 API、memory 锁、Postgres 事务和数据库 trigger 四层不可变，冲突返回 409，不泄露证据正文。
- SQL 参数化；模型不能写 SQL，且没有工具、网络、shell 或文件访问。证据正文在 prompt 中明确标记为不可信数据。
- 引用必须属于当前 workspace/version 的检索集；无效引用、PII 主张和把 M5 观点写成事实的主张均被删除，零有效主张即拒答。
- PII 首道 guard 覆盖 evidence 的 `id/workspace_id/dataset_version/source_material/content/source_file/source_sheet/source_field/source_row/metadata`，以及 question、thread、feedback notes/dataset。422 不回显 offending value。
- `limitations` 和 `next_steps` 去 PII、去重并限制长度；引用安全、PII、M5、跨域提示优先于模型文本保留，不会因容量压力或 6 条模型 limitations 丢失关键边界或触发 502。
- M5 记录类型由 contract 强制为 opinion/hypothesis；跨域 verify 再阻止其成为 observation。M2/M3/M4 的单域指标附窗口/粒度/非总体限制。
- 原始导入只读取用户指定目录；M4 只输出聚合值，不输出 ORD/USER 等记录级键；原声保留原文但不复制原始文件。Manifest 用 SHA256 防来源漂移。
- 请求体、问题、证据条数/长度、模型输出、attempt/fallback、并发和总 deadline 均有硬上限；SDK 隐式 retry 关闭。
- 日志不记录 question、证据或 feedback 正文；Postgres DSN、token 和供应商异常详情不返回客户端。
- LangGraph 使用严格 serializer；反馈先验证真实 run/thread/dataset，防止伪造审计关联。
- `/prototype` 与 `/evidence` 只挂载指定静态目录；meeting snapshot 路由不会误返回详细分析 schema。

## Regex 和模型不能解决的事项

- 当前 PII 检查是保守的第一道防线，不是完整 DLP、匿名化或法律合规。生产导入需要上游分类/tokenization、字段 allowlist、恶意文件扫描和人工复核。高精度数值型统计不会按字符串误判为身份证，但文本中的其他准标识符仍可能漏检。
- Citation allowlist 只证明 ID 被检索过，不证明模型语义忠实；需要真实标注集和人工抽样。
- Prompt injection 仍可能影响措辞；本应用无特权工具可降低影响，但业务决策前仍需证据审阅。
- MemorySaver 与内存 store 只用于本地/test；其锁不提供多进程一致性。生产一致性依赖 Postgres 事务/trigger 与正确的数据库权限。
- DB trigger 不替代最小数据库权限：运行角色不应拥有绕过约束、truncate 或改 trigger 的权限。
- Readiness 的 model adapter 状态不证明外部模型可用、质量合格或其数据政策获批。
- 原始材料已脱敏，但原声仍可能含敏感业务信息；交付和日志访问需按内部授权控制，不能反推真实企业、账号或用户身份。

## 上线前必须完成

1. 用 OIDC/JWT 网关和可信 workspace claims 替换 API key map；secret manager、轮换、TLS、分布式 rate limit 和 egress allowlist。
2. 数据库 migration/runtime 角色分离，at-rest/in-transit 加密、PITR、restore drill、审计留存与删除流程；禁止运行角色修改 trigger/约束。
3. 真实 Postgres 上验证并发 ingest/activate、checkpoint 恢复、跨 tenant 隔离和多副本；当前单元测试不冒充实库验收。
4. 审查模型供应方区域、保留、训练使用和合法依据；`store=False` 不等于法律批准。模型 ID/fallback 定期审计。
5. 真实模型语义引用忠实度、prompt injection、成本/负载和跨域人群误导评测通过后才能开放。
