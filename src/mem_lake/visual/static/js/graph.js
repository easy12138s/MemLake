import { api, esc } from "/static/js/app.js";

const TYPES = [
  "ProjectProfile", "Requirement", "CodeSnippet",
  "Solution", "DesignIntent", "Decision", "Pitfall",
];
const TYPE_COLORS = {
  ProjectProfile: "#5470c6", Requirement: "#91cc75", CodeSnippet: "#fac858",
  Solution: "#ee6666", DesignIntent: "#73c0de", Decision: "#3ba272",
  Pitfall: "#fc8451",
};
const EDGE_PALETTE = [
  "#9aa5b1", "#c4b5fd", "#f9a8d4", "#fcd34d", "#a7f3d0", "#bfdbfe",
  "#fecaca", "#ddd6fe", "#bbf7d0", "#fed7aa", "#cbd5e1", "#dbeafe",
];
const SIZES = [100, 300, 500, 1000];

const state = {
  systemId: "", status: "approved", q: "", limit: 500,
  types: new Set(TYPES),
  nodes: [], edges: [], truncated: false,
  chart: null, edgeColor: new Map(),
};

export async function render(root) {
  root.innerHTML = `
    <h2>知识图谱</h2>
    <div class="graph-toolbar">
      <select id="gf-system"><option value="">全部系统域</option></select>
      <select id="gf-status">
        <option value="approved">已生效</option>
        <option value="archived">已归档（软删除）</option>
        <option value="all">全部状态</option>
      </select>
      <select id="gf-limit">
        ${SIZES.map((s) => `<option value="${s}"${s === 500 ? " selected" : ""}>${s} 节点</option>`).join("")}
      </select>
      <span class="chips">
        ${TYPES.map((t) => `<label><input type="checkbox" class="gf-type" value="${t}" checked> ${t}</label>`).join("")}
      </span>
      <input id="gf-q" placeholder="搜索标题 / 需求主键（回车）">
      <button id="gf-apply">应用</button>
    </div>
    <p id="graph-notice" class="notice hidden"></p>
    <div class="graph-layout">
      <div id="graph-chart" class="graph-chart"></div>
      <aside id="graph-detail" class="graph-detail hidden"></aside>
    </div>`;
  bindToolbar(root);
  await loadSystems(root);
  await reload(root);
  return () => disposeChart();
}

function disposeChart() {
  if (state.chart) { state.chart.dispose(); state.chart = null; }
}

function bindToolbar(root) {
  root.querySelector("#gf-system").addEventListener("change", (e) => {
    state.systemId = e.target.value;
    reload(root);
  });
  root.querySelector("#gf-status").addEventListener("change", (e) => {
    state.status = e.target.value;
    reload(root);
  });
  root.querySelector("#gf-limit").addEventListener("change", (e) => {
    state.limit = Number(e.target.value);
    reload(root);
  });
  root.querySelectorAll(".gf-type").forEach((cb) =>
    cb.addEventListener("change", () => {
      state.types = new Set(
        [...root.querySelectorAll(".gf-type:checked")].map((c) => c.value)
      );
      reload(root);
    })
  );
  const qInput = root.querySelector("#gf-q");
  const applyQ = () => {
    state.q = qInput.value.trim();
    reload(root);
  };
  qInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") applyQ();
  });
  root.querySelector("#gf-apply").addEventListener("click", applyQ);
}

