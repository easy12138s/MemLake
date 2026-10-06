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

// 视图内动态插入的 data-lucide 节点渲染为 SVG（lucide UMD 全局）
export function renderIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function showLogin() { $("#login-view").classList.remove("hidden"); $("#app-view").classList.add("hidden"); }
function showApp() { $("#login-view").classList.add("hidden"); $("#app-view").classList.remove("hidden"); }

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errEl = $("#login-error");
  errEl.classList.add("hidden");
  errEl.textContent = "";
  try {
    await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("#login-username").value, password: $("#login-password").value }),
    }).then(async (r) => { if (!r.ok) throw new Error((await r.json()).error || "登录失败"); });
    await boot();
  } catch (err) {
    errEl.textContent = String(err.message || err);
    errEl.classList.remove("hidden");
  }
});

$("#logout-btn").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  cleanupCurrentView();
  $("#view-root").innerHTML = "";
  $("#nav-user").textContent = "";
  showLogin();
});

const views = {
  dashboard: () => import("/static/js/dashboard.js").then((m) => m.render($("#view-root"))),
  graph: () => import("/static/js/graph.js").then((m) => m.render($("#view-root"))),
  monitor: () => import("/static/js/monitor.js").then((m) => m.render($("#view-root"))),
  keys: () => import("/static/js/keys.js").then((m) => m.render($("#view-root"))),
};

let currentCleanup = null;

function cleanupCurrentView() {
  if (currentCleanup) { currentCleanup(); currentCleanup = null; }
}

async function route() {
  const name = (location.hash || "#/dashboard").replace("#/", "");
  document.querySelectorAll(".nav-tabs a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  const root = $("#view-root");
  cleanupCurrentView();
  root.innerHTML = "";
  // 图页全屏（无内边距），其余页正常留白
  root.classList.toggle("fullscreen", name === "graph");
  try {
    const result = await (views[name] || views.dashboard)();
    if (typeof result === "function") currentCleanup = result;
  } catch (err) {
    if (String(err.message || err) !== "未认证") {
      root.innerHTML = `<div class="bg-[var(--ml-card)] border border-[var(--ml-line)] rounded-lg p-8 max-w-md w-full mx-auto mt-16 text-center">
        <i data-lucide="alert-triangle" class="w-10 h-10 text-[var(--ml-state-error)] mx-auto mb-4"></i>
        <h3 class="text-base font-medium mb-2">视图加载失败</h3>
        <p class="text-sm text-[var(--ml-ink-2)]">${esc(String(err.message || err))}</p>
      </div>`;
      renderIcons();
    }
  }
}

window.addEventListener("hashchange", route);

async function boot() {
  let me;
  try { me = await api("/api/me"); } catch { return; }
  $("#nav-user").textContent = me.username;
  showApp(); await route();
}
renderIcons();
boot();
