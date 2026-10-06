import { api, esc, renderIcons } from "/static/js/app.js";
import { TYPE_COLORS, baseChartOption, initChart, typeLabel } from "/static/js/theme.js";
import { renderList } from "/static/js/graph-list.js";

const TYPES = [
  "ProjectProfile", "Requirement", "CodeSnippet",
  "Solution", "DesignIntent", "Decision", "Pitfall",
];
const EDGE_PALETTE = [
  "#9aa5b1", "#c4b5fd", "#f9a8d4", "#fcd34d", "#a7f3d0", "#bfdbfe",
  "#fecaca", "#ddd6fe", "#bbf7d0", "#fed7aa", "#cbd5e1", "#dbeafe",
];
const SIZES = [100, 300, 500, 1000];
// 节点 label 显示阈值：超过后全图隐藏文字（力导向图密集成团时 label 只会互相覆盖）
const LABEL_MAX_NODES = 150;
// 列表模式每页需求条数（后端 graph_tree 上限 200）
const LIST_PAGE_SIZE = 100;

const FIELD_CLASS = "h-9 px-3 rounded-md text-sm border bg-[var(--ml-surface-2)] text-[var(--ml-ink)] focus:outline-none focus:ring-2 focus:ring-ring";

const state = {
  view: "graph", // graph=网图 list=列表
  systemId: "", status: "approved", q: "", limit: 300, offset: 0,
  types: new Set(TYPES),
  nodes: [], edges: [], total: 0,
  chart: null, edgeColor: new Map(),
  systems: [], // [{id, name}] 系统域名映射（列表分组用）
};

