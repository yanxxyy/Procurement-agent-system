const state = {
  run: null,
  tasks: [],
  events: [],
  expandedTasks: new Set(["report"]),
  expandedTools: new Set(["report.compose"]),
  stream: null,
  replaying: false,
  approvalReady: false,
  langfuse: { connected: false },
};

const els = {
  taskList: document.querySelector("#taskList"),
  workflow: document.querySelector("#workflow"),
  overallProgress: document.querySelector("#overallProgress"),
  scoreRing: document.querySelector("#scoreRing"),
  completionText: document.querySelector("#completionText"),
  streamStatus: document.querySelector("#streamStatus"),
  replayButton: document.querySelector("#replayButton"),
  toggleAllButton: document.querySelector("#toggleAllButton"),
  eventList: document.querySelector("#eventList"),
  eventCount: document.querySelector("#eventCount"),
  recoveryStrip: document.querySelector("#recoveryStrip"),
  sandboxInstance: document.querySelector("#sandboxInstance"),
  langfuseHeaderStatus: document.querySelector("#langfuseHeaderStatus"),
  traceLink: document.querySelector("#traceLink"),
  langfuseInlineStatus: document.querySelector("#langfuseInlineStatus"),
  sidebarAvatar: document.querySelector("#sidebarAvatar"),
  sidebarUserName: document.querySelector("#sidebarUserName"),
  sidebarUserRole: document.querySelector("#sidebarUserRole"),
  rolePill: document.querySelector("#rolePill"),
  applicationList: document.querySelector("#applicationList"),
  applicationsTitle: document.querySelector("#applicationsTitle"),
  toast: document.querySelector("#toast"),
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function prettyJson(value) {
  return escapeHtml(JSON.stringify(value, null, 2));
}

function statusLabel(status) {
  return {
    completed: "已完成",
    in_progress: "执行中",
    pending: "待处理",
    awaiting_approval: "待审批",
    failed: "失败",
    running: "调用中",
  }[status] || status;
}

function statusGlyph(status) {
  if (status === "completed") return "✓";
  if (status === "in_progress" || status === "running") return "↻";
  if (status === "awaiting_approval") return "!";
  return "·";
}

function nowStamp() {
  const now = new Date();
  return now.toLocaleTimeString("zh-CN", { hour12: false }) + "." + String(now.getMilliseconds()).padStart(3, "0");
}

function renderWorkflow() {
  els.workflow.innerHTML = state.tasks.map((task) => `
    <div class="workflow-step ${task.status}" data-workflow-id="${task.id}">
      <div class="workflow-node">
        <span class="workflow-number">${task.status === "completed" ? "✓" : task.index}</span>
        <span class="workflow-copy">
          <strong>${escapeHtml(task.title)}</strong>
          <small>${statusLabel(task.status)}</small>
        </span>
      </div>
    </div>
  `).join("");
}

function toolTemplate(tool) {
  const expanded = state.expandedTools.has(tool.id);
  return `
    <article class="tool-call ${tool.status} ${expanded ? "expanded" : ""}" data-tool-id="${tool.id}">
      <button class="tool-summary" type="button" aria-expanded="${expanded}">
        <span class="tool-status">${statusGlyph(tool.status)}</span>
        <span class="tool-copy"><strong>${escapeHtml(tool.name)}</strong><span>${escapeHtml(tool.description)}</span></span>
        <span class="tool-meta"><b class="tool-kind">${escapeHtml(tool.kind)}</b><span>${escapeHtml(tool.duration)}</span></span>
        <span class="mini-chevron">⌄</span>
      </button>
      <div class="tool-detail">
        <div class="tool-io">
          <div class="code-block"><label>INPUT</label><pre>${prettyJson(tool.input)}</pre></div>
          <div class="code-block"><label>OUTPUT</label><pre>${prettyJson(tool.output)}</pre></div>
        </div>
        <div class="span-row">LangFuse span · <b>${escapeHtml(tool.trace)}</b> · ${statusLabel(tool.status)}</div>
      </div>
    </article>
  `;
}

function approvalTemplate(task) {
  if (!task.approval || task.status === "completed") {
    if (task.order_no) return `<div class="approval-box"><div><strong>订单已同步 ERP</strong><span class="order-result">${escapeHtml(task.order_no)} · ¥287,400</span></div></div>`;
    return "";
  }
  if (!state.run?.permissions?.can_approve_order) {
    return `
      <div class="approval-box">
        <div><strong>当前账号没有审批权限</strong><span>普通员工可以发起采购申请并跟踪进度，订单审批由管理员完成。</span></div>
        <a class="approve-button approve-link" href="/chat">发起采购申请</a>
      </div>
    `;
  }
  const ready = task.status === "awaiting_approval";
  return `
    <div class="approval-box">
      <div><strong>${ready ? "需要人工二次确认" : "敏感写操作已拦截"}</strong><span>${ready ? "震坤行 · ¥287,400 · 2026-09-12 到仓" : "报告完成后开放订单审批"}</span></div>
      <button class="approve-button" type="button" ${ready ? "" : "disabled"}>${ready ? "批准并创建订单" : "等待前置任务"}</button>
    </div>
  `;
}

function renderTasks() {
  els.taskList.innerHTML = state.tasks.map((task) => {
    const expanded = state.expandedTasks.has(task.id);
    return `
      <article class="task-item ${task.status} ${expanded ? "expanded" : ""}" data-task-id="${task.id}">
        <button class="task-summary" type="button" aria-expanded="${expanded}">
          <span class="status-icon">${statusGlyph(task.status)}</span>
          <span class="task-main">
            <span class="task-title-line"><span class="task-index">${task.index}</span><span class="task-title">${escapeHtml(task.title)}</span></span>
            <p>${escapeHtml(task.summary)}</p>
          </span>
          <span class="task-meta"><span class="agent-tag">${escapeHtml(task.agent)}</span><span>${task.tools.length ? `${task.tools.length} tools` : "工具明细受限"}</span><span class="duration">${escapeHtml(task.duration)}</span></span>
          <svg class="chevron" aria-hidden="true" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>
        </button>
        <div class="task-body">
          <div class="task-progress"><span style="--task-progress:${task.progress}%"></span></div>
          <div class="tool-list">${task.tools.length ? task.tools.map(toolTemplate).join("") : '<div class="permission-notice">当前角色只能查看任务状态，工具输入、输出和链路详情仅管理员可见。</div>'}</div>
          ${approvalTemplate(task)}
        </div>
      </article>
    `;
  }).join("");
  bindTaskInteractions();
}

function applyUserPermissions(user) {
  if (!user) return;
  document.body.classList.remove("auth-pending");
  document.body.dataset.role = user.role;
  els.sidebarAvatar.textContent = user.name.slice(0, 1);
  els.sidebarUserName.textContent = user.name;
  els.sidebarUserRole.textContent = `${user.department} · ${user.role_label}`;
  els.rolePill.textContent = user.role_label;
  els.rolePill.classList.toggle("admin", user.role === "admin");
  document.querySelectorAll(".admin-only").forEach((node) => { node.hidden = user.role !== "admin"; });
  if (user.role !== "admin") {
    els.toggleAllButton.textContent = "查看任务状态";
    els.toggleAllButton.disabled = true;
  }
}

function renderProgress() {
  const completed = state.tasks.filter((task) => task.status === "completed").length;
  const active = state.tasks.find((task) => task.status === "in_progress");
  const progress = state.run?.progress ?? Math.min(100, Math.round(((completed + ((active?.progress || 0) / 100)) / state.tasks.length) * 100));
  els.overallProgress.textContent = progress;
  els.scoreRing.style.setProperty("--progress", progress);
  els.completionText.textContent = `${completed} / ${state.tasks.length} 已完成`;
}

function renderEvents() {
  els.eventList.innerHTML = state.events.slice(-9).reverse().map((event) => `
    <li><span class="event-time">${escapeHtml(event.time)}</span><i class="event-dot ${event.tone || "info"}"></i><span>${escapeHtml(event.text)}</span></li>
  `).join("");
  els.eventCount.textContent = String(state.events.length).padStart(2, "0");
}

function renderLangfuseStatus(status) {
  state.langfuse = { ...state.langfuse, ...status };
  const connected = Boolean(state.langfuse.connected);
  els.langfuseHeaderStatus.textContent = connected ? "LangFuse 已接入" : "LangFuse 未接入";
  els.traceLink.textContent = connected
    ? `${state.langfuse.project_name || "打开最新 Trace"} ↗`
    : "去接入 LangFuse ↗";
  els.traceLink.href = connected ? (state.langfuse.trace_url || "/langfuse/open") : "/langfuse";
  els.traceLink.target = connected ? "_blank" : "";
  els.traceLink.rel = connected ? "noopener" : "";
  els.langfuseInlineStatus.classList.toggle("connected", connected);
  const title = els.langfuseInlineStatus.querySelector("strong");
  const copy = els.langfuseInlineStatus.querySelector("small");
  const action = els.langfuseInlineStatus.querySelector("b");
  title.textContent = connected ? `已接入 · ${state.langfuse.project_name || "LangFuse Project"}` : "尚未接入真实 LangFuse";
  copy.textContent = connected
    ? `最新 Trace ${state.langfuse.trace_id || "等待生成"}`
    : "点击配置 Host 与项目 API Key";
  action.textContent = connected ? "管理" : "配置";
}

async function loadLangfuseStatus() {
  try {
    const response = await fetch("/api/langfuse/status", { credentials: "same-origin" });
    if (!response.ok) return;
    renderLangfuseStatus(await response.json());
  } catch (_) {
    renderLangfuseStatus({ connected: false });
  }
}

function renderApplications(applications) {
  const isAdmin = state.run?.user?.role === "admin";
  els.applicationsTitle.textContent = isAdmin ? "待审批订单申请" : "我的订单申请";
  if (!applications.length) {
    els.applicationList.innerHTML = `<div class="application-empty">${isAdmin ? "当前没有待处理的订单申请" : "你还没有提交订单申请，可在采购对话中发起"}</div>`;
    return;
  }
  els.applicationList.innerHTML = applications.map((item) => `
    <article class="application-row" data-application-id="${escapeHtml(item.id)}">
      <div class="application-primary"><strong>${escapeHtml(item.item)}</strong><span>${escapeHtml(item.id)} · ${escapeHtml(item.applicant)}</span></div>
      <div class="application-cell"><strong>${Number(item.quantity).toLocaleString("zh-CN")} 件</strong><span>${escapeHtml(item.required_date)} 到货</span></div>
      <div class="application-cell"><strong>${escapeHtml(item.department)}</strong><span>${escapeHtml(item.reason)}</span></div>
      <div class="application-row-actions">
        ${item.status === "approved"
          ? `<span class="application-state approved">已批准 · ${escapeHtml(item.order_no || "已生成订单")}</span>`
          : isAdmin
            ? `<button class="application-approve" type="button">批准申请</button>`
            : '<span class="application-state">等待管理员审批</span>'}
        <a class="application-detail-link" href="/applications?id=${encodeURIComponent(item.id)}">查看详情</a>
      </div>
    </article>
  `).join("");
  document.querySelectorAll(".application-approve").forEach((button) => button.addEventListener("click", approveApplication));
}

async function loadApplications() {
  try {
    const response = await fetch("/api/orders/applications", { credentials: "same-origin" });
    if (!response.ok) return;
    renderApplications(((await response.json()).applications || []).slice(0, 5));
  } catch (_) {
    els.applicationList.innerHTML = '<div class="application-empty">申请记录暂时无法载入</div>';
  }
}

async function approveApplication(event) {
  const button = event.currentTarget;
  const id = button.closest(".application-row").dataset.applicationId;
  button.disabled = true;
  button.textContent = "审批中…";
  try {
    const response = await fetch(`/api/orders/applications/${encodeURIComponent(id)}/approve`, { method: "POST", credentials: "same-origin" });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "审批失败");
    showToast(`${body.message} · ${body.application.order_no}`);
    await loadApplications();
    if (state.run?.permissions?.can_view_traces) await loadLangfuseStatus();
  } catch (error) {
    showToast(error.message);
    button.disabled = false;
    button.textContent = "批准申请";
  }
}

