# 词元引擎 FDE 实战题｜王泽栋 V2.0

这是一套“报告＋一页纸＋业务架构＋原始数据链路＋可运行原型＋真实运行佐证”的完整交付。所有业务数据均来自题目提供的脱敏虚构 M1–M6；未采集外部人群、评论、竞品或行业信号。

## 1. 先看哪些文件

1. `deliverables/01_词元引擎FDE_人群洞察分析报告_王泽栋.pdf`：主报告，含 3 条品牌A证据链与 1 条 YOUNG 待验证假设。
2. `deliverables/02_词元引擎FDE_六栏一页纸_王泽栋.pdf`：快速筛选用六栏一页纸。
3. `deliverables/03_词元引擎FDE_产品方案架构图.png` 与同名 HTML：业务、数据治理、人机责任与降级架构。
4. `deliverables/04_词元引擎FDE_采用与风险说明_王泽栋.pdf`：操作步骤、耗时、持续使用、三个月失败假设与风险。
5. `evidence/prototype_run.json` 与 `evidence/screenshots/`：真实 API 流程和 Chrome 截图。
6. `deliverables/06_终面15分钟讲解.md`：终面讲解顺序与追问速答。

对应 DOCX 一并保留，便于修改。`evidence/insights.json` 保存完整结构化洞察、计算规则和 M2 的 72 个真实文件名。

## 2. 本地运行原型

要求：Python 3.12+、uv；默认无需模型密钥、Docker 或 Postgres。

```bash
cd agent
uv sync --locked --group dev
uv run audience-agent serve
```

打开：

- 工作台：`http://127.0.0.1:8000/prototype/`
- API 文档：`http://127.0.0.1:8000/docs`
- readiness：`http://127.0.0.1:8000/readyz`

本地 workspace 为 `demo`，页面内置的本地演示令牌为 `local-query-token`。它只是本地 demo 约定，不是生产认证。

默认 `LLM_MODE=mock`：真实执行 HTTP、检索、引用校验、同线程追问和反馈写入，但生成器是确定性 mock，**不证明真实模型质量**。`STORAGE_BACKEND=memory` 也不证明生产持久化。

## 3. 原始数据复现

回传包不包含 30MB 原始材料副本，避免重复分发。将 `RAW_ROOT` 指向原始 `M1-M6` 根目录后运行：

```bash
cd agent
uv run python -m audience_agent.raw_ingest "/absolute/path/to/M1-M6" --output-dir data
```

生成：

- `data/evidence.raw.json`：证据正文、材料、真实文件、sheet、字段、行号、时间窗、证据等级与限制。
- `data/raw_manifest.json`：87 个输入文件的 SHA256、解析范围、清洗规则、血缘位置和记录数。

导入器直接读取 XLSX/DOCX，不复制原始用户标识；M5 仅生成 opinion/hypothesis，不生成事实记录。数据版本由输入内容确定，当前版本见 manifest。

## 4. 原型真实流程复现

服务启动后，在交付根目录运行：

```bash
python scripts/run_prototype_flow.py --output evidence/prototype_run.json
python scripts/capture_prototype.py --output-dir evidence/screenshots
```

流程包括：跨域首问→同 thread 反方追问→证据卡校验→`needs_validation` 反馈。截图由本机 Chrome 访问真实 FastAPI 页面生成；没有注入模拟响应。

也可直接打开：

- `/prototype/?autorun=1`：真实首问。
- `/prototype/?scenario=full`：首问→同 thread 追问→展开证据。
- `/prototype/?scenario=feedback&decision=needs_validation`：真实提问后提交反馈。

## 5. 测试与边界

```bash
cd agent
uv run pytest
uv run ruff check src tests evals
uv run python evals/run_eval.py
```

最终命令输出与退出码保存在 `qa/`。真实 OpenAI、Postgres、Docker/Compose、企业 OIDC/TLS、备份恢复与多副本不在本次本地验证范围内；上线门槛保留在后端文档中。

## 6. 业务使用边界

- M2 三组非嵌套，不计算曝光→互动→成交漏斗，不用单月证明长期趋势。
- M3 人工月报与人工样本不用于估算总体问题率，7 月样本不冒充 6 月。
- M4 商品行不等于订单；问题标签非互斥，关联不等于责任或因果，当前窗口存在右删失。
- M5 是局部专家观点，不是企业共识或客观市场事实。
- 品牌A只提供 YOUNG 的研究线索；Y1 是低置信度、可证伪、待验证假设。
- 本次未采集外部人群信号，不以 AI 常识或技术文档冒充市场证据。

## 7. 保密

题目材料仅用于招聘评估。请勿外传、转载或用于商业用途；公开部署前必须重新完成身份、权限、数据政策、加密、留存和删除评审。
