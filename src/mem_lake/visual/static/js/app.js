const $ = (sel) => document.querySelector(sel);
export async function api(path, opts = {}) {
  const resp = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (resp.status === 401) { showLogin(); throw new Error("未认证"); }
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
  return body;
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

$("#logout-btn").addEventListener("click", async () => { await fetch("/api/logout", { method: "POST" }); showLogin(); });

const views = { dashboard: () => import("/static/js/dashboard.js").then((m) => m.render($("#view-root"))) };
async function route() {
  const name = (location.hash || "#/dashboard").replace("#/", "");
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  await (views[name] || views.dashboard)();
}
window.addEventListener("hashchange", route);

async function boot() {
  try { await api("/api/me"); } catch { return; }
  showApp(); await route();
}
boot();
