import { api, esc, renderIcons } from "/static/js/app.js";

const CARD_CLASS = "bg-[var(--ml-card)] border border-[var(--ml-line)] rounded-lg p-5";
const BADGE = "text-xs px-2 py-0.5 rounded-full";
const PAGE_SIZE = 50;
const SELECT_CLASS =
  "px-3 py-2 bg-[var(--ml-input)] border border-[var(--ml-border)] rounded-md text-sm text-[var(--ml-ink)] focus:outline-none focus:ring-2 focus:ring-[var(--ml-ring)]";

const state = { role: "", status: "", limit: PAGE_SIZE, offset: 0 };

export async function render(root) {
  root.innerHTML = `
    <header class="mb-6">
      <h1 class="text-2xl font-semibold tracking-tight">用户</h1>
      <p class="text-sm text-[var(--ml-ink-2)] mt-1">Access Key 列表、scope 权限与使用统计</p>
    </header>
    <section id="keys-cards" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
      ${'<div class="h-[96px] bg-[var(--ml-surface-2)] rounded-lg animate-pulse"></div>'.repeat(4)}
    </section>
    <div class="flex flex-wrap items-center gap-3 mb-4">
      <select id="keys-role" class="${SELECT_CLASS}">
        <option value="">全部角色</option>
        <option value="admin">admin</option>
        <option value="pm">pm</option>
        <option value="dev">dev</option>
      </select>
      <select id="keys-status" class="${SELECT_CLASS}">
        <option value="">全部状态</option>
        <option value="active">active</option>
        <option value="revoked">revoked</option>
      </select>
    </div>
    <div class="bg-[var(--ml-card)] border border-[var(--ml-line)] rounded-lg overflow-x-auto">
      <table class="w-full text-[13px] border-collapse">
        <thead><tr>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">Key</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">角色</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">状态</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">审核模式</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">系统 scope</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">项目 scope</th>
          <th class="px-3.5 py-2.5 text-right text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">调用次数</th>
          <th class="px-3.5 py-2.5 text-right text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">错误</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">最后使用</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">创建时间</th>
        </tr></thead>
        <tbody id="keys-tbody"><tr><td colspan="10" class="px-3.5 py-8 text-center text-sm text-[var(--ml-ink-2)]">加载中…</td></tr></tbody>
      </table>
    </div>
    <div id="keys-pager" class="flex items-center justify-between mt-4"></div>`;

  root.querySelector("#keys-role").addEventListener("change", (e) => {
    state.role = e.target.value;
    state.offset = 0;
    load(root);
  });
  root.querySelector("#keys-status").addEventListener("change", (e) => {
    state.status = e.target.value;
    state.offset = 0;
    load(root);
  });

  await load(root);
  renderIcons();
}

async function load(root) {
  const params = new URLSearchParams({ limit: String(state.limit), offset: String(state.offset) });
  if (state.role) params.set("role", state.role);
  if (state.status) params.set("status", state.status);
  let data;
  try {
    data = await api(`/api/keys?${params}`);
  } catch (err) {
    root.querySelector("#keys-cards").innerHTML =
      `<p class="text-sm text-[var(--ml-state-error)] col-span-full">加载失败：${esc(String(err.message || err))}</p>`;
    root.querySelector("#keys-tbody").innerHTML =
      `<tr><td colspan="10" class="px-3.5 py-3 text-sm text-[var(--ml-state-error)]">加载失败：${esc(String(err.message || err))}</td></tr>`;
    root.querySelector("#keys-pager").innerHTML = "";
    return;
  }
  renderCards(root.querySelector("#keys-cards"), data.summary);
  renderTable(root.querySelector("#keys-tbody"), data.keys);
  renderPager(root, root.querySelector("#keys-pager"), data);
  renderIcons();
}

function renderCards(el, s) {
  const NEU = "bg-[var(--ml-surface-2)] text-[var(--ml-ink-2)]";
  const cards = [
    ["key-round", "Key 总数", s.total, NEU, ""],
    ["circle-check", "Active", s.active, "bg-[var(--ml-state-success)]/10 text-[var(--ml-state-success)]", ""],
    ["circle-slash", "Revoked", s.revoked,
      s.revoked > 0 ? "bg-[var(--ml-state-warning)]/10 text-[var(--ml-state-warning)]" : NEU, ""],
    ["activity", "近 24h 活跃", s.active_24h, NEU, "有工具调用"],
  ];
  el.innerHTML = cards
    .map(
      ([icon, label, value, bcls, btxt]) => `
      <article class="${CARD_CLASS}">
        <div class="flex items-center gap-2 mb-3">
          <i data-lucide="${icon}" class="w-4 h-4 text-[var(--ml-brand)]"></i>
          <span class="text-xs font-medium text-[var(--ml-ink-2)]">${label}</span>
        </div>
        <div class="flex items-end justify-between">
          <span class="text-2xl font-semibold tabular-nums">${esc(String(value))}</span>
          ${btxt ? `<span class="${BADGE} ${bcls}">${esc(btxt)}</span>` : ""}
        </div>
      </article>`
    )
    .join("");
}

