import { api, esc, renderIcons } from "/static/js/app.js";
import { DATA_VIZ_PALETTE, baseChartOption, cssVar, darkAxis, initChart, typeLabel } from "/static/js/theme.js";

const charts = [];

export async function render(root) {
  charts.splice(0).forEach((c) => c.dispose());
  // 骨架屏先行：数据到达前保持稳定布局，避免闪白
  root.innerHTML = `
    <header class="mb-6">
      <h1 class="text-2xl font-semibold tracking-tight">总览</h1>
      <p class="text-sm text-[var(--ml-ink-2)] mt-1">知识库实时状态</p>
    </header>
    <section class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7 gap-4 mb-6">
      ${'<div class="h-[96px] bg-[var(--ml-surface-2)] rounded-lg animate-pulse"></div>'.repeat(7)}
    </section>
    <section class="grid grid-cols-1 lg:grid-cols-2 gap-4">
      ${'<div class="h-[320px] bg-[var(--ml-surface-2)] rounded-lg animate-pulse"></div>'.repeat(4)}
    </section>`;
  let data;
  try {
    data = await api("/api/overview");
  } catch (err) {
    renderError(root, String(err.message || err));
    return;
  }
  renderCards(root, data);
  renderCharts(root, data);
  renderIcons();
  return () => charts.splice(0).forEach((c) => c.dispose());
}

function renderError(root, message) {
  root.innerHTML = `
    <header class="mb-6">
      <h1 class="text-2xl font-semibold tracking-tight">总览</h1>
      <p class="text-sm text-[var(--ml-ink-2)] mt-1">知识库实时状态</p>
    </header>
    <div class="bg-[var(--ml-card)] border border-[var(--ml-line)] rounded-lg p-8 max-w-md w-full mx-auto text-center">
      <i data-lucide="alert-triangle" class="w-10 h-10 text-[var(--ml-state-error)] mx-auto mb-4"></i>
      <h3 class="text-base font-medium mb-2">数据加载失败</h3>
      <p class="text-sm text-[var(--ml-ink-2)] mb-6">${esc(message)}</p>
      <button type="button" id="ov-retry" class="px-4 py-2 rounded-md text-sm font-medium bg-[var(--ml-brand)] text-[var(--ml-brand-ink)] hover:opacity-90 transition-opacity">重试</button>
    </div>`;
  root.querySelector("#ov-retry").addEventListener("click", () => render(root));
  renderIcons();
}

const CARD_CLASS = "bg-[var(--ml-card)] border border-[var(--ml-line)] rounded-lg p-5";
const CARD_BADGE = "text-xs px-2 py-0.5 rounded-full";

function renderCards(root, data) {
  const activeKeys = Object.values(data.keys).reduce((s, r) => s + (r.active || 0), 0);
  const pending = data.batches_pending;
  const running = data.reindex_tasks.running || 0;
  const embedOk = data.health.embedding;
  // 卡片右侧徽章：状态语义（overview 无时序数据，不做误导性增量展示）
  const cards = [
    ["clock", "待审批批次", pending,
      pending > 0 ? "bg-[var(--ml-state-warning)]/10 text-[var(--ml-state-warning)]" : "bg-[var(--ml-surface-2)] text-[var(--ml-ink-2)]",
      pending > 0 ? `${pending} 待处理` : "无待办"],
    ["key", "Key（active）", activeKeys,
      "bg-[var(--ml-surface-2)] text-[var(--ml-ink-2)]", `${Object.keys(data.keys).length} 角色`],
    ["globe", "System 域", data.systems.systems,
      "bg-[var(--ml-state-info)]/10 text-[var(--ml-state-info)]", "稳定"],
    ["folder-tree", "项目挂载", data.systems.project_mounts,
      "bg-[var(--ml-surface-2)] text-[var(--ml-ink-2)]", "—"],
    ["unlink", "孤儿节点", data.quality.orphan_nodes,
      data.quality.orphan_nodes > 0 ? "bg-[var(--ml-state-error)]/10 text-[var(--ml-state-error)]" : "bg-[var(--ml-state-success)]/10 text-[var(--ml-state-success)]",
      data.quality.orphan_nodes > 0 ? "待治理" : "健康"],
    ["refresh-cw", "重嵌 running", running,
      running > 0 ? "bg-[var(--ml-state-info)]/10 text-[var(--ml-state-info)]" : "bg-[var(--ml-surface-2)] text-[var(--ml-ink-2)]",
      running > 0 ? "运行中" : "空闲"],
    ["activity", "Embedding", embedOk ? "正常" : "不可达",
      embedOk ? "bg-[var(--ml-state-success)]/10 text-[var(--ml-state-success)]" : "bg-[var(--ml-state-error)]/10 text-[var(--ml-state-error)]",
      embedOk ? "健康" : "不可达"],
  ];
  const el = document.createElement("section");
  el.className = "grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7 gap-4 mb-6";
  el.innerHTML = cards
    .map(([icon, label, value, bcls, btxt]) => `
      <article class="${CARD_CLASS}">
        <div class="flex items-center gap-2 mb-3">
          <i data-lucide="${icon}" class="w-4 h-4 text-[var(--ml-brand)]"></i>
          <span class="text-xs font-medium text-[var(--ml-ink-2)]">${label}</span>
        </div>
        <div class="flex items-end justify-between">
          <span class="text-2xl font-semibold tabular-nums${label === "Embedding" && !embedOk ? " text-[var(--ml-state-error)]" : ""}">${esc(String(value))}</span>
          <span class="${CARD_BADGE} ${bcls}">${esc(btxt)}</span>
        </div>
      </article>`)
    .join("");
  root.querySelectorAll("section")[0].replaceWith(el);
}