async function loadSystems(root) {
  try {
    const data = await api("/api/systems");
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
  const chartEl = root.querySelector("#graph-chart");
  disposeChart();
  if (!state.types.size) {
    state.nodes = [];
    state.edges = [];
    state.truncated = false;
    notice(root, false);
    chartEl.innerHTML = `<p class="empty">未选择任何节点类型</p>`;
    return;
  }
  const params = new URLSearchParams();
  if (state.systemId) params.set("system_id", state.systemId);
  if (state.types.size < TYPES.length) {
    params.set("types", [...state.types].join(","));
  }
  if (state.status !== "approved") params.set("status", state.status);
  if (state.q) params.set("q", state.q);
  params.set("limit", String(state.limit));
  chartEl.innerHTML = `<p class="loading">加载中…</p>`;
  const data = await api(`/api/graph?${params}`);
  state.nodes = data.nodes;
  state.edges = data.edges;
  state.truncated = data.truncated;
  notice(
    root,
    data.truncated,
    `结果已截断（超过 ${state.limit} 个节点），请收窄过滤条件`
  );
  if (!state.nodes.length) {
    chartEl.innerHTML = `<p class="empty">无匹配节点</p>`;
    return;
  }
  renderChart(root, chartEl);
}

function notice(root, truncated, text) {
  const el = root.querySelector("#graph-notice");
  el.classList.toggle("hidden", !truncated);
  if (truncated) el.textContent = text;
}

function renderChart(root, chartEl) {
  chartEl.innerHTML = "";
  state.chart = echarts.init(chartEl);
  const present = [...new Set(state.nodes.map((n) => n.type))];
  const categories = present.map((t) => ({ name: t }));
  const catIndex = new Map(present.map((t, i) => [t, i]));
  const edgeTypes = [...new Set(state.edges.map((e) => e.edge_type))].sort();
  state.edgeColor = new Map(
    edgeTypes.map((t, i) => [t, EDGE_PALETTE[i % EDGE_PALETTE.length]])
  );
  const highlight = Boolean(state.q);
  state.chart.setOption({
    tooltip: {
      confine: true,
      formatter: (p) =>
        p.dataType === "edge" ? edgeTip(p.data.raw) : nodeTip(p.data.raw),
    },
    legend: { data: present, bottom: 0, type: "scroll" },
    series: [
      {
        type: "graph",
        layout: "force",
        roam: true,
        draggable: true,
        categories,
        force: { repulsion: 140, edgeLength: [40, 140], gravity: 0.08 },
        label: { show: false },
        emphasis: { focus: "adjacency" },
        data: state.nodes.map((n) => ({
          id: n.id,
          name: n.title,
          category: catIndex.get(n.type),
          itemStyle: {
            color: TYPE_COLORS[n.type] || "#9aa5b1",
            borderColor: highlight ? "#d33" : "#fff",
            borderWidth: highlight ? 2 : 1,
          },
          raw: n,
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

function nodeTip(n) {
  const lines = [
    `<b>${esc(n.title)}</b>`,
    `类型：${esc(n.type)} · 状态：${esc(n.status)}`,
  ];
  if (n.requirement_key) lines.push(`需求主键：${esc(n.requirement_key)}`);
  if (n.vector_ready !== undefined) {
    lines.push(`向量：${n.vector_ready ? "就绪" : "未生成"}`);
  }
  if (n.content_preview) {
    lines.push(`<div class="tip-preview">${esc(n.content_preview)}…</div>`);
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

async function showDetail(root, id) {
  const panel = root.querySelector("#graph-detail");
  panel.classList.remove("hidden");
  panel.innerHTML = `<p class="loading">加载详情…</p>`;
  let n;
  try {
    n = await api(`/api/node/${id}`);
  } catch (err) {
    panel.innerHTML = `<p class="error">${esc(String(err.message || err))}</p>`;
    return;
  }
  panel.innerHTML = detailHtml(n);
  panel.querySelectorAll("[data-neighbor]").forEach((el) =>
    el.addEventListener("click", () => showDetail(root, el.dataset.neighbor))
  );
}

function detailHtml(n) {
  const props = Object.keys(n.properties || {}).length
    ? `<pre>${esc(JSON.stringify(n.properties, null, 2))}</pre>`
    : `<p class="muted">（无）</p>`;
  const tags = (n.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join(" ") ||
    `<span class="muted">（无）</span>`;
  const edges = n.edges.length
    ? `<table><tbody>${n.edges
        .map((e) => {
          const other = e.source === n.id ? e.target : e.source;
          const dir = e.source === n.id ? "→" : "←";
          return `<tr><td>${dir}</td><td class="link" data-neighbor="${esc(other)}">${esc(other.slice(0, 8))}…</td><td>${esc(e.edge_type)}</td></tr>`;
        })
        .join("")}</tbody></table>`
    : `<p class="muted">（无边）</p>`;
  const neighbors = n.neighbors.length
    ? `<ul>${n.neighbors
        .map(
          (nb) =>
            `<li class="link" data-neighbor="${esc(nb.id)}">${esc(nb.title)} <span class="muted">(${esc(nb.type)})</span></li>`
        )
        .join("")}</ul>`
    : `<p class="muted">（无邻居）</p>`;
  return `
    <h3>${esc(n.title)}</h3>
    <p class="muted">${esc(n.type)} · ${esc(n.status)} · v${n.version} · 向量${n.vector_ready ? "就绪" : "未生成"}</p>
    ${n.requirement_key ? `<p>需求主键：<b>${esc(n.requirement_key)}</b></p>` : ""}
    <h4>正文</h4>
    <pre class="detail-content">${esc(n.content)}</pre>
    <h4>属性</h4>
    ${props}
    <h4>标签</h4>
    <p>${tags}</p>
    <h4>关联边</h4>
    ${edges}
    <h4>一跳邻居</h4>
    ${neighbors}`;
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
  renderChart(root, root.querySelector("#graph-chart"));
}
