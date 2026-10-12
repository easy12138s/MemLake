// 知识图谱列表视图：按 系统域 → 项目 两级分组，需求为顶点、关联资产为子行。
// 由 graph.js 调用（renderList），数据来自 /api/graph/tree。

import { esc } from "/static/js/app.js";
import { TYPE_COLORS } from "/static/js/theme.js";

export function renderList(el, data, ctx) {
  const systemNames = new Map(ctx.systems.map((s) => [s.id, s.name]));
  const projects = new Map(data.projects.map((p) => [p.id, p.title]));
  const assets = new Map(data.assets.map((a) => [a.id, a]));

  // 需求 → 关联资产列表（links 双向匹配：a 为资产侧时挂到 b 需求侧；
  // 需求-需求边两侧都不在 assets 中，自然跳过）
  const linksByReq = new Map();
  for (const l of data.links) {
    for (const [a, b] of [[l.source, l.target], [l.target, l.source]]) {
      if (!assets.has(a)) continue;
      if (!linksByReq.has(b)) linksByReq.set(b, []);
      linksByReq.get(b).push({ asset: assets.get(a), edgeType: l.edge_type });
    }
  }

  // 系统域 → 项目 → 需求 三级桶（空 key = 未挂载）
  const tree = new Map();
  for (const req of data.requirements) {
    const sysKey = req.system_id || "";
    if (!tree.has(sysKey)) tree.set(sysKey, new Map());
    const byProject = tree.get(sysKey);
    const projKey = req.project_id || "";
    if (!byProject.has(projKey)) byProject.set(projKey, []);
    byProject.get(projKey).push(req);
  }

  el.innerHTML = `<div style="position:absolute;inset:0;overflow-y:auto;padding:16px 0;">
    ${[...tree.entries()]
      .map(([sysKey, byProject]) => systemHtml(sysKey, byProject, systemNames, projects, linksByReq, ctx))
      .join("")}
  </div>`;

  // 标题点击 → 打开详情抽屉；preventDefault 阻断 summary 默认开合
  //（点标题看详情、点箭头/空白处才展开子行）。用 onclick 属性赋值：
  // 翻页重复渲染时天然覆盖，不会像 addEventListener 累积监听器
  el.onclick = (e) => {
    const target = e.target.closest("[data-open]");
    if (!target) return;
    e.preventDefault();
    ctx.openDetail(target.dataset.open);
  };
}

function systemHtml(sysKey, byProject, systemNames, projects, linksByReq, ctx) {
  const name = sysKey
    ? (systemNames.get(sysKey) || `未知系统域（${sysKey.slice(0, 8)}…）`)
    : "未挂载系统域";
  const count = [...byProject.values()].reduce((s, r) => s + r.length, 0);
  return `
    <details open class="mx-6 mb-4 group">
      <summary class="flex items-center gap-2 px-3 py-2 rounded-md cursor-pointer select-none bg-[var(--ml-surface)] border border-[var(--ml-line)] text-sm font-medium hover:bg-[var(--ml-surface-2)] transition-colors">
        <i data-lucide="globe" class="w-4 h-4 text-[var(--ml-brand)]"></i>
        <span>${esc(name)}</span>
        <span class="ml-auto text-xs font-normal text-[var(--ml-ink-3)]">${count} 需求</span>
      </summary>
      <div class="mt-2 ml-4 space-y-3">
        ${[...byProject.entries()]
          .map(([projKey, reqs]) => projectHtml(projKey, reqs, projects, linksByReq, ctx))
          .join("")}
      </div>
    </details>`;
}

function projectHtml(projKey, reqs, projects, linksByReq, ctx) {
  const title = projKey ? (projects.get(projKey)?.title || `未知项目（${projKey.slice(0, 8)}…）`) : "悬浮需求（无项目）";
  return `
    <details open>
      <summary class="flex items-center gap-2 px-3 py-1.5 rounded-md cursor-pointer select-none bg-[var(--ml-surface-2)] text-[13px] hover:bg-[var(--ml-surface-3)] transition-colors">
        <i data-lucide="folder" class="w-3.5 h-3.5 text-[var(--ml-state-warning)]"></i>
        <span>${esc(title)}</span>
        <span class="ml-auto text-xs text-[var(--ml-ink-3)]">${reqs.length}</span>
      </summary>
      <div class="mt-1 ml-3 space-y-1">
        ${reqs.map((req) => requirementHtml(req, linksByReq, ctx)).join("")}
      </div>
    </details>`;
}

function requirementHtml(req, linksByReq, ctx) {
  const linked = linksByReq.get(req.id) || [];
  const assetRows = linked.length
    ? linked.map(
        ({ asset, edgeType }) => `
        <li class="flex items-center gap-2 pl-8 pr-3 py-1.5 rounded-md text-[13px] hover:bg-[var(--ml-surface-2)] transition-colors">
          <span class="w-1.5 h-1.5 rounded-full flex-none" style="background:${TYPE_COLORS[asset.type] || "#9aa5b1"}"></span>
          <span class="link" data-open="${esc(asset.id)}">${esc(asset.title)}</span>
          <span class="px-1.5 py-0.5 rounded text-[11px] font-medium bg-[var(--ml-surface-3)] text-[var(--ml-ink-2)]">${ctx.typeLabel(asset.type)}</span>
          <span class="ml-auto text-xs text-[var(--ml-ink-3)]">${esc(edgeType)}</span>
        </li>`
      ).join("")
    : `<li class="pl-8 pr-3 py-1 text-xs text-[var(--ml-ink-3)]">（无关联资产）</li>`;
  return `
    <details>
      <summary class="flex items-center gap-2 px-3 py-2 rounded-md cursor-pointer select-none hover:bg-[var(--ml-surface-2)] transition-colors">
        <i data-lucide="chevron-right" class="w-3.5 h-3.5 text-[var(--ml-ink-3)] flex-none group-open:rotate-90 transition-transform"></i>
        <span class="text-sm link" data-open="${esc(req.id)}">${esc(req.title)}</span>
        ${req.requirement_key ? `<span class="font-mono text-xs text-[var(--ml-ink-3)]">${esc(req.requirement_key)}</span>` : ""}
        <span class="px-1.5 py-0.5 rounded text-[11px] font-medium ${req.status === "approved" ? "bg-[var(--ml-state-success)]/10 text-[var(--ml-state-success)]" : "bg-[var(--ml-surface-3)] text-[var(--ml-ink-2)]"}">${req.status === "approved" ? "已生效" : esc(req.status)}</span>
        <span class="ml-auto text-xs text-[var(--ml-ink-3)]">关联 ${linked.length}</span>
      </summary>
      <ul class="mt-0.5 space-y-0.5">${assetRows}</ul>
    </details>`;
}
