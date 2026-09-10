const state = { user: null, applications: [], selectedId: null, filter: "all", query: "" };
const els = {
  avatar: document.querySelector("#sidebarAvatar"), name: document.querySelector("#sidebarUserName"), role: document.querySelector("#sidebarUserRole"),
  rolePill: document.querySelector("#rolePill"), scope: document.querySelector("#scopeLabel"), navCount: document.querySelector("#navApplicationCount"),
  total: document.querySelector("#totalCount"), pending: document.querySelector("#pendingCount"), approved: document.querySelector("#approvedCount"),
  list: document.querySelector("#archiveList"), detail: document.querySelector("#applicationDetail"), search: document.querySelector("#searchInput"),
  refresh: document.querySelector("#refreshButton"), toast: document.querySelector("#toast"), description: document.querySelector("#pageDescription"),
};

function escapeHtml(value) { const node = document.createElement("div"); node.textContent = value == null ? "" : String(value); return node.innerHTML; }
function formatDate(value) { if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date); }
function statusLabel(status) { return status === "approved" ? "已批准" : "待审批"; }
function showToast(message) { els.toast.textContent = message; els.toast.classList.add("visible"); window.setTimeout(() => els.toast.classList.remove("visible"), 2800); }

function setUser(user) {
  state.user = user;
  document.body.classList.remove("auth-pending");
  document.body.dataset.role = user.role;
  els.avatar.textContent = user.name.slice(0, 1);
  els.name.textContent = user.name;
  els.role.textContent = `${user.department} · ${user.role_label}`;
  els.rolePill.textContent = user.role_label;
  els.rolePill.classList.toggle("admin", user.role === "admin");
  document.querySelectorAll(".admin-only").forEach((node) => { node.hidden = user.role !== "admin"; });
  els.scope.textContent = user.role === "admin" ? "全部员工申请" : "仅我的申请";
  els.description.textContent = user.role === "admin" ? "查看全部员工申请，并保留每一次审批的完整记录。" : "每笔申请独立归档，你只能查看本人提交的记录。";
}

function renderSummary(summary) {
  els.total.textContent = summary.total;
  els.pending.textContent = summary.pending;
  els.approved.textContent = summary.approved;
  els.navCount.textContent = summary.total;
}

function visibleApplications() {
  const query = state.query.toLowerCase();
  return state.applications.filter((item) => {
    const matchesStatus = state.filter === "all" || item.status === state.filter;
    const haystack = `${item.id} ${item.item} ${item.applicant} ${item.department}`.toLowerCase();
    return matchesStatus && (!query || haystack.includes(query));
  });
}

function renderList() {
  const items = visibleApplications();
  if (!items.length) {
    els.list.innerHTML = '<div class="archive-empty">当前筛选条件下没有申请记录。<br />可从“采购对话”发起一笔新申请。</div>';
    return;
  }
  els.list.innerHTML = items.map((item) => `
    <button class="archive-item ${item.id === state.selectedId ? "active" : ""}" type="button" data-id="${escapeHtml(item.id)}">
      <div class="archive-item-head"><strong>${escapeHtml(item.item)}</strong></div>
      <span class="status-badge ${item.status === "approved" ? "approved" : ""}">${statusLabel(item.status)}</span>
      <div class="archive-item-meta"><span class="archive-item-id">${escapeHtml(item.id)}</span><span>${escapeHtml(item.applicant)}</span><span>${Number(item.quantity).toLocaleString("zh-CN")} 件</span><span>${formatDate(item.created_at)}</span></div>
      <span class="archive-item-id">${item.event_count || 1} 条记录</span>
    </button>
  `).join("");
  els.list.querySelectorAll(".archive-item").forEach((button) => button.addEventListener("click", () => selectApplication(button.dataset.id)));
}