function bindTaskInteractions() {
  document.querySelectorAll(".task-summary").forEach((button) => {
    button.addEventListener("click", () => {
      const id = button.closest(".task-item").dataset.taskId;
      state.expandedTasks.has(id) ? state.expandedTasks.delete(id) : state.expandedTasks.add(id);
      renderTasks();
    });
  });
  document.querySelectorAll(".tool-summary").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const id = button.closest(".tool-call").dataset.toolId;
      state.expandedTools.has(id) ? state.expandedTools.delete(id) : state.expandedTools.add(id);
      renderTasks();
    });
  });
  document.querySelectorAll(".approve-button:not(:disabled)").forEach((button) => {
    button.addEventListener("click", approveOrder);
  });
}

function updateTask(id, patch) {
  const task = state.tasks.find((item) => item.id === id);
  if (task) Object.assign(task, patch);
  return task;
}

function updateTool(taskId, toolId, patch) {
  const task = state.tasks.find((item) => item.id === taskId);
  const tool = task?.tools.find((item) => item.id === toolId);
  if (tool) Object.assign(tool, patch);
  return tool;
}

function addEvent(text, tone = "info") {
  state.events.push({ time: nowStamp(), text, tone });
  renderEvents();
}

function handleTrace(event) {
  const task = event.task_id ? state.tasks.find((item) => item.id === event.task_id) : null;
  if (event.type === "run_reset") resetForReplay();
  if (event.type === "task_started" && task) {
    state.tasks.forEach((item) => { if (item.status === "in_progress") item.status = "pending"; });
    updateTask(event.task_id, { status: "in_progress", progress: 18, duration: "进行中", started_at: nowStamp().slice(0, 8) });
    state.expandedTasks.add(event.task_id);
  }
  if (event.type === "tool_started") {
    updateTool(event.task_id, event.tool_id, { status: "running", duration: "进行中" });
    if (task) task.progress = Math.max(task.progress, 52);
  }
  if (event.type === "tool_progress" && task) task.progress = event.task_progress || task.progress;
  if (event.type === "tool_completed") {
    updateTool(event.task_id, event.tool_id, { status: "completed", duration: "完成" });
    if (task) task.progress = Math.max(task.progress, 82);
  }
  if (event.type === "task_completed") {
    updateTask(event.task_id, { status: "completed", progress: 100, duration: task?.duration === "进行中" ? "完成" : task?.duration });
    task?.tools.forEach((tool) => {
      if (tool.status !== "completed") {
        tool.status = "completed";
        tool.duration = tool.duration === "—" || tool.duration === "进行中" ? "完成" : tool.duration;
      }
    });
    if (event.task_id === "report") document.querySelector(".running-bar")?.classList.remove("running-bar");
    state.run.progress = event.progress;
  }
  if (event.type === "approval_required") {
    updateTask(event.task_id, { status: "awaiting_approval", progress: 0, duration: "等待审批" });
    state.run.progress = event.progress;
    state.approvalReady = true;
    state.expandedTasks.add("order");
  }
  if (event.type === "sandbox_hotswap") {
    els.sandboxInstance.textContent = event.sandbox?.instance_id
      ? `${event.sandbox.instance_id} · Gen ${event.sandbox.generation}`
      : "instance restored";
    els.recoveryStrip.classList.remove("flash");
    void els.recoveryStrip.offsetWidth;
    els.recoveryStrip.classList.add("flash");
  }
  if (event.langfuse?.connected) {
    renderLangfuseStatus({
      connected: true,
      trace_id: event.langfuse.trace_id,
      trace_url: event.langfuse.trace_url,
    });
  }
  addEvent(event.message, event.type === "sandbox_hotswap" || event.type === "approval_required" ? "warning" : event.type.includes("completed") ? "success" : "live");
  renderWorkflow();
  renderTasks();
  renderProgress();
}

