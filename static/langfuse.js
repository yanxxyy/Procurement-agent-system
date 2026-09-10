const elements = {
  form: document.querySelector("#connectForm"),
  connectView: document.querySelector("#connectView"),
  connectedView: document.querySelector("#connectedView"),
  region: document.querySelector("#regionSelect"),
  baseUrl: document.querySelector("#baseUrl"),
  publicKey: document.querySelector("#publicKey"),
  secretKey: document.querySelector("#secretKey"),
  toggleSecret: document.querySelector("#toggleSecret"),
  connectButton: document.querySelector("#connectButton"),
  error: document.querySelector("#connectError"),
  directConsole: document.querySelector("#directConsole"),
  connectionDot: document.querySelector("#connectionDot"),
  connectionLabel: document.querySelector("#connectionLabel"),
  projectName: document.querySelector("#projectName"),
  organizationName: document.querySelector("#organizationName"),
  connectedHost: document.querySelector("#connectedHost"),
  maskedKey: document.querySelector("#maskedKey"),
  latestTraceId: document.querySelector("#latestTraceId"),
  openTraceButton: document.querySelector("#openTraceButton"),
  testTraceButton: document.querySelector("#testTraceButton"),
  disconnectButton: document.querySelector("#disconnectButton"),
  toast: document.querySelector("#toast"),
};

function setState(type, label) {
  elements.connectionDot.className = `lf-state-dot ${type || ""}`;
  elements.connectionLabel.textContent = label;
}

function showError(message) {
  elements.error.textContent = message;
  elements.error.classList.add("visible");
}

function clearError() {
  elements.error.textContent = "";
  elements.error.classList.remove("visible");
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  window.setTimeout(() => elements.toast.classList.remove("visible"), 3000);
}

function detailFrom(responseBody, fallback) {
  if (typeof responseBody?.detail === "string") return responseBody.detail;
  if (Array.isArray(responseBody?.detail)) return responseBody.detail.map((item) => item.msg).join("；");
  return fallback;
}

function showConnected(status) {
  elements.connectView.hidden = true;
  elements.connectedView.hidden = false;
  setState("connected", "已连接");
  elements.projectName.textContent = status.project_name || "LangFuse Project";
  elements.organizationName.textContent = status.organization_name || "—";
  elements.connectedHost.textContent = status.base_url || "—";
  elements.maskedKey.textContent = status.public_key_masked || "—";
  elements.latestTraceId.textContent = status.trace_id || "等待首条 Trace";
  elements.openTraceButton.href = status.trace_url || "/langfuse/open";
}

function showConnectForm(status = {}) {
  elements.connectView.hidden = false;
  elements.connectedView.hidden = true;
  setState("", "等待配置");
  if (status.base_url) {
    elements.baseUrl.value = status.base_url;
    elements.directConsole.href = status.base_url;
  }
}

elements.region.addEventListener("change", () => {
  if (elements.region.value !== "custom") elements.baseUrl.value = elements.region.value;
  elements.baseUrl.focus();
  elements.directConsole.href = elements.baseUrl.value;
});

elements.baseUrl.addEventListener("input", () => {
  try {
    const url = new URL(elements.baseUrl.value);
    if (["http:", "https:"].includes(url.protocol)) elements.directConsole.href = url.origin;
  } catch (_) {
    elements.directConsole.href = "#";
  }
});

elements.toggleSecret.addEventListener("click", () => {
  const visible = elements.secretKey.type === "text";
  elements.secretKey.type = visible ? "password" : "text";
  elements.toggleSecret.textContent = visible ? "显示" : "隐藏";
  elements.toggleSecret.setAttribute("aria-label", visible ? "显示 Secret Key" : "隐藏 Secret Key");
});

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  if (!elements.form.reportValidity()) return;
  setState("loading", "正在验证");
  elements.connectButton.disabled = true;
  elements.connectButton.querySelector("span").textContent = "正在验证项目凭据…";
  try {
    const response = await fetch("/api/langfuse/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        base_url: elements.baseUrl.value.trim(),
        public_key: elements.publicKey.value.trim(),
        secret_key: elements.secretKey.value.trim(),
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(detailFrom(body, `连接失败（HTTP ${response.status}）`));
    elements.secretKey.value = "";
    showConnected(body);
    showToast("LangFuse 项目验证成功，测试 Trace 已写入");
  } catch (error) {
    setState("", "连接失败");
    showError(error.message);
  } finally {
    elements.connectButton.disabled = false;
    elements.connectButton.querySelector("span").textContent = "验证并接入 LangFuse";
  }
});

elements.testTraceButton.addEventListener("click", async () => {
  elements.testTraceButton.disabled = true;
  elements.testTraceButton.textContent = "正在提交测试 Trace…";
  try {
    const response = await fetch("/api/langfuse/test-trace", { method: "POST", credentials: "same-origin" });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(detailFrom(body, "测试 Trace 提交失败"));
    elements.latestTraceId.textContent = body.trace_id;
    elements.openTraceButton.href = body.trace_url;
    showToast("测试 Trace 已写入 LangFuse");
  } catch (error) {
    showToast(error.message);
  } finally {
    elements.testTraceButton.disabled = false;
    elements.testTraceButton.textContent = "发送一条测试 Trace";
  }
});

elements.disconnectButton.addEventListener("click", async () => {
  elements.disconnectButton.disabled = true;
  try {
    await fetch("/api/langfuse/connection", { method: "DELETE", credentials: "same-origin" });
    elements.publicKey.value = "";
    elements.secretKey.value = "";
    showConnectForm({ base_url: elements.baseUrl.value });
    showToast("已断开 LangFuse 项目");
  } finally {
    elements.disconnectButton.disabled = false;
  }
});

async function boot() {
  try {
    const response = await fetch("/api/langfuse/status", { credentials: "same-origin" });
    if (response.status === 401) { window.location.replace("/login?return_to=/langfuse"); return; }
    if (response.status === 403) { window.location.replace("/?forbidden=langfuse"); return; }
    const status = await response.json();
    if (!status.requires_api_key && status.direct_url && !status.connected) {
      window.location.replace(status.direct_url);
      return;
    }
    status.connected ? showConnected(status) : showConnectForm(status);
  } catch (_) {
    showConnectForm();
    showError("无法读取本地接入状态，请确认 FastAPI 服务正在运行。");
  }
}

boot();