function renderDetail(item) {
  const canApprove = state.user?.role === "admin" && item.status === "pending_approval";
  const events = item.events || [];
  els.detail.innerHTML = `
    <header class="detail-header">
      <div><p class="section-kicker">APPLICATION DETAIL</p><h2>${escapeHtml(item.item)}</h2><p>${escapeHtml(item.id)}</p></div>
      <span class="status-badge ${item.status === "approved" ? "approved" : ""}">${statusLabel(item.status)}</span>
    </header>
    <div class="detail-body">
      <div class="detail-grid">
        <div class="detail-field"><span>申请人</span><strong>${escapeHtml(item.applicant)} · ${escapeHtml(item.department)}</strong></div>
        <div class="detail-field"><span>提交时间</span><strong>${formatDate(item.created_at)}</strong></div>
        <div class="detail-field"><span>采购数量</span><strong>${Number(item.quantity).toLocaleString("zh-CN")} 件</strong></div>
        <div class="detail-field"><span>要求到货</span><strong>${escapeHtml(item.required_date)}</strong></div>
        <div class="detail-field wide"><span>申请原因</span><strong>${escapeHtml(item.reason)}</strong></div>
      </div>
      ${item.order_no ? `<div class="order-result"><span>已生成采购订单</span><strong>${escapeHtml(item.order_no)}</strong></div>` : ""}
      ${canApprove ? '<div class="detail-actions"><button class="detail-approve" id="detailApproveButton" type="button">批准并生成采购订单</button></div>' : ""}
      <section class="timeline-section"><h3>审批时间线 · ${events.length} 条记录</h3><ol class="audit-timeline">
        ${events.map((event) => `<li class="audit-event"><i class="audit-dot"></i><strong>${escapeHtml(event.message)}</strong><p>${escapeHtml(event.actor)} · ${event.actor_role === "admin" ? "管理员" : "普通员工"}</p><time>${formatDate(event.created_at)}</time></li>`).join("")}
      </ol></section>
    </div>`;
  document.querySelector("#detailApproveButton")?.addEventListener("click", approveSelected);
}

async function selectApplication(id) {
  state.selectedId = id;
  renderList();
  els.detail.innerHTML = '<div class="detail-empty"><strong>正在读取申请详情…</strong></div>';
  const response = await fetch(`/api/orders/applications/${encodeURIComponent(id)}`, { credentials: "same-origin" });
  if (response.status === 401) { location.replace("/login?return_to=/applications"); return; }
  if (!response.ok) { els.detail.innerHTML = '<div class="detail-empty"><strong>申请详情读取失败</strong><p>请刷新后重试。</p></div>'; return; }
  renderDetail((await response.json()).application);
  const url = new URL(location.href); url.searchParams.set("id", id); history.replaceState(null, "", url);
}

async function approveSelected() {
  const button = document.querySelector("#detailApproveButton");
  if (!button || !state.selectedId) return;
  button.disabled = true; button.textContent = "正在批准…";
  const response = await fetch(`/api/orders/applications/${encodeURIComponent(state.selectedId)}/approve`, { method: "POST", credentials: "same-origin" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) { button.disabled = false; button.textContent = "批准并生成采购订单"; showToast(body.detail || "审批失败"); return; }
  showToast(`${body.message} · ${body.application.order_no}`);
  await loadApplications(state.selectedId);
}

async function loadApplications(preferredId = null) {
  els.refresh.disabled = true;
  try {
    const response = await fetch("/api/orders/applications", { credentials: "same-origin" });
    if (response.status === 401) { location.replace("/login?return_to=/applications"); return; }
    if (!response.ok) throw new Error("申请记录读取失败");
    const body = await response.json();
    state.applications = body.applications || [];
    renderSummary(body.summary || { total: 0, pending: 0, approved: 0 });
    const candidate = preferredId || state.selectedId || new URLSearchParams(location.search).get("id") || state.applications[0]?.id;
    if (candidate && state.applications.some((item) => item.id === candidate)) await selectApplication(candidate); else { state.selectedId = null; renderList(); }
  } catch (error) {
    els.list.innerHTML = `<div class="archive-empty">${escapeHtml(error.message)}</div>`;
  } finally { els.refresh.disabled = false; }
}

document.querySelectorAll("[data-filter]").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll("[data-filter]").forEach((item) => item.classList.remove("active")); button.classList.add("active"); state.filter = button.dataset.filter; renderList(); }));
els.search.addEventListener("input", () => { state.query = els.search.value.trim(); renderList(); });
els.refresh.addEventListener("click", () => loadApplications(state.selectedId));

async function boot() {
  const userResponse = await fetch("/api/auth/me", { credentials: "same-origin" });
  if (userResponse.status === 401) { location.replace("/login?return_to=/applications"); return; }
  if (!userResponse.ok) { location.replace("/login?return_to=/applications"); return; }
  setUser(await userResponse.json());
  await loadApplications();
}
boot();
