const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
const formatDate = (value) => new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
function renderRows(items, type) {
  if (!items.length) return '<div class="empty-state">暂无记录</div>';
  return `<div class="overview-list">${items.map((item) => type === "application" ? `<a class="overview-row" href="/applications?id=${encodeURIComponent(item.id)}"><div><strong>${escapeHtml(item.item)}</strong><span>${escapeHtml(item.id)} · ${escapeHtml(item.applicant)}</span></div><em class="${item.status === "approved" ? "approved" : ""}">${item.status === "approved" ? "已批准" : "待审批"}</em></a>` : `<a class="overview-row" href="/chat?conversation=${encodeURIComponent(item.id)}"><div><strong>${escapeHtml(item.title)}</strong><span>${formatDate(item.updated_at)} · ${item.message_count} 条消息</span></div><em class="approved">续聊 →</em></a>`).join("")}</div>`;
}
async function boot() {
  const response = await fetch("/api/overview", { credentials: "same-origin" });
  if (response.status === 401) { location.replace("/login?return_to=/"); return; }
  const data = await response.json(); const user = data.user;
  document.body.classList.remove("auth-pending"); document.body.dataset.role = user.role;
  $("#sidebarAvatar").textContent = user.name.slice(0,1); $("#sidebarUserName").textContent = user.name; $("#sidebarUserRole").textContent = `${user.department} · ${user.role_label}`; $("#rolePill").textContent = user.role_label; $("#rolePill").classList.toggle("admin", user.role === "admin");
  document.querySelectorAll(".admin-only").forEach((node) => node.hidden = user.role !== "admin");
  $("#welcomeTitle").textContent = `${user.name}，欢迎回来`; $("#conversationCount").textContent = data.conversations.total; $("#applicationCount").textContent = data.applications.total; $("#pendingCount").textContent = data.applications.pending; $("#runProgress").textContent = `${data.run.progress}%`; $("#runSummary").textContent = `${data.run.completed}/${data.run.total} 子任务完成`;
  $("#applicationMetricLabel").textContent = user.role === "admin" ? "全部采购申请" : "我的采购申请"; $("#applicationMetricCopy").textContent = `${data.applications.approved} 笔已批准`;
  $("#recentApplications").innerHTML = renderRows(data.applications.recent, "application"); $("#recentConversations").innerHTML = renderRows(data.conversations.recent, "conversation");
  if (data.technical) { $("#skillsStatus").textContent = `${data.technical.skills_enabled}/${data.technical.skills_total} 已启用`; $("#sandboxStatus").textContent = data.technical.sandbox?.status === "active" ? "运行正常" : "需要检查"; }
  $("#refreshTime").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", {hour12:false})}`;
  if (new URLSearchParams(location.search).has("forbidden")) { $("#toast").textContent = "当前账号没有访问该管理功能的权限"; $("#toast").classList.add("visible"); setTimeout(() => $("#toast").classList.remove("visible"), 3200); }
}
boot().catch(() => { $("#recentApplications").innerHTML = '<div class="empty-state">概览载入失败，请刷新重试</div>'; });
