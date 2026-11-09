# 收拢 · 会前洞察工作台

一个无外部 CDN、无前端框架、无构建步骤的中文单页业务原型。HTML/CSS/原生 JavaScript 由现有 FastAPI **同源**托管；问答与反馈始终请求真实后端，失败时明确报错，不使用静态内容冒充 API 回答。

## 文件

- `index.html`：语义化单页结构、SVG 图标、工作台与会前一页纸。
- `styles.css`：响应式布局、键盘焦点、加载/空/错误状态与 A4 打印样式。
- `app.js`：真实 API、同 thread 追问、证据定位、规则检查、反馈与 URL 自动执行。
- `insights.json`：由项目内脱敏派生证据整理的**静态会前副本**，只用于会前视图；不是模型结果。

## 运行

从项目根目录启动后端（Python 3.12+，需要 `uv`）：

```bash
cd agent
uv sync --group dev
uv run audience-agent serve
```

打开：

```text
http://127.0.0.1:8000/prototype/
```

FastAPI 将本目录挂载在 `/prototype`，API 使用同源绝对路径：

- `POST /v1/workspaces/demo/insights`
- `POST /v1/workspaces/demo/feedback`

不要直接双击 `index.html`，也不要用另一个静态端口打开页面：这两种方式会破坏同源 API 请求。后端未启动、路由不可用、返回非 JSON 或非 2xx 时，页面会显示错误，不会补造答案。

本地默认 `X-API-Key` 为 `local-query-token`，可在“本地连接设置”中修改。它只保留在当前页面内存中，不写入 `localStorage` / `sessionStorage`。**这是本地 demo 访问令牌，不是生产认证**；公开或生产环境仍需企业 SSO/OIDC、TLS、网关授权、密钥管理等服务端能力。

后端若使用默认 `LLM_MODE=mock`，页面展示的是 mock 后端经真实 HTTP 返回的结果，只能证明端到端线路，**不代表已调用或验证真实生成模型**。页面不会把“真实 API 请求”描述成“真实模型结果”。

## 正常交互流程

1. **选择模板或输入问题**：模板只填充输入框，不自动伪造回答。点击“生成洞察”后发送 `question`。
2. **查看主张与证据**：区分观察、推断、建议及置信度。点击主张的引用 ID，展开并定位对应证据卡；文件、sheet、字段、行号/行标识、数据版本全部取自后端响应。
3. **同 thread 追问**：首轮保存后端 `thread_id`，后续请求携带该 ID。顶部显示轮次；可切回本话题的上一轮结果。点击“新话题”清空当前线程。
4. **反方校验 / 还缺什么**：根据本轮 `claims/evidence/limitations` 做透明的规则检查，明确标为“非二次模型回答”。“带入同 thread 追问”只填充问题，仍需用户点击发送并真实请求后端。
5. **四键反馈**：选择采纳、修改后采纳、驳回或待验证。修改后采纳必须填写修改说明，其余状态可补充说明。反馈绑定当前 `run_id/thread_id/dataset_version`，成功与失败均可见。
6. **会前一页纸**：点击“收拢成会前一页纸”，把当前真实 API 轮次整理为六栏简报；可打印或用浏览器存为 PDF。初次进入时也可读取 `/evidence/insights.json`，不可用时回退到本目录的 `insights.json` 静态快照。两种来源在页面上有明确标识。
7. **加载、取消与重试**：请求中显示非伪造的加载状态，可取消；超时、网络错误、401/403/413/422/429/5xx 会给出具体提示。失败后保留问题供修改或重试。

### 反馈契约

当前 FastAPI `FeedbackRequest` 使用：

```json
{
  "run_id": "后端返回的 UUID",
  "thread_id": "后端返回的线程 ID",
  "dataset_version": "后端返回的数据版本或 null",
  "decision": "accepted | edited | rejected | needs_validation",
  "notes": "可选修改或补充说明"
}
```

界面中的四种“状态”映射到后端 `decision`，修改说明映射到 `notes`。不会额外发送当前模型声明禁止的 `status/note` 字段。

## 用于真实运行截图的 URL 自动执行

所有参数只调用正常表单与 API 流程，不注入响应、不绕过加载/错误状态。自动执行结果也会暴露在：

- `document.body.dataset.autorun`：`running` / `done` / `error`
- `window.__prototypeAutomation`：包含状态、场景、真实 `runId/threadId/rounds`（失败时包含错误）

无头浏览器应等待 `data-autorun="done"` 后截图；若为 `error`，应保留错误截图并检查后端，而不是改用假数据。

### 1. 自动提问一次

```text
http://127.0.0.1:8000/prototype/?autorun=1&question=品牌A的GenZ人群占比是多少？能否构成转化漏斗？
```

未提供 `question` 时使用页面定义的 Gen Z 业务问题；仍只发一次真实 API 请求。

### 2. 完整流程：首问 → 同 thread 追问 → 打开证据

```text
http://127.0.0.1:8000/prototype/?scenario=full&question=品牌A的GenZ人群占比是多少？能否构成转化漏斗？
```

页面顺序等待两次真实请求完成，第二次携带首轮 `thread_id`，随后展开第一张证据卡。可通过 `followup=...` 指定追问，未提供时使用内置追问文本。若后端拒答或无证据，则显示实际结果，不补造证据卡。

### 3. 真实提问后提交反馈

```text
http://127.0.0.1:8000/prototype/?scenario=feedback&decision=accepted&question=品牌A的GenZ人群占比是多少？
```

或：

```text
http://127.0.0.1:8000/prototype/?scenario=feedback&decision=needs_validation&note=需业务负责人补核样本口径
```

`scenario=feedback` 仅允许 `accepted` / `needs_validation` 两种自动反馈，其他值回退为 `accepted`。自动反馈在真实洞察成功返回后才提交，并展示服务端成功或失败结果。`note` 可选，默认说明仅用于标识会前流程自动截图。

## 可访问性与业务采用设计

- 语义化区域、表单标签、跳转主内容链接、aria-live 状态播报、键盘可操作的 tabs 与 details。
- 清晰的 `:focus-visible`，适配窄屏与 `prefers-reduced-motion`。
- 动态 API 文本通过 `textContent` 写入，不使用 `innerHTML` 注入响应。
- 针对“不愿维护表格”：无需新建或日常维护业务表；会前只走提问、核证据、做反馈三步。
- 针对“会前信息散落”：主张—证据—边界—动作—反馈收拢为一页，支持打印。
- 针对“答案看起来都对”：反方检查暴露推断、低置信、来源缺口与外推边界，并可转成同线程真实追问。

## 验证

若 `node` 在 PATH 中：

```bash
node --check prototype/app.js
python3 -m json.tool prototype/insights.json >/dev/null
```

建议再做真实链路冒烟：

```bash
curl -sS http://127.0.0.1:8000/readyz
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/prototype/
```

最后分别打开上述三种自动执行 URL，确认 `data-autorun="done"`；`scenario=full` 应有两轮历史，`scenario=feedback` 应显示反馈成功标记。默认 mock 模式的通过只代表本地端到端链路通过，不是生产或真实模型质量验收。