function connectStream(mode = "tail") {
  if (state.stream) state.stream.close();
  els.streamStatus.textContent = "SSE CONNECTING";
  const stream = new EventSource(`/api/runs/demo/events?mode=${mode}&t=${Date.now()}`);
  state.stream = stream;
  stream.addEventListener("open", () => { els.streamStatus.textContent = "SSE LIVE"; });
  stream.addEventListener("trace", (message) => handleTrace(JSON.parse(message.data)));
  stream.addEventListener("done", (message) => {
    const result = JSON.parse(message.data);
    if (result.langfuse_connected) {
      renderLangfuseStatus({ connected: true, trace_id: result.trace_id, trace_url: result.trace_url });
    }
    stream.close();
    state.replaying = false;
    els.replayButton.disabled = false;
    els.streamStatus.textContent = state.approvalReady ? "AWAITING APPROVAL" : "SSE IDLE";
  });
  stream.onerror = () => {
    if (stream.readyState === EventSource.CLOSED) return;
    els.streamStatus.textContent = "SSE RETRYING";
  };
}

function resetForReplay() {
  state.tasks.forEach((task, index) => {
    task.status = index === 0 ? "in_progress" : "pending";
    task.progress = index === 0 ? 12 : 0;
    task.duration = index === 0 ? "进行中" : "等待前置任务";
    task.order_no = null;
    task.tools.forEach((tool) => { tool.status = "pending"; tool.duration = "—"; });
  });
  state.run.progress = 2;
  state.events = [];
  state.approvalReady = false;
  state.expandedTasks = new Set(["requirement"]);
  state.expandedTools.clear();
  renderWorkflow(); renderTasks(); renderProgress(); renderEvents();
}

