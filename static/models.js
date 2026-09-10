const state = { providers: [] };
const els = {
  grid: document.querySelector("#providerGrid"), providerCount: document.querySelector("#providerCount"), configuredCount: document.querySelector("#configuredCount"), modelCount: document.querySelector("#modelCount"),
  avatar: document.querySelector("#sidebarAvatar"), name: document.querySelector("#sidebarUserName"), role: document.querySelector("#sidebarUserRole"), toast: document.querySelector("#toast"),
};
function escapeHtml(value) { const node = document.createElement("div"); node.textContent = value == null ? "" : String(value); return node.innerHTML; }
function showToast(message) { els.toast.textContent = message; els.toast.classList.add("visible"); window.setTimeout(() => els.toast.classList.remove("visible"), 2800); }

function setUser(user) { document.body.classList.remove("auth-pending"); els.avatar.textContent = user.name.slice(0,1); els.name.textContent = user.name; els.role.textContent = `${user.department} · ${user.role_label}`; }
function providerCard(provider) {
  if (provider.id === "local") return `
    <article class="provider-card configured"><header class="provider-head"><div class="provider-title"><span class="provider-logo">LOCAL</span><div><h2>${escapeHtml(provider.name)}</h2><p>${escapeHtml(provider.description)}</p></div></div><span class="provider-state ready">始终可用</span></header><div class="local-body"><i>AI</i><strong>无需配置凭据</strong><p>当外部厂商未配置或调用失败时，对话会自动回退到本地采购引擎。</p></div></article>`;
  const keyHint = provider.configured ? `已配置 ${escapeHtml(provider.api_key_masked || "环境变量密钥")}，留空则继续复用` : "首次配置该厂商时必须填写";
  return `
    <article class="provider-card ${provider.configured ? "configured" : ""}" data-provider-id="${escapeHtml(provider.id)}">
      <header class="provider-head"><div class="provider-title"><span class="provider-logo">${escapeHtml(provider.short_name.slice(0,6).toUpperCase())}</span><div><h2>${escapeHtml(provider.name)}</h2><p>${escapeHtml(provider.description)}</p></div></div><span class="provider-state ${provider.available ? "ready" : ""}">${provider.available ? "可用于对话" : provider.configured ? "已停用" : "待配置"}</span></header>
      <form class="provider-body provider-form">
        <div class="provider-fields">
          <label class="provider-field"><span>API Base URL</span><input class="provider-url" type="url" required value="${escapeHtml(provider.base_url)}" /></label>
          <label class="provider-field"><span>厂商 API Key</span><div class="key-input-wrap"><input class="provider-key" type="password" autocomplete="new-password" placeholder="${provider.configured ? "无需重复输入" : "输入 API Key"}" /><button class="toggle-key" type="button">显示</button></div><p class="field-hint">${keyHint}</p></label>
        </div>
        <div class="model-choice"><div class="model-choice-heading"><span>此厂商启用的模型</span><small>共用上方同一密钥</small></div><div class="model-checks">${provider.models.map((model) => `<label class="model-check"><input type="checkbox" name="model" value="${escapeHtml(model.id)}" ${model.enabled ? "checked" : ""} /><span><strong>${escapeHtml(model.name)}</strong><small>${escapeHtml(model.description)} · ${escapeHtml(model.id)}</small></span></label>`).join("")}</div></div>
        <div class="provider-actions"><label><input class="provider-enabled" type="checkbox" ${provider.enabled ? "checked" : ""} />允许员工在对话中选择</label><div class="provider-buttons">${provider.credential_source === "saved" ? '<button class="provider-remove" type="button">清除配置</button>' : ""}<button class="provider-save" type="submit">保存厂商配置</button></div></div>
      </form>
    </article>`;
}
function render() {
  els.providerCount.textContent = state.providers.length;
  els.configuredCount.textContent = state.providers.filter((provider) => provider.id !== "local" && provider.configured).length;
  els.modelCount.textContent = state.providers.filter((provider) => provider.available).flatMap((provider) => provider.models.filter((model) => model.enabled)).length;
  els.grid.innerHTML = state.providers.map(providerCard).join("");
  els.grid.querySelectorAll(".toggle-key").forEach((button) => button.addEventListener("click", () => { const input = button.previousElementSibling; const visible = input.type === "text"; input.type = visible ? "password" : "text"; button.textContent = visible ? "显示" : "隐藏"; }));
  els.grid.querySelectorAll(".provider-form").forEach((form) => form.addEventListener("submit", saveProvider));
  els.grid.querySelectorAll(".provider-remove").forEach((button) => button.addEventListener("click", removeProvider));
}
async function saveProvider(event) {
  event.preventDefault(); const card = event.currentTarget.closest(".provider-card"); const id = card.dataset.providerId; const button = card.querySelector(".provider-save");
  const enabledModels = [...card.querySelectorAll('input[name="model"]:checked')].map((input) => input.value);
  if (!enabledModels.length) { showToast("请至少启用一个模型"); return; }
  button.disabled = true; button.textContent = "保存中…";
  const key = card.querySelector(".provider-key").value.trim();
  const response = await fetch(`/api/models/providers/${encodeURIComponent(id)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, credentials: "same-origin", body: JSON.stringify({ base_url: card.querySelector(".provider-url").value.trim(), api_key: key || null, enabled_models: enabledModels, enabled: card.querySelector(".provider-enabled").checked }) });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) { button.disabled = false; button.textContent = "保存厂商配置"; showToast(body.detail || "保存失败"); return; }
  showToast(body.message); await loadProviders();
}
async function removeProvider(event) {
  const card = event.currentTarget.closest(".provider-card"); const id = card.dataset.providerId;
  if (!window.confirm("确认清除此厂商保存的 API Key 和模型设置？")) return;
  event.currentTarget.disabled = true;
  const response = await fetch(`/api/models/providers/${encodeURIComponent(id)}`, { method: "DELETE", credentials: "same-origin" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) { showToast(body.detail || "清除失败"); event.currentTarget.disabled = false; return; }
  showToast(body.message); await loadProviders();
}
async function loadProviders() {
  const response = await fetch("/api/models/providers", { credentials: "same-origin" });
  if (response.status === 401) { location.replace("/login?return_to=/models"); return; }
  if (response.status === 403) { location.replace("/?forbidden=models"); return; }
  if (!response.ok) { els.grid.innerHTML = '<div class="providers-loading">模型配置读取失败，请刷新后重试。</div>'; return; }
  state.providers = (await response.json()).providers || []; render();
}
async function boot() {
  const userResponse = await fetch("/api/auth/me", { credentials: "same-origin" });
  if (userResponse.status === 401) { location.replace("/login?return_to=/models"); return; }
  const user = await userResponse.json(); if (user.role !== "admin") { location.replace("/?forbidden=models"); return; }
  setUser(user); await loadProviders();
}
boot();