export async function render(root) {
  root.innerHTML = `
    <section class="flex flex-wrap items-center gap-3 px-6 py-3 border-b bg-[var(--ml-surface)] border-[var(--ml-line)]">
      <span class="inline-flex p-[3px] gap-0.5 rounded-lg bg-[var(--ml-surface-2)]" id="gv-switch">
        <button data-view="graph" class="gv-btn px-3.5 py-1 rounded-md text-[13px] transition-colors">网图</button>
        <button data-view="list" class="gv-btn px-3.5 py-1 rounded-md text-[13px] transition-colors">列表</button>
      </span>
      <select id="gf-system" class="${FIELD_CLASS}"><option value="">全部系统域</option></select>
      <select id="gf-status" class="${FIELD_CLASS}">
        <option value="approved">已生效</option>
        <option value="archived">已归档</option>
        <option value="all">全部状态</option>
      </select>
      <select id="gf-limit" class="${FIELD_CLASS}">
        ${SIZES.map((s) => `<option value="${s}"${s === state.limit ? " selected" : ""}>${s} 节点</option>`).join("")}
      </select>
      <span class="flex flex-wrap items-center gap-2" id="gf-types">
        ${TYPES.map((t) => `
          <label class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs border cursor-pointer select-none border-[var(--ml-input)] bg-[var(--ml-surface-2)] text-[var(--ml-ink)]">
            <input type="checkbox" class="gf-type accent-[var(--ml-brand)]" value="${t}"${state.types.has(t) ? " checked" : ""}>
            <span>${typeLabel(t)}</span>
          </label>`).join("")}
      </span>
      <div class="flex-1 min-w-[12rem] max-w-md">
        <input id="gf-q" type="text" placeholder="搜索标题 / 需求主键（回车）" class="w-full ${FIELD_CLASS} placeholder:text-[var(--ml-ink-3)]">
      </div>
      <button id="gf-apply" type="button"
        class="h-9 px-4 rounded-md text-sm font-medium text-[var(--ml-primary-foreground)] bg-[var(--ml-primary)] hover:opacity-90 transition-opacity">应用</button>
    </section>
    <p id="graph-notice" class="hidden mx-6 mt-3 px-3 py-2 rounded-md text-[13px] bg-[var(--ml-state-warning)]/10 text-[var(--ml-state-warning)] border border-[var(--ml-state-warning)]/25"></p>
    <section class="flex-1 relative min-h-0 bg-[var(--ml-background)]">
      <div id="graph-content" class="absolute inset-0" style="position:absolute;inset:0;"></div>
      <div id="graph-legend" class="absolute bottom-4 left-4 p-3 rounded-lg border bg-[var(--ml-surface)] border-[var(--ml-line)]" style="box-shadow: var(--ml-shadow-popover); position:absolute;">
        <p class="text-xs font-medium text-[var(--ml-ink-2)] mb-2">节点类型</p>
        <ul class="space-y-1.5">
          ${TYPES.map((t) => `<li class="flex items-center gap-2 text-xs text-[var(--ml-ink)]"><span class="w-2.5 h-2.5 rounded-full" style="background: ${TYPE_COLORS[t]}"></span>${typeLabel(t)}</li>`).join("")}
        </ul>
      </div>
      <button id="g-reset" type="button" title="重置视图（缩放与平移复位）"
        class="absolute top-3 right-3 z-10 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] text-[var(--ml-ink-2)] hover:text-[var(--ml-ink)] bg-[var(--ml-surface)] border border-[var(--ml-line)] transition-colors hidden">
        <i data-lucide="locate-fixed" class="w-3.5 h-3.5"></i>重置视图
      </button>
      <aside id="graph-detail" class="absolute right-0 top-0 bottom-0 w-96 max-w-[90vw] z-20 border-l overflow-y-auto transition-transform duration-300 translate-x-full bg-[var(--ml-surface)] border-[var(--ml-line)]" style="box-shadow: var(--ml-shadow-popover);">
        <div class="sticky top-0 z-10 flex items-center justify-between px-5 py-3 border-b bg-[var(--ml-surface)] border-[var(--ml-line)]">
          <h3 class="text-base font-semibold">节点详情</h3>
          <button id="detail-close" type="button" title="关闭"
            class="inline-flex items-center justify-center w-7 h-7 rounded-md text-[var(--ml-ink-2)] hover:text-[var(--ml-ink)] hover:bg-[var(--ml-surface-2)] transition-colors">
            <i data-lucide="x" class="w-4 h-4"></i>
          </button>
        </div>
        <div id="detail-body" class="p-5 space-y-5"></div>
      </aside>
    </section>
    <footer id="g-pagination" class="hidden flex items-center justify-between gap-3 px-6 py-2.5 border-t bg-[var(--ml-surface)] border-[var(--ml-line)]">
      <span id="g-page-info" class="text-xs text-[var(--ml-ink-2)]"></span>
      <span class="flex items-center gap-2">
        <button id="g-prev" type="button" class="h-7 px-3 rounded-md text-[13px] text-[var(--ml-ink-2)] hover:text-[var(--ml-ink)] hover:bg-[var(--ml-surface-2)] border border-[var(--ml-line)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-[var(--ml-ink-2)]">上一页</button>
        <button id="g-next" type="button" class="h-7 px-3 rounded-md text-[13px] text-[var(--ml-ink-2)] hover:text-[var(--ml-ink)] hover:bg-[var(--ml-surface-2)] border border-[var(--ml-line)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-[var(--ml-ink-2)]">下一页</button>
      </span>
    </footer>`;
  bindToolbar(root);
  root.querySelector("#detail-close").addEventListener("click", closeDetail);
  root.querySelector("#g-prev").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - pageSize());
    reload(root);
  });
  root.querySelector("#g-next").addEventListener("click", () => {
    if (state.offset + pageSize() < state.total) {
      state.offset += pageSize();
      reload(root);
    }
  });
  root.querySelector("#g-reset").addEventListener("click", resetView);
  await loadSystems(root);
  await reload(root);
  renderIcons();
  return () => disposeChart();
}

function pageSize() {
  return state.view === "list" ? LIST_PAGE_SIZE : state.limit;
}

function disposeChart() {
  if (state.chart) { state.chart.dispose(); state.chart = null; }
}

// 视图与缩放复位（网图 roam 平移/缩放后一键回到初始状态）
function resetView() {
  if (!state.chart) return;
  state.chart.setOption({ series: [{ zoom: 1, center: ["50%", "50%"] }] });
}

function bindToolbar(root) {
  root.querySelectorAll(".gv-btn").forEach((btn) =>
    btn.addEventListener("click", () => {
      if (state.view === btn.dataset.view) return;
      state.view = btn.dataset.view;
      state.offset = 0; // 两种视图的 limit 语义不同（节点数 vs 需求数），切换重置页码
      syncToolbar(root);
      reload(root);
    })
  );
  root.querySelector("#gf-system").addEventListener("change", (e) => {
    state.systemId = e.target.value;
    state.offset = 0;
    reload(root);
  });
  root.querySelector("#gf-status").addEventListener("change", (e) => {
    state.status = e.target.value;
    state.offset = 0;
    reload(root);
  });
  root.querySelector("#gf-limit").addEventListener("change", (e) => {
    state.limit = Number(e.target.value);
    state.offset = 0;
    reload(root);
  });
  root.querySelectorAll(".gf-type").forEach((cb) =>
    cb.addEventListener("change", () => {
      state.types = new Set(
        [...root.querySelectorAll(".gf-type:checked")].map((c) => c.value)
      );
      state.offset = 0;
      reload(root);
    })
  );
  const qInput = root.querySelector("#gf-q");
  const applyQ = () => {
    state.q = qInput.value.trim();
    state.offset = 0;
    reload(root);
  };
  qInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") applyQ();
  });
  root.querySelector("#gf-apply").addEventListener("click", applyQ);
}