async function replay() {
  if (state.replaying) return;
  state.replaying = true;
  els.replayButton.disabled = true;
  connectStream("replay");
}

async function approveOrder(event) {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在写入 ERP…";
  updateTask("order", { status: "in_progress", progress: 45, duration: "进行中" });
  updateTool("order", "erp.create_order", { status: "running", duration: "进行中" });
  addEvent("人工审批通过，正在调用 erp.create_order", "live");
  renderWorkflow(); renderTasks(); renderProgress();
  try {
    const response = await fetch("/api/runs/demo/approve", { method: "POST" });
    if (!response.ok) throw new Error("审批请求失败");
    const result = await response.json();
    const task = updateTask("order", { status: "completed", progress: 100, duration: result.tool.duration, order_no: result.order_no });
    Object.assign(task.tools[0], result.tool);
    state.run.progress = 100;
    state.approvalReady = false;
    if (result.langfuse?.connected) {
      renderLangfuseStatus({ connected: true, trace_id: result.langfuse.trace_id, trace_url: result.langfuse.trace_url });
    }
    addEvent(`${result.message} · ${result.order_no}`, "success");
    els.streamStatus.textContent = "RUN COMPLETED";
    renderWorkflow(); renderTasks(); renderProgress();
    showToast(`订单创建成功 · ${result.order_no}`);
  } catch (error) {
    updateTask("order", { status: "awaiting_approval", progress: 0, duration: "等待审批" });
    updateTool("order", "erp.create_order", { status: "pending", duration: "—" });
    renderWorkflow(); renderTasks();
    showToast(error.message);
  }
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("visible");
  window.setTimeout(() => els.toast.classList.remove("visible"), 3200);
}

