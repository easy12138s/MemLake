const $ = (sel) => document.querySelector(sel);
export async function api(path, opts = {}) {
  const resp = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (resp.status === 401) { showLogin(); throw new Error("未认证"); }
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
  return body;
}

export function esc(s) {
  const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(s).replace(/[&<>"']/g, (c) => map[c]);
}

function showLogin() { $("#login-view").classList.remove("hidden"); $("#app-view").classList.add("hidden"); }
function showApp() { $("#login-view").classList.add("hidden"); $("#app-view").classList.remove("hidden"); }

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("#login-username").value, password: $("#login-password").value }),
    }).then(async (r) => { if (!r.ok) throw new Error((await r.json()).error || "登录失败"); });
    await boot();
  } catch (err) { $("#login-error").textContent = String(err.message || err); }
});

$("#logout-btn").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  cleanupCurrentView();
  $("#view-root").innerHTML = "";
  showLogin();
});

const views = {
  dashboard: () => import("/static/js/dashboard.js").then((m) => m.render($("#view-root"))),
  graph: () => import("/static/js/graph.js").then((m) => m.render($("#view-root"))),
};

let currentCleanup = null;

function cleanupCurrentView() {
  if (currentCleanup) { currentCleanup(); currentCleanup = null; }
}

async function route() {
  const name = (location.hash || "#/dashboard").replace("#/", "");
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  const root = $("#view-root");
  cleanupCurrentView();
  root.innerHTML = "";
  try {
    const result = await (views[name] || views.dashboard)();
    if (typeof result === "function") currentCleanup = result;
  } catch (err) {
    if (String(err.message || err) !== "未认证") {
      root.innerHTML = `<p class="error">视图加载失败：${esc(String(err.message || err))}</p>`;
    }
  }
}

window.addEventListener("hashchange", route);

async function boot() {
  try { await api("/api/me"); } catch { return; }
  showApp(); await route();
}
boot();