// 视图切换后同步工具栏可见性（列表模式隐藏节点数/类型筛选/图例——树以需求为中心）
function syncToolbar(root) {
  const isGraph = state.view === "graph";
  root.querySelector("#gf-limit").classList.toggle("hidden", !isGraph);
  root.querySelector("#gf-types").classList.toggle("hidden", !isGraph);
  root.querySelector("#g-reset").classList.toggle("hidden", !isGraph);
  root.querySelector("#graph-legend").classList.toggle("hidden", !isGraph);
  root.querySelectorAll(".gv-btn").forEach((b) => {
    const on = b.dataset.view === state.view;
    b.classList.toggle("bg-[var(--ml-brand)]", on);
    b.classList.toggle("text-white", on);
    b.classList.toggle("text-[var(--ml-ink-2)]", !on);
  });
}

async function loadSystems(root) {
  try {
    const data = await api("/api/systems");
    state.systems = data.systems;
    const sel = root.querySelector("#gf-system");
    data.systems.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.name;
      sel.appendChild(opt);
    });
  } catch {
    /* 下拉加载失败不阻塞图渲染（其余过滤条件仍可用） */
  }
}

async function reload(root) {
  const contentEl = root.querySelector("#graph-content");
  disposeChart();
  closeDetail();
  contentEl.innerHTML = `<div class="absolute inset-0 flex flex-col items-center justify-center gap-3">
    <div class="w-8 h-8 border-2 border-t-transparent rounded-full animate-spin" style="border-color: var(--ml-brand); border-top-color: transparent;"></div>
    <p class="text-sm text-[var(--ml-ink-2)]">加载中…</p>
  </div>`;
  const ok = state.view === "list"
    ? await reloadList(root, contentEl)
    : await reloadNetwork(root, contentEl);
  if (ok) renderPagination(root);
  renderIcons();
}

function renderPagination(root) {
  const footer = root.querySelector("#g-pagination");
  const size = pageSize();
  const pages = Math.max(1, Math.ceil(state.total / size));
  if (pages <= 1 && state.total <= size) {
    footer.classList.add("hidden");
    return;
  }
  footer.classList.remove("hidden");
  const page = Math.floor(state.offset / size) + 1;
  root.querySelector("#g-page-info").textContent =
    `共 ${state.total} 条 · 第 ${page}/${pages} 页`;
  root.querySelector("#g-prev").disabled = state.offset <= 0;
  root.querySelector("#g-next").disabled = state.offset + size >= state.total;
}

// ============================== 列表视图 ==============================

async function reloadList(root, contentEl) {
  const params = new URLSearchParams();
  if (state.systemId) params.set("system_id", state.systemId);
  if (state.status !== "approved") params.set("status", state.status);
  if (state.q) params.set("q", state.q);
  params.set("limit", String(LIST_PAGE_SIZE));
  params.set("offset", String(state.offset));
  let data;
  try {
    data = await api(`/api/graph/tree?${params}`);
  } catch (err) {
    contentEl.innerHTML = emptyHtml("alert-triangle", `加载失败：${esc(String(err.message || err))}`);
    state.total = 0;
    return false;
  }
  state.total = data.total;
  if (!data.requirements.length) {
    contentEl.innerHTML = emptyHtml("search-x", "无匹配需求");
    return true;
  }
  renderList(contentEl, data, {
    systems: state.systems,
    typeLabel,
    openDetail: (id) => showDetail(root, id),
  });
  return true;
}

// ============================== 网图视图 ==============================