els.replayButton.addEventListener("click", replay);
els.toggleAllButton.addEventListener("click", () => {
  const allExpanded = state.expandedTasks.size === state.tasks.length;
  state.expandedTasks = allExpanded ? new Set() : new Set(state.tasks.map((task) => task.id));
  els.toggleAllButton.textContent = allExpanded ? "全部展开" : "全部收起";
  renderTasks();
});

async function boot() {
  try {
    const response = await fetch("/api/runs/demo");
    if (response.status === 401) {
      location.replace("/login?return_to=/runs");
      return;
    }
    if (!response.ok) throw new Error("无法载入演示任务");
    state.run = await response.json();
    state.tasks = state.run.tasks;
    state.events = state.run.events;
    applyUserPermissions(state.run.user);
    document.querySelector("#runId").textContent = state.run.run_id;
    document.querySelector("#runTitle").textContent = state.run.title;
    document.querySelector("#runRequest").textContent = state.run.request;
    renderWorkflow(); renderTasks(); renderProgress(); renderEvents();
    await loadApplications();
    if (state.run.permissions?.can_view_traces) await loadLangfuseStatus();
    window.setTimeout(() => connectStream("tail"), 700);
    if (new URLSearchParams(location.search).has("forbidden")) showToast("当前账号没有访问该管理功能的权限");
  } catch (error) {
    els.taskList.innerHTML = `<div class="approval-box"><div><strong>数据载入失败</strong><span>${escapeHtml(error.message)}</span></div></div>`;
    els.streamStatus.textContent = "OFFLINE";
  }
}

window.addEventListener("focus", () => { if (state.run?.permissions?.can_view_traces) loadLangfuseStatus(); });
boot();