function renderCharts(root, data) {
  const wrap = document.createElement("section");
  wrap.className = "grid grid-cols-1 lg:grid-cols-2 gap-4";
  // 图表容器用内联尺寸：Tailwind browser 编译晚于视图渲染，utility 高度未生效时
  // echarts.init 会拿到 0 高容器导致空白（首次进入必现），内联样式同步生效无时序问题
  const CHART_STYLE = "width:100%;height:256px;";
  wrap.innerHTML = `
    <article class="${CARD_CLASS}"><h3 class="text-sm font-medium mb-4">节点类型分布</h3><div id="chart-nodes" style="${CHART_STYLE}"></div></article>
    <article class="${CARD_CLASS}"><h3 class="text-sm font-medium mb-4">边类型分布</h3><div id="chart-edges" style="${CHART_STYLE}"></div></article>
    <article class="${CARD_CLASS}"><h3 class="text-sm font-medium mb-4">节点状态分布</h3><div id="chart-status" style="${CHART_STYLE}"></div></article>
    <article class="${CARD_CLASS}"><h3 class="text-sm font-medium mb-4">图谱质量</h3><div id="chart-quality" style="${CHART_STYLE}"></div></article>`;
  root.querySelectorAll("section")[1].replaceWith(wrap);

  donut(root.querySelector("#chart-nodes"), data.graph.nodes_by_type);
  donut(root.querySelector("#chart-edges"), data.graph.edges_by_type);
  bar(root.querySelector("#chart-status"), data.nodes_by_status);
  qualityBars(root.querySelector("#chart-quality"), data.quality);
}

function donut(el, kv) {
  const surface = cssVar("--ml-surface") || "#14161c";
  const base = baseChartOption();
  const chart = initChart(el);
  charts.push(chart);
  chart.setOption({
    ...base,
    tooltip: { ...base.tooltip, trigger: "item" },
    legend: { ...base.legend, bottom: 0 },
    series: [{
      type: "pie", radius: ["45%", "70%"], center: ["50%", "45%"],
      avoidLabelOverlap: false,
      itemStyle: { borderRadius: 6, borderColor: surface, borderWidth: 2 },
      label: { show: false },
      emphasis: {
        label: {
          show: true, fontSize: 14,
          color: cssVar("--ml-ink") || "#f0f1f5",
          formatter: "{b}\n{c} ({d}%)",
        },
      },
      // 节点类型 key 转中文展示（边类型无映射时 typeLabel 原样返回，安全）
      data: Object.entries(kv).map(([name, value]) => ({ name: typeLabel(name), value })),
    }],
  });
}

function bar(el, kv) {
  const chart = initChart(el);
  charts.push(chart);
  const entries = Object.entries(kv);
  chart.setOption({
    ...baseChartOption(),
    tooltip: { ...baseChartOption().tooltip, trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: "3%", right: "4%", bottom: "3%", top: "10%", containLabel: true },
    xAxis: { type: "category", data: entries.map(([k]) => k), ...darkAxis({ splitLine: { show: false } }) },
    yAxis: { type: "value", ...darkAxis() },
    series: [{
      type: "bar", barWidth: "40%",
      itemStyle: { borderRadius: [4, 4, 0, 0], color: DATA_VIZ_PALETTE[0] },
      data: entries.map(([, v]) => v),
    }],
  });
}

function qualityBars(el, q) {
  const chart = initChart(el);
  charts.push(chart);
  const rows = [
    ["孤儿节点", q.orphan_nodes],
    ["重复分组", q.duplicate_groups_count],
    ["连通分量", q.connected_components],
    ["总节点", q.total_nodes],
  ];
  chart.setOption({
    ...baseChartOption(),
    tooltip: { ...baseChartOption().tooltip, trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: "3%", right: "8%", bottom: "3%", top: "5%", containLabel: true },
    xAxis: { type: "value", ...darkAxis() },
    yAxis: { type: "category", data: rows.map(([k]) => k), ...darkAxis({ splitLine: { show: false } }) },
    series: [{
      type: "bar", barWidth: "50%",
      itemStyle: { borderRadius: [0, 4, 4, 0] },
      data: rows.map(([k, v], i) => ({
        value: v,
        itemStyle: { color: DATA_VIZ_PALETTE[(i + 1) % DATA_VIZ_PALETTE.length] },
        name: k,
      })),
    }],
  });
}