function renderTable(tbody, keys) {
  if (!keys.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="px-3.5 py-10 text-center text-sm text-[var(--ml-ink-2)]">无匹配的 Access Key</td></tr>`;
    return;
  }
  const TD = "px-3.5 py-2.5 whitespace-nowrap border-b border-[var(--ml-line-subtle)]";
  const ROLE_BADGE = { admin: "bg-[var(--ml-brand-subtle)] text-[var(--ml-brand)]", pm: "bg-[var(--ml-state-info)]/10 text-[var(--ml-state-info)]", dev: "bg-[var(--ml-viz-codesnippet)]/20 text-[var(--ml-ink)]" };
  tbody.innerHTML = keys
    .map((k) => {
      const isActive = k.status === "active";
      const systemCell = scopeCell(k.scope_unlimited, k.scope.systems, (s) => s.name || s.code || sid(s.id));
      const projectCell = scopeCell(k.scope_unlimited, k.scope.projects, (p) => p.name || sid(p.id));
      const lastUsed = k.usage.last_used_at ? fmtTime(k.usage.last_used_at) : "—";
      return `<tr class="transition-colors hover:bg-[var(--ml-surface-2)]">
        <td class="${TD} font-mono" title="${esc(k.key_id)}">${esc(k.key_id.slice(0, 8))}…</td>
        <td class="${TD}"><span class="${BADGE} ${ROLE_BADGE[k.role] || "bg-[var(--ml-surface-2)] text-[var(--ml-ink-2)]"}">${esc(k.role)}</span></td>
        <td class="${TD}"><span class="${BADGE} ${isActive ? "bg-[var(--ml-state-success)]/10 text-[var(--ml-state-success)]" : "bg-[var(--ml-state-warning)]/10 text-[var(--ml-state-warning)]"}">${isActive ? "active" : "revoked"}</span></td>
        <td class="${TD}">${k.lax_mode ? '<span class="text-[var(--ml-state-warning)]">宽松</span>' : '<span class="text-[var(--ml-ink-2)]">严格</span>'}</td>
        <td class="${TD}">${systemCell}</td>
        <td class="${TD}">${projectCell}</td>
        <td class="${TD} font-mono text-right">${k.usage.total_calls}</td>
        <td class="${TD} font-mono text-right ${k.usage.error_calls > 0 ? "text-[var(--ml-state-error)]" : "text-[var(--ml-ink-2)]"}">${k.usage.error_calls}</td>
        <td class="${TD} font-mono text-[var(--ml-ink-2)]">${lastUsed}</td>
        <td class="${TD} font-mono text-[var(--ml-ink-2)]">${fmtTime(k.created_at)}</td>
      </tr>`;
    })
    .join("");
}

function scopeCell(unlimited, list, resolve) {
  if (unlimited) {
    return '<span class="text-[var(--ml-ink-3)]">全部</span>';
  }
  if (!list || !list.length) {
    return '<span class="text-[var(--ml-ink-3)]">未授权</span>';
  }
  return list.map((it) => {
    const label = resolve(it);
    return `<span class="inline-block px-1.5 py-0.5 mr-1 mb-1 rounded bg-[var(--ml-surface-2)] text-xs" title="${esc(label)}">${esc(label.length > 12 ? label.slice(0, 12) + "…" : label)}</span>`;
  }).join("");
}

function sid(id) {
  // 无名称时回退显示 id 短前缀
  return String(id || "").slice(0, 8) + "…";
}

function fmtTime(iso) {
  return String(iso).replace("T", " ").replace(/\..*$/, "");
}

function renderPager(root, el, data) {
  if (data.total === 0) {
    el.innerHTML = `<span class="text-xs text-[var(--ml-ink-3)]">共 0 条</span>`;
    return;
  }
  const start = data.offset + 1;
  const end = Math.min(data.offset + data.keys.length, data.total);
  const hasPrev = data.offset > 0;
  const hasNext = data.offset + data.keys.length < data.total;
  el.innerHTML = `
    <span class="text-xs text-[var(--ml-ink-3)]">第 ${start}..${end} 条 / 共 ${data.total} 条</span>
    <div class="flex gap-2">
      <button id="keys-prev" ${hasPrev ? "" : "disabled"} class="px-3 py-1.5 rounded-md text-sm border border-[var(--ml-border)] text-[var(--ml-ink-2)] disabled:opacity-40 disabled:cursor-not-allowed hover:bg-[var(--ml-surface-2)] transition-colors">上一页</button>
      <button id="keys-next" ${hasNext ? "" : "disabled"} class="px-3 py-1.5 rounded-md text-sm border border-[var(--ml-border)] text-[var(--ml-ink-2)] disabled:opacity-40 disabled:cursor-not-allowed hover:bg-[var(--ml-surface-2)] transition-colors">下一页</button>
    </div>`;
  const prevBtn = el.querySelector("#keys-prev");
  const nextBtn = el.querySelector("#keys-next");
  if (hasPrev) prevBtn.addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); load(root); });
  if (hasNext) nextBtn.addEventListener("click", () => { state.offset += state.limit; load(root); });
}