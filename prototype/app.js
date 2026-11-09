(() => {
  "use strict";

  const INSIGHTS_ENDPOINT = "/v1/workspaces/demo/insights";
  const FEEDBACK_ENDPOINT = "/v1/workspaces/demo/feedback";
  const REQUEST_TIMEOUT_MS = 90_000;
  const FEEDBACK_TIMEOUT_MS = 30_000;
  const DECISIONS = {
    accepted: { label: "采纳", saved: "已提交采纳反馈" },
    edited: { label: "修改后采纳", saved: "已提交修改说明" },
    rejected: { label: "驳回", saved: "已提交驳回反馈" },
    needs_validation: { label: "待验证", saved: "已提交待验证反馈" },
  };
  const KINDS = {
    observation: "观察",
    inference: "推断",
    recommendation: "建议",
    hypothesis: "待验证假设",
  };
  const CONFIDENCE = { high: "高置信", medium: "中置信", low: "低置信" };
  const INTENTS = {
    live_roi: "直播投放",
    audience_profile: "人群画像",
    experience: "产品体验",
    fulfillment: "订单履约",
  };

  const state = {
    runs: [],
    currentRunId: null,
    threadId: null,
    loading: false,
    lastQuestion: "",
    requestController: null,
    feedbackController: null,
    selectedDecision: null,
    activeView: "workspace",
    briefData: null,
    briefSource: "static",
    automationRunning: false,
  };

  const byId = (id) => document.getElementById(id);
  const els = {
    apiStatus: byId("api-status"),
    apiStatusText: byId("api-status-text"),
    form: byId("question-form"),
    question: byId("question-input"),
    questionError: byId("question-error"),
    characterCount: byId("character-count"),
    askButton: byId("ask-button"),
    askButtonLabel: byId("ask-button-label"),
    apiKey: byId("api-key-input"),
    newThread: byId("new-thread-button"),
    threadContext: byId("thread-context"),
    threadIdLabel: byId("thread-id-label"),
    threadRoundLabel: byId("thread-round-label"),
    loading: byId("loading-state"),
    loadingMessage: byId("loading-message"),
    error: byId("error-state"),
    errorMessage: byId("error-message"),
    errorDetail: byId("error-detail"),
    empty: byId("empty-state"),
    results: byId("results"),
    announcer: byId("request-announcer"),
    toastRegion: byId("toast-region"),
    runHistory: byId("run-history"),
    currentQuestion: byId("current-question"),
    answerStatus: byId("answer-status"),
    answerRound: byId("answer-round"),
    claims: byId("claims-container"),
    evidence: byId("evidence-container"),
    evidenceCount: byId("evidence-count"),
    counterChecks: byId("counter-checks"),
    missingChecks: byId("missing-checks"),
    followupComposer: byId("followup-composer"),
    limitations: byId("limitations-list"),
    nextSteps: byId("next-steps-list"),
    feedbackForm: byId("feedback-form"),
    feedbackNote: byId("feedback-note"),
    feedbackNoteLabel: byId("feedback-note-label"),
    feedbackError: byId("feedback-error"),
    feedbackSubmit: byId("submit-feedback"),
    feedbackTarget: byId("feedback-target"),
    feedbackSavedBadge: byId("feedback-saved-badge"),
    selectedDecisionLabel: byId("selected-decision-label"),
    syncCurrent: byId("sync-current-button"),
    briefLoading: byId("brief-loading"),
    briefError: byId("brief-error"),
    briefPaper: byId("brief-paper"),
  };

  function createElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function createIcon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.appendChild(use);
    return svg;
  }

  function clear(element) {
    element.replaceChildren();
  }

  function currentRun() {
    return state.runs.find((run) => run.run_id === state.currentRunId) || null;
  }

  function setApiStatus(status, label) {
    els.apiStatus.dataset.state = status;
    els.apiStatusText.textContent = label;
  }

  function announce(message) {
    els.announcer.textContent = "";
    window.setTimeout(() => { els.announcer.textContent = message; }, 30);
  }

  function showToast(message, type = "success") {
    const toast = createElement("div", `toast${type === "error" ? " error" : ""}`);
    toast.appendChild(createIcon(type === "error" ? "alert" : "check"));
    toast.appendChild(createElement("span", "", message));
    els.toastRegion.appendChild(toast);
    window.setTimeout(() => toast.remove(), 4800);
  }

  function setResultState(name) {
    els.loading.hidden = name !== "loading";
    els.error.hidden = name !== "error";
    els.empty.hidden = name !== "empty";
    els.results.hidden = name !== "results";
  }

  function updateCharacterCount() {
    els.characterCount.textContent = `${els.question.value.length} / 1200`;
  }

  function validateQuestion(question) {
    const trimmed = question.trim();
    if (trimmed.length < 3) return "请输入至少 3 个非空字符的业务问题。";
    if (trimmed.length > 1200) return "问题不能超过 1200 个字符。";
    if (!els.apiKey.value.trim()) return "请填写本地 X-API-Key；它不是生产身份认证。";
    return "";
  }

  function showQuestionError(message) {
    els.questionError.textContent = message;
    els.questionError.hidden = !message;
    els.question.setAttribute("aria-invalid", message ? "true" : "false");
  }

  function humanError(status, payload) {
    const detail = payload && payload.detail;
    if (status === 401) return "访问令牌无效或缺失。请展开“本地连接设置”核对 X-API-Key。";
    if (status === 403) return "当前令牌没有 demo 工作空间的所需权限。";
    if (status === 413) return "问题超过服务端限制，请缩短后重试。";
    if (status === 422) {
      if (Array.isArray(detail)) {
        const messages = detail.map((item) => item.message || item.msg).filter(Boolean);
        return `请求未通过校验${messages.length ? `：${messages.join("；")}` : "，请检查问题与线程信息。"}`;
      }
      return "请求未通过校验。请移除直接身份标识，并检查问题长度。";
    }
    if (status === 502) return "后端未能完成生成或引用校验，已按设计拒答。请稍后重试或检查模型与数据服务。";
    if (status === 429) return "请求过于频繁，请稍后再试。";
    if (status >= 500) return "服务端暂时不可用，请检查 FastAPI 日志与就绪状态。";
    if (typeof detail === "string" && detail.length <= 300) return detail;
    return `请求失败（HTTP ${status}），未返回可用洞察。`;
  }

  async function parseResponse(response) {
    const text = await response.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch {
      throw new Error(`服务返回了非 JSON 内容（HTTP ${response.status}）。请确认页面与 FastAPI 同源，且未由静态服务器拦截 API 路径。`);
    }
  }

  async function postJson(url, body, timeoutMs, externalSignal) {
    const controller = new AbortController();
    const abortFromOutside = () => controller.abort(externalSignal.reason);
    if (externalSignal) {
      if (externalSignal.aborted) controller.abort(externalSignal.reason);
      else externalSignal.addEventListener("abort", abortFromOutside, { once: true });
    }
    const timeout = window.setTimeout(() => controller.abort("timeout"), timeoutMs);
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json",
          "X-API-Key": els.apiKey.value.trim(),
        },
        body: JSON.stringify(body),
        credentials: "same-origin",
        signal: controller.signal,
      });
      const payload = await parseResponse(response);
      if (!response.ok) {
        const error = new Error(humanError(response.status, payload));
        error.httpStatus = response.status;
        throw error;
      }
      return payload;
    } catch (error) {
      if (controller.signal.aborted) {
        const abortError = new Error(controller.signal.reason === "timeout" ? "请求超过前端等待时限，已取消。服务端可能仍在处理，请检查后端状态。" : "请求已取消。未使用任何替代答案。", { cause: error });
        abortError.name = "AbortError";
        abortError.reason = controller.signal.reason;
        throw abortError;
      }
      if (error instanceof TypeError) {
        throw new Error("无法连接同源 FastAPI。请确认服务已启动、API 路由存在，并通过 http(s) 访问页面，而不是直接双击 HTML。", { cause: error });
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
      if (externalSignal) externalSignal.removeEventListener("abort", abortFromOutside);
    }
  }

  function requireString(value, field) {
    if (typeof value !== "string" || !value.trim()) throw new Error(`后端响应缺少有效的 ${field} 字段，无法安全展示或提交反馈。`);
    return value;
  }

  function normalizeInsight(payload, question) {
    requireString(payload.run_id, "run_id");
    requireString(payload.thread_id, "thread_id");
    if (!Array.isArray(payload.claims) || !Array.isArray(payload.evidence)) {
      throw new Error("后端响应缺少 claims[] 或 evidence[]，未展示不完整答案。");
    }
    return {
      ...payload,
      status: payload.status === "answered" ? "answered" : "abstained",
      claims: payload.claims,
      evidence: payload.evidence,
      limitations: Array.isArray(payload.limitations) ? payload.limitations : [],
      next_steps: Array.isArray(payload.next_steps) ? payload.next_steps : [],
      question,
      receivedAt: new Date().toISOString(),
      feedback: null,
    };
  }

  async function requestInsight(question) {
    const trimmed = question.trim();
    const validationError = validateQuestion(trimmed);
    if (validationError) {
      showQuestionError(validationError);
      els.question.focus();
      throw new Error(validationError);
    }
    if (state.loading) return null;

    showQuestionError("");
    state.loading = true;
    state.lastQuestion = trimmed;
    state.question = trimmed;
    els.question.value = trimmed;
    updateCharacterCount();
    els.askButton.disabled = true;
    els.askButtonLabel.textContent = "正在生成…";
    setApiStatus("loading", "正在请求 FastAPI");
    setResultState("loading");
    els.loadingMessage.textContent = state.threadId
      ? "正在携带当前 thread_id 追问；检索、生成与引用校验耗时取决于服务端。"
      : "正在创建新对话；检索、生成与引用校验耗时取决于服务端，此处不会填入演示答案。";
    announce("正在请求后端生成洞察，请稍候。取消不会生成替代答案。");
    state.requestController = new AbortController();

    try {
      const body = { question: trimmed };
      if (state.threadId) body.thread_id = state.threadId;
      const payload = await postJson(INSIGHTS_ENDPOINT, body, REQUEST_TIMEOUT_MS, state.requestController.signal);
      const run = normalizeInsight(payload, trimmed);
      const isSameThread = state.threadId === run.thread_id;
      state.threadId = run.thread_id;
      if (!isSameThread) state.runs = [];
      state.runs.push(run);
      state.currentRunId = run.run_id;
      state.selectedDecision = null;
      els.feedbackNote.value = "";
      els.followupComposer.hidden = true;
      setApiStatus("ready", "真实 API 已返回");
      renderRun(run);
      setResultState("results");
      announce(`后端已返回${run.status === "answered" ? "洞察" : "拒答"}，包含 ${run.claims.length} 条主张与 ${run.evidence.length} 张证据卡。`);
      return run;
    } catch (error) {
      if (error.name === "AbortError" && error.reason !== "timeout") {
        setApiStatus(state.runs.length ? "ready" : "idle", state.runs.length ? "已取消，保留上次结果" : "请求已取消");
        setResultState(currentRun() ? "results" : "empty");
        showToast("已取消请求，未生成替代答案");
        announce("请求已取消。");
      } else {
        setApiStatus("error", "API 请求失败");
        els.errorMessage.textContent = error.message || "请求失败，未获得可用洞察。";
        els.errorDetail.textContent = error.httpStatus ? `POST ${INSIGHTS_ENDPOINT} · HTTP ${error.httpStatus}` : `POST ${INSIGHTS_ENDPOINT} · ${error.name || "Error"}`;
        setResultState("error");
        announce(`请求失败：${els.errorMessage.textContent}`);
      }
      throw error;
    } finally {
      state.loading = false;
      state.requestController = null;
      els.askButton.disabled = false;
      els.askButtonLabel.textContent = state.threadId ? "继续追问" : "生成洞察";
      renderThreadContext();
    }
  }

  function renderThreadContext() {
    const hasThread = Boolean(state.threadId);
    els.threadContext.hidden = !hasThread;
    els.newThread.hidden = !hasThread;
    if (!hasThread) return;
    els.threadIdLabel.textContent = state.threadId;
    els.threadIdLabel.title = state.threadId;
    els.threadRoundLabel.textContent = `${state.runs.length} 轮`;
    els.askButtonLabel.textContent = state.loading ? "正在生成…" : "继续追问";
  }

  function renderRunHistory() {
    clear(els.runHistory);
    els.runHistory.hidden = state.runs.length < 2;
    if (state.runs.length < 2) return;
    els.runHistory.appendChild(createElement("span", "run-history-label", "本话题"));
    state.runs.forEach((run, index) => {
      const button = createElement("button", run.run_id === state.currentRunId ? "is-active" : "", `第 ${index + 1} 轮${index === state.runs.length - 1 ? " · 最新" : ""}`);
      button.type = "button";
      button.setAttribute("aria-pressed", run.run_id === state.currentRunId ? "true" : "false");
      button.addEventListener("click", () => {
        state.currentRunId = run.run_id;
        state.selectedDecision = null;
        renderRun(run);
        byId("answer-title").scrollIntoView({ behavior: "smooth", block: "start" });
      });
      els.runHistory.appendChild(button);
    });
  }

  function renderClaim(claim, index, evidenceMap) {
    const kind = KINDS[claim.kind] ? claim.kind : "observation";
    const card = createElement("article", "claim-card");
    card.dataset.kind = kind;
    const header = createElement("div", "claim-header");
    header.appendChild(createElement("span", "claim-kind", KINDS[kind]));
    header.appendChild(createElement("span", "claim-confidence", CONFIDENCE[claim.confidence] || `置信度：${claim.confidence || "未标注"}`));
    header.appendChild(createElement("span", "claim-number", `C${String(index + 1).padStart(2, "0")}`));
    card.appendChild(header);
    card.appendChild(createElement("p", "claim-text", claim.text || "后端未提供主张文本。"));
    const citations = createElement("div", "claim-citations");
    const ids = Array.isArray(claim.evidence_ids) ? claim.evidence_ids : [];
    ids.forEach((id) => {
      const evidenceIndex = evidenceMap.get(id);
      if (evidenceIndex === undefined) {
        citations.appendChild(createElement("span", "missing-citation", `未找到引用 ${id}`));
        return;
      }
      const button = createElement("button", "citation-button", id);
      button.type = "button";
      button.title = "定位到对应证据卡";
      button.prepend(createIcon("link"));
      button.addEventListener("click", () => openEvidence(evidenceIndex, true));
      citations.appendChild(button);
    });
    if (!ids.length) citations.appendChild(createElement("span", "missing-citation", "此主张未返回证据 ID，请在会议中谨慎使用。"));
    card.appendChild(citations);
    return card;
  }

  function sourceRow(row, label, value, wide = false) {
    const wrapper = createElement("div", wide ? "source-wide" : "");
    wrapper.appendChild(createElement("dt", "", label));
    wrapper.appendChild(createElement("dd", "", value || "后端未提供"));
    row.appendChild(wrapper);
  }

  function renderEvidenceCard(evidence, index) {
    const card = createElement("details", "evidence-card");
    card.id = `evidence-card-${index}`;
    card.dataset.evidenceIndex = String(index);
    const summary = createElement("summary");
    summary.appendChild(createElement("span", "evidence-source-material", evidence.source_material || "证据"));
    const summaryCopy = createElement("div", "evidence-summary-copy");
    summaryCopy.appendChild(createElement("strong", "", evidence.id || `证据 ${index + 1}`));
    summaryCopy.appendChild(createElement("span", "", evidence.source_file || "后端未提供文件名"));
    summary.appendChild(summaryCopy);
    summary.appendChild(createElement("span", `confidence-badge ${evidence.confidence || ""}`, CONFIDENCE[evidence.confidence] || evidence.confidence || "未标置信"));
    summary.appendChild(createIcon("chevron"));
    card.appendChild(summary);

    const detail = createElement("div", "evidence-detail");
    detail.appendChild(createElement("blockquote", "evidence-quote", evidence.content || "后端未提供证据正文。"));
    const sources = createElement("dl", "source-grid");
    sourceRow(sources, "源文件 / FILE", evidence.source_file, true);
    sourceRow(sources, "工作表 / SHEET", evidence.source_sheet);
    sourceRow(sources, "行号或行标识 / ROW", evidence.source_row || "未提供（需回源补核）");
    sourceRow(sources, "字段 / FIELD", evidence.source_field, true);
    detail.appendChild(sources);
    detail.appendChild(createElement("div", "evidence-version", `dataset_version: ${evidence.dataset_version || "未提供"}`));
    card.appendChild(detail);
    return card;
  }

  function openEvidence(index, scroll) {
    const card = byId(`evidence-card-${index}`);
    if (!card) return;
    card.open = true;
    document.querySelectorAll(".evidence-card.is-highlighted").forEach((item) => item.classList.remove("is-highlighted"));
    card.classList.add("is-highlighted");
    if (scroll) card.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => card.classList.remove("is-highlighted"), 2200);
  }

  function deriveChecks(run) {
    const counter = [];
    const missing = [];
    run.claims.forEach((claim, index) => {
      if (["inference", "recommendation", "hypothesis"].includes(claim.kind)) {
        counter.push({
          text: `第 ${index + 1} 条是${KINDS[claim.kind]}，不是原始事实。是否存在同样符合证据的其他解释？`,
          detail: `C${String(index + 1).padStart(2, "0")} · ${claim.kind}`,
          question: `请针对上一轮第 ${index + 1} 条${KINDS[claim.kind]}进行反方校验：还有哪些替代解释？哪些新增证据能推翻它？请继续引用证据，不要把推断说成事实。`,
        });
      }
      if (claim.confidence !== "high") {
        missing.push({
          text: `第 ${index + 1} 条主张为${CONFIDENCE[claim.confidence] || "未标置信"}，不宜直接变成预算或责任决策。`,
          detail: "需要业务 owner 判断证据是否足够",
          question: `上一轮第 ${index + 1} 条主张的置信度不是 high。请指出它最关键的验证缺口，以及应补充什么材料或切片。`,
        });
      }
    });
    const weakEvidence = run.evidence.filter((item) => item.confidence !== "high");
    if (weakEvidence.length) {
      counter.push({
        text: `${weakEvidence.length} 张证据卡不是高置信。人工归纳、抽样原声与平台事实是否被混用了？`,
        detail: weakEvidence.map((item) => item.id).filter(Boolean).slice(0, 3).join(" / "),
      });
    }
    counter.push({
      text: "如果结论被反过来使用，会不会把相关关系误读成因果，或把单月样本外推成长期趋势？",
      detail: "会议通用反方问题 · 仍需人工判断",
      question: "请基于上一轮证据列出最重要的口径边界：哪些结论不能用于因果归因、长期趋势或跨品牌外推？",
    });
    if (!run.evidence.length) {
      missing.push({ text: "本轮没有证据卡，不能形成可追溯会议结论。", detail: "先缩小问题或检查数据接入", question: "上一轮没有可用证据。请明确缺少哪个数据域、时间窗或字段，并给出最小的补数清单。" });
    }
    const incompleteSources = run.evidence.filter((item) => !item.source_file || !item.source_sheet || !item.source_field || !item.source_row);
    if (incompleteSources.length) {
      missing.push({ text: `${incompleteSources.length} 张证据卡的文件、sheet、字段或行标识不完整。`, detail: "回源定位前，不把卡片当作已完成复核", question: "请指出上一轮证据卡中缺失的文件、sheet、字段或行号信息；如果原数据没有，请明确拒答，不要补造。" });
    }
    if (!run.dataset_version) {
      missing.push({ text: "响应未绑定 dataset_version，本轮不可作为可复现数据快照。", detail: "反馈仍会按空版本提交" });
    }
    run.limitations.slice(0, 3).forEach((limitation) => {
      missing.push({ text: limitation, detail: "来自后端 limitations · 待会议确认" });
    });
    if (!run.limitations.length) {
      missing.push({ text: "后端未返回 limitations，不代表没有限制；需人工补齐时间窗、样本与分母检查。", detail: "空白不等于已核验" });
    }
    if (run.status === "abstained") {
      counter.unshift({ text: "本轮为拒答，不应从空主张中自行补出会议结论。", detail: "abstained · fail closed" });
    }
    return { counter: counter.slice(0, 5), missing: missing.slice(0, 5) };
  }

  function renderCheckList(container, items, emptyText) {
    clear(container);
    if (!items.length) {
      container.appendChild(createElement("p", "check-empty", emptyText));
      return;
    }
    items.forEach((item) => {
      const wrapper = createElement("div", "check-item");
      wrapper.appendChild(createElement("p", "", item.text));
      if (item.detail) wrapper.appendChild(createElement("small", "", item.detail));
      if (item.question) {
        const button = createElement("button", "", "带入同 thread 追问");
        button.type = "button";
        button.appendChild(createIcon("arrow"));
        button.addEventListener("click", () => prepareFollowup(item.question));
        wrapper.appendChild(button);
      }
      container.appendChild(wrapper);
    });
  }

  function prepareFollowup(question) {
    els.question.value = question;
    updateCharacterCount();
    showQuestionError("");
    els.followupComposer.hidden = false;
    announce("反方追问已放入输入框，尚未发送。确认后点击继续追问。");
  }

  function renderList(element, items, emptyText, ordered = false) {
    clear(element);
    if (!items.length) {
      element.appendChild(createElement("li", "list-empty", emptyText));
      return;
    }
    items.forEach((item) => element.appendChild(createElement("li", "", item)));
  }

  function renderFeedback(run) {
    document.querySelectorAll("[data-decision]").forEach((button) => {
      if (button.tagName !== "BUTTON") return;
      button.setAttribute("aria-pressed", button.dataset.decision === state.selectedDecision ? "true" : "false");
    });
    els.feedbackForm.hidden = !state.selectedDecision;
    els.feedbackError.hidden = true;
    els.feedbackSavedBadge.hidden = !run.feedback;
    if (run.feedback) els.feedbackSavedBadge.textContent = DECISIONS[run.feedback.decision].saved;
    if (state.selectedDecision) {
      const label = DECISIONS[state.selectedDecision].label;
      els.selectedDecisionLabel.textContent = `已选择：${label}`;
      els.feedbackNoteLabel.textContent = state.selectedDecision === "edited" ? "修改说明（必填）" : "补充说明（可选）";
      els.feedbackTarget.textContent = `run_id: ${run.run_id}`;
      els.feedbackSubmit.textContent = `提交“${label}”反馈`;
    }
  }

  function renderRun(run) {
    renderThreadContext();
    renderRunHistory();
    const runIndex = state.runs.findIndex((item) => item.run_id === run.run_id);
    els.currentQuestion.textContent = run.question;
    els.answerStatus.textContent = run.status === "answered" ? "已回答" : "明确拒答";
    els.answerStatus.className = `status-pill ${run.status}`;
    els.answerRound.textContent = `第 ${runIndex + 1} 轮`;
    byId("meta-intent").textContent = INTENTS[run.intent] || run.intent || "未提供";
    byId("meta-dataset").textContent = run.dataset_version || "未绑定";
    byId("meta-run-id").textContent = run.run_id;
    byId("meta-run-id").title = run.run_id;

    clear(els.claims);
    if (!run.claims.length) {
      els.claims.appendChild(createElement("p", "no-claims", "后端没有返回可验证主张。请查看已知边界与下一步，不要把拒答改写成确定结论。"));
    } else {
      const evidenceMap = new Map(run.evidence.map((item, index) => [item.id, index]));
      run.claims.forEach((claim, index) => els.claims.appendChild(renderClaim(claim, index, evidenceMap)));
    }

    clear(els.evidence);
    els.evidenceCount.textContent = `${run.evidence.length} 张`;
    if (!run.evidence.length) {
      els.evidence.appendChild(createElement("p", "no-claims", "本轮没有证据卡。系统不会用静态快照填补 API 回答。"));
    } else {
      run.evidence.forEach((evidence, index) => els.evidence.appendChild(renderEvidenceCard(evidence, index)));
    }
    const checks = deriveChecks(run);
    renderCheckList(els.counterChecks, checks.counter, "没有触发的反方检查；仍需人工复核。 ");
    renderCheckList(els.missingChecks, checks.missing, "后端未标出额外缺口；这不等于材料已完整。 ");
    renderList(els.limitations, run.limitations, "后端未返回限制说明；请人工补核样本、时间窗与分母。 ");
    renderList(els.nextSteps, run.next_steps, "后端未返回下一步；请先指定业务 owner 与验证动作。 ", true);
    state.selectedDecision = null;
    renderFeedback(run);
    els.syncCurrent.disabled = false;
  }

  function selectDecision(decision) {
    if (!DECISIONS[decision] || !currentRun()) return;
    state.selectedDecision = decision;
    renderFeedback(currentRun());
    if (!state.automationRunning) els.feedbackNote.focus();
  }

  async function submitFeedback() {
    const run = currentRun();
    const decision = state.selectedDecision;
    if (!run || !decision) {
      const error = new Error("请先选择一种反馈状态。");
      els.feedbackError.textContent = error.message;
      els.feedbackError.hidden = false;
      throw error;
    }
    const note = els.feedbackNote.value.trim();
    if (decision === "edited" && !note) {
      const error = new Error("选择“修改后采纳”时，请填写具体修改说明。");
      els.feedbackError.textContent = error.message;
      els.feedbackError.hidden = false;
      els.feedbackNote.focus();
      throw error;
    }
    if (!els.apiKey.value.trim()) {
      const error = new Error("缺少 X-API-Key，无法提交反馈。 ");
      els.feedbackError.textContent = error.message;
      els.feedbackError.hidden = false;
      throw error;
    }

    els.feedbackError.hidden = true;
    els.feedbackSubmit.disabled = true;
    els.feedbackSubmit.textContent = "正在提交…";
    state.feedbackController = new AbortController();
    try {
      // 与当前 FastAPI FeedbackRequest 对齐：decision / notes，而非额外伪造 status / note 字段。
      await postJson(FEEDBACK_ENDPOINT, {
        run_id: run.run_id,
        thread_id: run.thread_id,
        dataset_version: run.dataset_version || null,
        decision,
        notes: note,
      }, FEEDBACK_TIMEOUT_MS, state.feedbackController.signal);
      run.feedback = { decision, note, savedAt: new Date().toISOString() };
      renderFeedback(run);
      showToast(`${DECISIONS[decision].saved}，已绑定当前 Run ID`);
      announce(`${DECISIONS[decision].saved}。服务端已接收，未触发自动业务动作。`);
      if (state.briefSource === "live" && state.briefData && state.briefData.runId === run.run_id) {
        state.briefData = makeBriefFromRun(run);
        renderBrief(state.briefData);
      }
      return run.feedback;
    } catch (error) {
      els.feedbackError.textContent = error.message || "反馈提交失败，请重试。";
      els.feedbackError.hidden = false;
      showToast("反馈未提交，请查看错误", "error");
      announce(`反馈提交失败：${els.feedbackError.textContent}`);
      throw error;
    } finally {
      state.feedbackController = null;
      els.feedbackSubmit.disabled = false;
      if (state.selectedDecision) els.feedbackSubmit.textContent = `提交“${DECISIONS[state.selectedDecision].label}”反馈`;
    }
  }

  function resetThread() {
    if (state.requestController) state.requestController.abort("user");
    state.runs = [];
    state.currentRunId = null;
    state.threadId = null;
    state.lastQuestion = "";
    state.selectedDecision = null;
    els.question.value = "";
    els.feedbackNote.value = "";
    els.followupComposer.hidden = true;
    updateCharacterCount();
    showQuestionError("");
    renderThreadContext();
    setResultState("empty");
    setApiStatus("idle", "等待真实请求");
    els.syncCurrent.disabled = true;
    if (state.briefSource === "live") {
      state.briefSource = "static";
      state.briefData = null;
      loadStaticBrief(true);
    }
    els.question.focus();
  }

  function setView(view, focusTab = false) {
    const validView = view === "brief" ? "brief" : "workspace";
    state.activeView = validView;
    document.querySelectorAll(".view-tab").forEach((tab) => {
      const active = tab.dataset.view === validView;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
      if (active && focusTab) tab.focus();
    });
    byId("workspace-view").hidden = validView !== "workspace";
    byId("brief-view").hidden = validView !== "brief";
    if (validView === "brief" && state.briefData) renderBrief(state.briefData);
  }

  function makeBriefFromRun(run) {
    const checks = deriveChecks(run);
    return {
      sourceType: "live",
      title: "本轮洞察 · 会前收拢",
      period: `数据版本：${run.dataset_version || "未绑定"} · 意图：${INTENTS[run.intent] || run.intent || "未提供"}`,
      generatedAt: run.receivedAt,
      claims: run.claims,
      evidence: run.evidence,
      counter_checks: checks.counter.map((item) => item.text),
      missing: checks.missing.map((item) => item.text),
      next_steps: run.next_steps,
      limitations: run.limitations,
      runId: run.run_id,
      threadId: run.thread_id,
      question: run.question,
      feedback: run.feedback,
    };
  }

  function validateStaticBrief(payload) {
    if (!payload || !Array.isArray(payload.claims) || !Array.isArray(payload.evidence)) {
      throw new Error("静态快照格式无效：需要 claims[] 与 evidence[]。");
    }
    return {
      ...payload,
      sourceType: "static",
      title: payload.title || "品牌 A · 会前证据快照",
      period: payload.source_period || "来源时间窗见各证据卡",
      counter_checks: Array.isArray(payload.counter_checks) ? payload.counter_checks : [],
      missing: Array.isArray(payload.missing) ? payload.missing : (Array.isArray(payload.limitations) ? payload.limitations : []),
      next_steps: Array.isArray(payload.next_steps) ? payload.next_steps : [],
    };
  }

  async function loadStaticBrief(force = false) {
    if (state.briefData && state.briefSource === "static" && !force) {
      renderBrief(state.briefData);
      return;
    }
    els.briefLoading.hidden = false;
    els.briefError.hidden = true;
    if (state.briefSource !== "live") els.briefPaper.hidden = true;
    const candidates = ["/evidence/insights.json", "./insights.json"];
    let lastError = null;
    for (const path of candidates) {
      try {
        const response = await fetch(path, { headers: { "Accept": "application/json" }, credentials: "same-origin", cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = validateStaticBrief(await response.json());
        if (state.briefSource !== "live") {
          state.briefData = payload;
          state.briefSource = "static";
          renderBrief(payload);
        }
        return;
      } catch (error) {
        lastError = error;
      }
    }
    els.briefLoading.hidden = true;
    if (!state.briefData) {
      els.briefPaper.hidden = true;
      els.briefError.hidden = false;
      byId("brief-error-message").textContent = `未读取到 /evidence/insights.json 或 ./insights.json（${lastError ? lastError.message : "未知错误"}）。可返回工作台完成一次真实问答，再点击“收拢成会前一页纸”。`;
    }
  }

  function renderBrief(data) {
    els.briefLoading.hidden = true;
    els.briefError.hidden = true;
    els.briefPaper.hidden = false;
    const isLive = data.sourceType === "live";
    byId("brief-source-label").textContent = isLive
      ? "来自当前选中的真实 API 轮次；内容随所选轮次与反馈更新。"
      : "静态证据快照，仅用于会前整理；不是模型回答，也不证明 API 已成功运行。";
    byId("brief-title").textContent = data.title;
    byId("brief-period").textContent = data.period;
    byId("brief-source-kind").textContent = isLive ? "真实 API 轮次" : "静态证据快照";
    const date = isLive ? new Date(data.generatedAt) : null;
    byId("brief-date").textContent = isLive && !Number.isNaN(date.getTime())
      ? date.toLocaleString("zh-CN", { hour12: false })
      : (data.snapshot_date || "快照日见来源");
    byId("brief-live-context").hidden = !isLive;
    if (isLive) byId("brief-live-context").textContent = `问题：${data.question} ｜ thread_id：${data.threadId}`;

    const claimsContainer = byId("brief-claims");
    clear(claimsContainer);
    if (!data.claims.length) claimsContainer.appendChild(createElement("p", "", "本轮没有可验证主张，不能用推测补齐。"));
    data.claims.forEach((claim) => {
      const item = createElement("div", "brief-claim");
      item.appendChild(createElement("strong", "", `${KINDS[claim.kind] || "观察"} · ${CONFIDENCE[claim.confidence] || claim.confidence || "未标置信"}`));
      item.appendChild(createElement("p", "", claim.text));
      claimsContainer.appendChild(item);
    });

    const evidenceContainer = byId("brief-evidence");
    clear(evidenceContainer);
    if (!data.evidence.length) evidenceContainer.appendChild(createElement("p", "", "无证据卡。请先补数，不作确定结论。"));
    data.evidence.forEach((evidence) => {
      const item = createElement("div", "brief-evidence-item");
      item.appendChild(createElement("strong", "", evidence.id || evidence.source_material || "证据"));
      const copy = createElement("p", "", evidence.content || "无正文");
      copy.appendChild(createElement("span", "", `${evidence.source_file || "文件未提供"} · ${evidence.source_sheet || "sheet 未提供"} · ${evidence.source_field || "字段未提供"} · 行：${evidence.source_row || "未提供"}`));
      item.appendChild(copy);
      evidenceContainer.appendChild(item);
    });

    const counterItems = data.counter_checks.length ? data.counter_checks : ["未提供反方检查；请在会议中主动询问替代解释与口径边界。"];
    const missingItems = data.missing.length ? data.missing : ["未列出额外缺口；空白不代表材料已经完整。"];
    const actionItems = data.next_steps.length ? data.next_steps : ["为每条待验证主张指定 owner、截止时间与最小补数动作。"];
    renderList(byId("brief-counter"), counterItems, "暂无");
    renderList(byId("brief-missing"), missingItems, "暂无");
    renderList(byId("brief-actions"), actionItems, "暂无", true);

    const feedbackSummary = byId("brief-feedback-summary");
    clear(feedbackSummary);
    if (isLive && data.feedback) {
      const note = createElement("p", "brief-feedback-note", `反馈：${DECISIONS[data.feedback.decision].label}${data.feedback.note ? ` · ${data.feedback.note}` : ""}`);
      feedbackSummary.appendChild(note);
    } else {
      feedbackSummary.appendChild(createElement("p", "brief-feedback-note", isLive ? "本轮尚未提交反馈；请回工作台完成采纳、修改、驳回或待验证。" : "静态快照没有 Run ID，不承载 API 反馈。"));
    }
    byId("brief-run-reference").textContent = isLive ? `Run ID: ${data.runId}` : `Dataset: ${data.dataset_version || "见证据卡"}`;
  }

  function useCurrentBrief() {
    const run = currentRun();
    if (!run) {
      showToast("请先完成一次真实问答", "error");
      return;
    }
    state.briefData = makeBriefFromRun(run);
    state.briefSource = "live";
    renderBrief(state.briefData);
    setView("brief");
  }

  async function copyRunId() {
    const run = currentRun();
    if (!run) return;
    try {
      await navigator.clipboard.writeText(run.run_id);
      showToast("Run ID 已复制");
    } catch {
      const input = createElement("textarea");
      input.value = run.run_id;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      const copied = document.execCommand("copy");
      input.remove();
      showToast(copied ? "Run ID 已复制" : "复制失败，请手动选择 Run ID", copied ? "success" : "error");
    }
  }

  function bindEvents() {
    document.querySelectorAll(".template-card").forEach((button) => {
      button.addEventListener("click", () => {
        els.question.value = button.dataset.question || "";
        updateCharacterCount();
        showQuestionError("");
        els.question.focus();
        els.question.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    });
    els.question.addEventListener("input", () => { updateCharacterCount(); showQuestionError(""); });
    els.form.addEventListener("submit", (event) => {
      event.preventDefault();
      requestInsight(els.question.value).catch(() => {});
    });
    byId("cancel-request").addEventListener("click", () => state.requestController && state.requestController.abort("user"));
    byId("retry-button").addEventListener("click", () => requestInsight(state.lastQuestion || els.question.value).catch(() => {}));
    byId("edit-question-button").addEventListener("click", () => { els.question.focus(); els.question.scrollIntoView({ behavior: "smooth", block: "center" }); });
    els.newThread.addEventListener("click", resetThread);
    byId("copy-run-id").addEventListener("click", copyRunId);
    byId("focus-followup-button").addEventListener("click", () => { els.question.focus(); els.question.scrollIntoView({ behavior: "smooth", block: "center" }); });
    document.querySelectorAll(".feedback-options button").forEach((button) => button.addEventListener("click", () => selectDecision(button.dataset.decision)));
    byId("close-feedback").addEventListener("click", () => { state.selectedDecision = null; renderFeedback(currentRun()); });
    els.feedbackForm.addEventListener("submit", (event) => { event.preventDefault(); submitFeedback().catch(() => {}); });
    byId("open-brief-button").addEventListener("click", useCurrentBrief);
    els.syncCurrent.addEventListener("click", useCurrentBrief);
    byId("print-brief-button").addEventListener("click", () => window.print());

    const tabs = Array.from(document.querySelectorAll(".view-tab"));
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => setView(tab.dataset.view));
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        let nextIndex = index;
        if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
        if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
        if (event.key === "Home") nextIndex = 0;
        if (event.key === "End") nextIndex = tabs.length - 1;
        setView(tabs[nextIndex].dataset.view, true);
      });
    });
  }

  async function runAutomation() {
    const params = new URLSearchParams(window.location.search);
    const scenario = params.get("scenario");
    const autorun = params.get("autorun") === "1";
    if (params.get("view") === "brief" && !autorun && !scenario) {
      setView("brief");
      document.body.dataset.autorun = "done";
      window.__prototypeAutomation = { status: "done", scenario: "brief" };
      return;
    }
    if (!autorun && !scenario) {
      document.body.dataset.autorun = "idle";
      return;
    }
    const defaultQuestion = "品牌A的 Gen Z 人群在曝光、互动和特定成交条件组中的占比分别是多少？这些指标能否构成转化漏斗？";
    const question = (params.get("question") || defaultQuestion).slice(0, 1200);
    state.automationRunning = true;
    document.body.dataset.autorun = "running";
    window.__prototypeAutomation = { status: "running", scenario: scenario || "autorun" };
    try {
      if (scenario === "full") {
        const firstRun = await requestInsight(question);
        const followupDefaults = {
          audience_profile: "请接着上一轮，仅针对品牌A的 Gen Z 人群证据继续追问：曝光、互动和特定成交条件组为什么不能构成漏斗？如果要带入会议，最先需要补哪一项人群验证？",
          live_roi: "请接着上一轮直播投放证据继续追问：加权ROI与报告日ROI简单均值为何不同？异常残余行和口径门槛在会议前还需要怎样复核？",
          experience: "请接着上一轮产品体验证据继续追问：哪些说法只来自人工归纳或样本原声？会议前最需要补充哪一项使用场景验证？",
          fulfillment: "请接着上一轮履约证据继续追问：为什么不能把工单关联直接当作退款责任？订单分母和观察窗口还需怎样复核？",
        };
        const defaultFollowup = followupDefaults[firstRun.intent] || "请接着上一轮证据继续追问：哪些结论不能用于因果归因、长期趋势或跨品牌外推？会议前最先需要补哪一项验证？";
        const followup = (params.get("followup") || defaultFollowup).slice(0, 1200);
        await requestInsight(followup);
        if (currentRun() && currentRun().evidence.length) {
          openEvidence(0, false);
          byId("evidence-card-0").scrollIntoView({ block: "center" });
        } else {
          byId("answer-title").scrollIntoView({ block: "start" });
        }
      } else if (scenario === "feedback") {
        await requestInsight(question);
        const requestedDecision = params.get("decision") || "accepted";
        const decision = ["accepted", "needs_validation"].includes(requestedDecision) ? requestedDecision : "accepted";
        selectDecision(decision);
        els.feedbackNote.value = (params.get("note") || (decision === "accepted" ? "会前流程自动截图：采纳本轮回答，仍保留人工复核。" : "会前流程自动截图：标记为待验证，需业务 owner 补核。")).slice(0, 2000);
        await submitFeedback();
        byId("feedback-section").scrollIntoView({ block: "center" });
      } else if (autorun) {
        await requestInsight(question);
        byId("answer-title").scrollIntoView({ block: "start" });
      } else {
        throw new Error(`不支持的 scenario=${scenario}；支持 full 或 feedback。`);
      }
      document.body.dataset.autorun = "done";
      window.__prototypeAutomation = { status: "done", scenario: scenario || "autorun", runId: state.currentRunId, threadId: state.threadId, rounds: state.runs.length };
    } catch (error) {
      document.body.dataset.autorun = "error";
      window.__prototypeAutomation = { status: "error", scenario: scenario || "autorun", message: error.message || String(error) };
      console.error("自动执行失败：", error);
    } finally {
      state.automationRunning = false;
    }
  }

  async function init() {
    bindEvents();
    updateCharacterCount();
    renderThreadContext();
    setResultState("empty");
    await loadStaticBrief();
    await runAutomation();
  }

  init().catch((error) => {
    console.error("工作台初始化失败：", error);
    setApiStatus("error", "页面初始化失败");
    els.errorMessage.textContent = `页面初始化失败：${error.message}`;
    els.errorDetail.textContent = "请检查浏览器控制台；未生成任何替代答案。";
    setResultState("error");
    document.body.dataset.autorun = "error";
  });
})();