async function reloadNetwork(root, contentEl) {
  if (!state.types.size) {
    state.nodes = [];
    state.edges = [];
    state.total = 0;
    contentEl.innerHTML = emptyHtml("filter-x", "未选择任何节点类型");
    return true;
  }
  const params = new URLSearchParams();
  if (state.systemId) params.set("system_id", state.systemId);
  if (state.types.size < TYPES.length) {
    params.set("types", [...state.types].join(","));
  }
  if (state.status !== "approved") params.set("status", state.status);
  if (state.q) params.set("q", state.q);
  params.set("limit", String(state.limit));
  params.set("offset", String(state.offset));
  let data;
  try {
    data = await api(`/api/graph?${params}`);
  } catch (err) {
    contentEl.innerHTML = emptyHtml("alert-triangle", `加载失败：${esc(String(err.message || err))}`);
    state.total = 0;
    return false;
  }
  state.nodes = data.nodes;
  state.edges = data.edges;
  state.total = data.total;
  if (!state.nodes.length) {
    contentEl.innerHTML = emptyHtml("search-x", "无匹配节点", true);
    bindClearFilter(root);
    return true;
  }
  renderChart(root, contentEl);
  return true;
}

function emptyHtml(icon, text, clearable = false) {
  return `<div class="absolute inset-0 flex flex-col items-center justify-center gap-3">
    <i data-lucide="${icon}" class="w-10 h-10 text-[var(--ml-ink-3)]"></i>
    <p class="text-sm text-[var(--ml-ink-2)]">${text}</p>
    ${clearable ? '<button id="empty-clear" type="button" class="px-3 py-1.5 rounded-md text-sm text-[var(--ml-primary-foreground)] bg-[var(--ml-primary)] hover:opacity-90 transition-opacity">清除筛选</button>' : ""}
  </div>`;
}

function bindClearFilter(root) {
  const btn = root.querySelector("#empty-clear");
  if (!btn) return;
  btn.addEventListener("click", () => {
    state.systemId = "";
    state.status = "approved";
    state.q = "";
    state.offset = 0;
    state.types = new Set(TYPES);
    root.querySelector("#gf-system").value = "";
    root.querySelector("#gf-status").value = "approved";
    root.querySelector("#gf-q").value = "";
    root.querySelectorAll(".gf-type").forEach((c) => { c.checked = true; });
    reload(root);
  });
}

// 布局参数随节点规模自适应（斥力/边长递减避免大图挤成一团或过度飞散）
function forceParams(n) {
  if (n <= 150) return { repulsion: 200, edgeLength: [50, 150], gravity: 0.08 };
  if (n <= 500) return { repulsion: 120, edgeLength: [40, 120], gravity: 0.06 };
  return { repulsion: 80, edgeLength: [30, 100], gravity: 0.05 };
}

function renderChart(root, contentEl) {
  contentEl.innerHTML = "";
  state.chart = initChart(contentEl);
  const present = [...new Set(state.nodes.map((n) => n.type))];
  const categories = present.map((t) => ({ name: typeLabel(t) }));
  const catIndex = new Map(present.map((t, i) => [t, i]));
  const edgeTypes = [...new Set(state.edges.map((e) => e.edge_type))].sort();
  state.edgeColor = new Map(
    edgeTypes.map((t, i) => [t, EDGE_PALETTE[i % EDGE_PALETTE.length]])
  );
  const highlight = Boolean(state.q);
  const showLabel = state.nodes.length <= LABEL_MAX_NODES;
  const n = state.nodes.length;
  const force = forceParams(n);
  // 发光效果仅小中图开启：大图（>500）shadowBlur 渲染开销显著
  const glow = n <= 500;
  state.chart.setOption({
    ...baseChartOption(),
    tooltip: {
      confine: true,
      ...baseChartOption().tooltip,
      formatter: (p) =>
        p.dataType === "edge" ? edgeTip(p.data.raw) : nodeTip(p.data.raw),
    },
    series: [
      {
        type: "graph",
        layout: "force",
        roam: true, // 平移（空白处拖拽）+ 滚轮缩放
        zoom: 1,
        scaleLimit: { min: 0.2, max: 5 },
        draggable: true,
        categories,
        // 力导向布局动画（节点散开收拢的动态过程）：中小图开启提升观感，
        // 仅 1000 档大图关闭防帧率崩塌
        force: { ...force, layoutAnimation: n <= 500 },
        edgeSymbol: ["none", "arrow"], // 边方向箭头
        edgeSymbolSize: 6,
        label: {
          show: showLabel,
          color: cssInk(),
          fontSize: 11,
          position: "right",
          distance: 3,
        },
        emphasis: { focus: "adjacency" },
        data: state.nodes.map((node) => ({
          id: node.id,
          name: node.title,
          category: catIndex.get(node.type),
          symbolSize: node.type === "ProjectProfile" ? 26 : 14,
          itemStyle: {
            color: TYPE_COLORS[node.type] || "#9aa5b1",
            borderColor: highlight ? "#ef4444" : "#0b0c0f",
            borderWidth: highlight ? 2 : 1.5,
            shadowBlur: glow ? 8 : 0,
            shadowColor: TYPE_COLORS[node.type] || "#9aa5b1",
          },
          raw: node,
        })),
        links: state.edges.map((e) => ({
          source: e.source,
          target: e.target,
          lineStyle: {
            color: state.edgeColor.get(e.edge_type) || "#9aa5b1",
            width: 1.2,
            curveness: 0.1,
          },
          raw: e,
        })),
      },
    ],
  });
  state.chart.on("click", (p) => {
    if (p.dataType === "node") showDetail(root, p.data.id);
  });
  state.chart.on("dblclick", (p) => {
    if (p.dataType === "node") expandNode(root, p.data.id);
  });
}

