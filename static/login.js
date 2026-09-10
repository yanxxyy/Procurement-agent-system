const els = {
  form: document.querySelector("#loginForm"), username: document.querySelector("#username"), password: document.querySelector("#password"),
  toggle: document.querySelector("#togglePassword"), button: document.querySelector("#loginButton"), error: document.querySelector("#loginError"),
  banner: document.querySelector("#currentUserBanner"), currentName: document.querySelector("#currentUserName"),
};

const params = new URLSearchParams(location.search);
const candidateReturnTo = params.get("return_to") || "/";
const returnTo = candidateReturnTo.startsWith("/") && !candidateReturnTo.startsWith("//") ? candidateReturnTo : "/";

document.querySelectorAll(".account-option").forEach((option) => {
  option.addEventListener("click", () => {
    document.querySelectorAll(".account-option").forEach((item) => item.classList.remove("selected"));
    option.classList.add("selected");
    els.username.value = option.dataset.user;
    els.password.value = option.dataset.password;
    els.error.classList.remove("visible");
  });
});

els.toggle.addEventListener("click", () => {
  const shown = els.password.type === "text";
  els.password.type = shown ? "password" : "text";
  els.toggle.textContent = shown ? "显示" : "隐藏";
});

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.error.classList.remove("visible");
  if (!els.form.reportValidity()) return;
  els.button.disabled = true;
  els.button.innerHTML = "正在验证身份…";
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
      body: JSON.stringify({ username: els.username.value.trim(), password: els.password.value }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "登录失败");
    location.assign(returnTo);
  } catch (error) {
    els.error.textContent = error.message;
    els.error.classList.add("visible");
    els.button.disabled = false;
    els.button.innerHTML = "登录工作台 <span>→</span>";
  }
});

async function boot() {
  try {
    const response = await fetch("/api/auth/me", { credentials: "same-origin" });
    if (!response.ok) return;
    const user = await response.json();
    if (!params.has("switch")) {
      location.replace(returnTo);
      return;
    }
    els.currentName.textContent = `${user.name}（${user.role_label}）`;
    els.banner.hidden = false;
  } catch (_) {}
}
boot();