function cssInk() {
  return getComputedStyle(document.documentElement).getPropertyValue("--ml-ink").trim() || "#f0f1f5";
}

function nodeTip(node) {
  const lines = [
    `<b>${esc(node.title)}</b>`,
    `类型：${typeLabel(node.type)} · 状态：${esc(node.status)}`,
  ];
  if (node.requirement_key) lines.push(`需求主键：${esc(node.requirement_key)}`);
  if (node.vector_ready !== undefined) {
    lines.push(`向量：${node.vector_ready ? "就绪" : "未生成"}`);
  }
  if (node.content_preview) {
    lines.push(`<div style="max-width:280px;max-height:5em;overflow:hidden;color:#a0a8b8">${esc(node.content_preview)}…</div>`);
  }
  return lines.join("<br>");
}

function edgeTip(e) {
  const by =
    e.properties && e.properties.created_by
      ? ` · by ${esc(e.properties.created_by)}`
      : "";
  return `关系：${esc(e.edge_type)}${by}`;
}

// ============================== 详情抽屉（两视图共用） ==============================

function closeDetail() {
  // Tailwind v4 的 translate-x-full 走独立 translate 属性，直接切类最可靠
  document.querySelectorAll("#graph-detail").forEach((d) => d.classList.add("translate-x-full"));
}

async function showDetail(root, id) {
  const panel = root.querySelector("#graph-detail");
  const body = root.querySelector("#detail-body");
  panel.classList.remove("translate-x-full");
  body.innerHTML = `<div class="flex flex-col items-center justify-center gap-3 py-12">
    <div class="w-8 h-8 border-2 border-t-transparent rounded-full animate-spin" style="border-color: var(--ml-brand); border-top-color: transparent;"></div>
    <p class="text-sm text-[var(--ml-ink-2)]">加载详情…</p>
  </div>`;
  let n;
  try {
    n = await api(`/api/node/${id}`);
  } catch (err) {
    body.innerHTML = `<p class="text-sm text-[var(--ml-state-error)]">${esc(String(err.message || err))}</p>`;
    return;
  }
  body.innerHTML = detailHtml(n);
  body.querySelectorAll("[data-neighbor]").forEach((el) =>
    el.addEventListener("click", () => showDetail(root, el.dataset.neighbor))
  );
  renderIcons();
}

function detailHtml(n) {
  const typeColor = TYPE_COLORS[n.type] || "#6e7681";
  const statusBadge = n.status === "approved"
    ? '<span class="px-2 py-0.5 rounded text-xs font-medium text-white bg-[var(--ml-state-success)]">已生效</span>'
    : `<span class="px-2 py-0.5 rounded text-xs font-medium bg-[var(--ml-surface-2)] text-[var(--ml-ink-2)]">${esc(n.status)}</span>`;
  const props = Object.keys(n.properties || {}).length
    ? `<pre class="p-3 rounded-md text-xs font-mono overflow-x-auto bg-[var(--ml-surface-2)] text-[var(--ml-ink)]">${esc(JSON.stringify(n.properties, null, 2))}</pre>`
    : `<p class="text-sm text-[var(--ml-ink-2)]">（无）</p>`;
  const tags = (n.tags || []).length
    ? n.tags.map((t) => `<span class="px-2 py-0.5 rounded text-xs border bg-[var(--ml-surface-2)] border-[var(--ml-line)] text-[var(--ml-ink-2)]">${esc(t)}</span>`).join(" ")
    : `<span class="text-xs text-[var(--ml-ink-3)]">（无）</span>`;
  const edges = n.edges.length
    ? `<ul class="space-y-1">${n.edges
        .map((e) => {
          const other = e.source === n.id ? e.target : e.source;
          const dir = e.source === n.id ? "→" : "←";
          return `<li class="flex items-center justify-between text-sm py-1 border-b border-[var(--ml-line-subtle)]">
            <span class="text-[var(--ml-ink-2)]">${dir} <span class="link font-mono" data-neighbor="${esc(other)}">${esc(other.slice(0, 8))}…</span></span>
            <span class="text-xs text-[var(--ml-ink-3)]">${esc(e.edge_type)}</span>
          </li>`;
        })
        .join("")}</ul>`
    : `<p class="text-xs text-[var(--ml-ink-3)]">（无边）</p>`;
  const neighbors = n.neighbors.length
    ? `<ul class="space-y-1">${n.neighbors
        .map(
          (nb) =>
            `<li class="text-sm"><span class="link" data-neighbor="${esc(nb.id)}">${esc(nb.title)}</span> <span class="text-xs text-[var(--ml-ink-3)]">(${typeLabel(nb.type)})</span></li>`
        )
        .join("")}</ul>`
    : `<p class="text-xs text-[var(--ml-ink-3)]">（无邻居）</p>`;
  return `
    <div>
      <h2 class="text-lg font-semibold leading-snug">${esc(n.title)}</h2>
      <div class="mt-2 flex flex-wrap items-center gap-2">
        <span class="px-2 py-0.5 rounded text-xs font-semibold text-[#0b0c0f]" style="background:${typeColor}">${typeLabel(n.type)}</span>
        ${statusBadge}
        <span class="px-2 py-0.5 rounded text-xs font-medium bg-[var(--ml-surface-2)] text-[var(--ml-ink-2)]">v${n.version}</span>
        <span class="px-2 py-0.5 rounded text-xs font-medium ${n.vector_ready ? "bg-[var(--ml-state-success)]/10 text-[var(--ml-state-success)]" : "bg-[var(--ml-surface-2)] text-[var(--ml-ink-2)]"}">向量${n.vector_ready ? "就绪" : "未生成"}</span>
      </div>
    </div>
    ${n.requirement_key ? `<div class="space-y-1"><p class="text-xs text-[var(--ml-ink-3)]">需求主键</p><p class="text-sm font-mono">${esc(n.requirement_key)}</p></div>` : ""}
    <div class="space-y-1"><p class="text-xs text-[var(--ml-ink-3)]">正文</p><pre class="p-3 rounded-md text-xs font-mono overflow-x-auto max-h-56 overflow-y-auto bg-[var(--ml-surface-2)] text-[var(--ml-ink-2)] whitespace-pre-wrap break-all">${esc(n.content)}</pre></div>
    <div class="space-y-1"><p class="text-xs text-[var(--ml-ink-3)]">属性</p>${props}</div>
    <div class="space-y-1"><p class="text-xs text-[var(--ml-ink-3)]">标签</p><p class="flex flex-wrap gap-1.5">${tags}</p></div>
    <div class="space-y-1"><p class="text-xs text-[var(--ml-ink-3)]">关联边</p>${edges}</div>
    <div class="space-y-1"><p class="text-xs text-[var(--ml-ink-3)]">一跳邻居</p>${neighbors}</div>`;
}

async function expandNode(root, id) {
  let n;
  try {
    n = await api(`/api/node/${id}`);
  } catch {
    return; // 双击扩展失败静默（点击详情面板仍可用）
  }
  const known = new Set(state.nodes.map((x) => x.id));
  const fresh = n.neighbors.filter((nb) => !known.has(nb.id));
  if (!fresh.length) return;
  state.nodes = [
    ...state.nodes,
    ...fresh.map((nb) => ({
      id: nb.id,
      type: nb.type,
      title: nb.title,
      status: nb.status,
      requirement_key: null,
      content_preview: "",
      vector_ready: undefined,
    })),
  ];
  const knownNow = new Set(state.nodes.map((x) => x.id));
  const edgeSet = new Set(
    state.edges.map((e) => `${e.source}|${e.target}|${e.edge_type}`)
  );
  for (const e of n.edges) {
    const key = `${e.source}|${e.target}|${e.edge_type}`;
    if (knownNow.has(e.source) && knownNow.has(e.target) && !edgeSet.has(key)) {
      state.edges.push(e);
      edgeSet.add(key);
    }
  }
  renderChart(root, root.querySelector("#graph-content"));
}
