import { api, esc, renderIcons } from "/static/js/app.js";
import { baseChartOption, darkAxis, initChart } from "/static/js/theme.js";

const WINDOWS = ["1h", "6h", "24h"];
const TAIL_REFRESH_MS = 10000;

const CARD_CLASS = "bg-[var(--ml-card)] border border-[var(--ml-line)] rounded-lg p-5";
const BADGE = "text-xs px-2 py-0.5 rounded-full";

const state = { window: "1h", chartMap: new Map(), timer: null };

export async function render(root) {
  root.innerHTML = `
    <header class="mb-6">
      <h1 class="text-2xl font-semibold tracking-tight">监控</h1>
      <p class="text-sm text-[var(--ml-ink-2)] mt-1">网关与 embedding 服务运行指标</p>
    </header>
    <section id="mon-cards" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7 gap-4 mb-6">
      ${'<div class="h-[96px] bg-[var(--ml-surface-2)] rounded-lg animate-pulse"></div>'.repeat(7)}
    </section>
    <div class="flex flex-wrap items-center gap-3 mb-4">
      <b class="text-sm font-medium text-[var(--ml-ink-2)]">趋势窗口</b>
      <span class="inline-flex p-[3px] gap-0.5 rounded-lg bg-[var(--ml-surface-2)]">
        ${WINDOWS.map((w) => `<button class="mon-win px-3.5 py-1 rounded-md text-[13px] transition-colors${w === state.window ? " bg-[var(--ml-brand)] text-white" : " text-[var(--ml-ink-2)] hover:text-[var(--ml-ink)]"}" data-w="${w}">${w}</button>`).join("")}
      </span>
      <span id="mon-series-note" class="text-xs text-[var(--ml-ink-3)]"></span>
    </div>
    <section class="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-2">
      <article class="${CARD_CLASS}"><h3 class="text-sm font-medium mb-4">工具调用速率（次/分钟）</h3><div class="chart" style="width:100%;height:256px;"></div></article>
      <article class="${CARD_CLASS}"><h3 class="text-sm font-medium mb-4">工具耗时分位（ms）</h3><div class="chart" style="width:100%;height:256px;"></div></article>
    </section>
    <h3 class="text-base font-semibold mt-6 mb-3">最近工具调用 <span class="text-xs font-normal text-[var(--ml-ink-3)]">（每 ${TAIL_REFRESH_MS / 1000}s 自动刷新）</span></h3>
    <div class="bg-[var(--ml-card)] border border-[var(--ml-line)] rounded-lg overflow-x-auto">
      <table class="w-full text-[13px] border-collapse">
        <thead><tr>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">时间</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">操作者</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">工具</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">状态</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">耗时(ms)</th>
          <th class="px-3.5 py-2.5 text-left text-xs font-medium text-[var(--ml-ink-2)] bg-[var(--ml-surface-2)] whitespace-nowrap">说明</th>
        </tr></thead>
        <tbody id="mon-tail"><tr><td colspan="6" class="px-3.5 py-8 text-center text-sm text-[var(--ml-ink-2)]">加载中…</td></tr></tbody>
      </table>
    </div>
    <p class="text-xs text-[var(--ml-ink-3)] mt-3">说明：指标为进程生命周期累计值 + 控制台采样历史（60s 间隔 / 24h 保留）；网关或控制台重启后趋势清零。</p>`;
  root.querySelectorAll(".mon-win").forEach((btn) =>
    btn.addEventListener("click", () => {
      state.window = btn.dataset.w;
      root.querySelectorAll(".mon-win").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("bg-[var(--ml-brand)]", on);
        b.classList.toggle("text-white", on);
        b.classList.toggle("text-[var(--ml-ink-2)]", !on);
      });
      loadSeries(root);
    })
  );
  await Promise.all([loadSnapshot(root), loadSeries(root), loadTail(root)]);
  state.timer = setInterval(() => loadTail(root), TAIL_REFRESH_MS);
  renderIcons();
  return () => cleanup();
}

function cleanup() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  state.chartMap.forEach((c) => c.dispose());
  state.chartMap.clear();
}

async function loadSnapshot(root) {
  const el = root.querySelector("#mon-cards");
  let data;
  try {
    data = await api("/api/monitor/snapshot");
  } catch (err) {
    el.innerHTML = `<p class="text-sm text-[var(--ml-state-error)] col-span-full">快照加载失败：${esc(String(err.message || err))}</p>`;
    return;
  }
  const gw = data.gateway;
  const es = data.embed_service;
  const fmtMs = (v) => (v === null || v === undefined ? "—" : `${v}`);
  const NEU = "bg-[var(--ml-surface-2)] text-[var(--ml-ink-2)]";
  const cards = [
    ["activity", "工具调用（累计）", gw.tool_calls.total, NEU, `${Object.keys(gw.tool_calls.by_tool || {}).length} 工具`],
    ["check-circle-2", "成功 / 失败", `${gw.tool_calls.success} / ${gw.tool_calls.error}`,
      gw.tool_calls.error > 0 ? "bg-[var(--ml-state-error)]/10 text-[var(--ml-state-error)]" : "bg-[var(--ml-state-success)]/10 text-[var(--ml-state-success)]",
      gw.tool_calls.error > 0 ? "有失败" : "全部成功"],
    ["timer", "耗时 p95", fmtMs(gw.tool_duration_ms.p95), NEU, "ms"],
    ["cpu", "Embedding 调用", `${gw.embedding_calls.embed} / 精排 ${gw.embedding_calls.rerank}`, NEU, "embed / rerank"],
    ["shuffle", "Rerank 降级", gw.rerank_fallback,
      gw.rerank_fallback > 0 ? "bg-[var(--ml-state-warning)]/10 text-[var(--ml-state-warning)]" : NEU, gw.rerank_fallback > 0 ? "曾降级" : "无"],
    ["stamp", "审批 approved/rejected", `${gw.approval_batches.approved} / ${gw.approval_batches.rejected}`, NEU, "累计"],
    ["server", "Embedding 服务", es ? "在线" : "不可达",
      es ? "bg-[var(--ml-state-success)]/10 text-[var(--ml-state-success)]" : "bg-[var(--ml-state-error)]/10 text-[var(--ml-state-error)]",
      es ? "健康" : "不可达"],
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
          <span class="text-2xl font-semibold tabular-nums${label === "Embedding 服务" && !es ? " text-[var(--ml-state-error)]" : ""}">${esc(String(value))}</span>
          <span class="${BADGE} ${bcls}">${esc(btxt)}</span>
        </div>
      </article>`
    )
    .join("") + (es ? "" : `
    <div class="col-span-full px-3 py-2 rounded-md text-[13px] bg-[var(--ml-state-warning)]/10 text-[var(--ml-state-warning)] border border-[var(--ml-state-warning)]/25">
      embedding 服务不可达：其指标已降级隐藏，恢复后自动展示。
    </div>`);
  renderIcons();
}

async function loadSeries(root) {
  const note = root.querySelector("#mon-series-note");
  let data;
  try {
    data = await api(`/api/monitor/series?window=${state.window}`);
  } catch (err) {
    note.textContent = `趋势加载失败：${String(err.message || err)}`;
    return;
  }
  note.textContent = data.sampler_running
    ? `${data.series.gw_tool_calls_per_min ? data.series.gw_tool_calls_per_min.length : 0} 个采样点`
    : "采样器未启动（趋势为空）";
  const chartEls = root.querySelectorAll(".chart");
  lineChart(chartEls[0], {
    "调用速率": data.series.gw_tool_calls_per_min || [],
  }, "次/min");
  lineChart(chartEls[1], {
    "p50": data.series.gw_tool_p50_ms || [],
    "p95": data.series.gw_tool_p95_ms || [],
    "p99": data.series.gw_tool_p99_ms || [],
  }, "ms");
  renderIcons();
}

function lineChart(el, series, unit) {
  // 同元素重复渲染（窗口切换）先销毁旧实例，避免 ECharts 叠加告警
  const prev = state.chartMap.get(el);
  if (prev) { prev.dispose(); state.chartMap.delete(el); }
  el.innerHTML = "";
  const names = Object.keys(series).filter((n) => series[n].length);
  if (!names.length) {
    el.innerHTML = `<div class="w-full h-full flex flex-col items-center justify-center gap-3">
      <i data-lucide="chart-line" class="w-10 h-10 text-[var(--ml-ink-3)]"></i>
      <p class="text-sm text-[var(--ml-ink-2)]">暂无采样数据（采样器刚启动或窗口内无数据）</p>
    </div>`;
    return;
  }
  const base = baseChartOption();
  const chart = initChart(el);
  state.chartMap.set(el, chart);
  chart.setOption({
    ...base,
    tooltip: { ...base.tooltip, trigger: "axis", valueFormatter: (v) => `${v} ${unit}` },
    legend: { ...base.legend, bottom: 0 },
    grid: { left: 50, right: 20, top: 20, bottom: 40 },
    xAxis: { type: "time", ...darkAxis({ splitLine: { show: false } }) },
    yAxis: { type: "value", scale: true, ...darkAxis() },
    series: names.map((n, i) => ({
      name: n, type: "line", showSymbol: false, smooth: false,
      connectNulls: true, data: series[n],
      lineStyle: { width: i === 0 ? 2 : 1.5 },
    })),
  });
}

async function loadTail(root) {
  const tbody = root.querySelector("#mon-tail");
  let data;
  try {
    data = await api("/api/monitor/tail?limit=50");
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" class="px-3.5 py-3 text-sm text-[var(--ml-state-error)]">流水加载失败：${esc(String(err.message || err))}</td></tr>`;
    return;
  }
  if (!data.calls.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="px-3.5 py-10 text-center text-sm text-[var(--ml-ink-2)]">暂无工具调用记录</td></tr>`;
    return;
  }
  const TD = "px-3.5 py-2.5 whitespace-nowrap border-b border-[var(--ml-line-subtle)]";
  tbody.innerHTML = data.calls
    .map((c) => {
      const bad = c.result_status === "error";
      const note = c.error_message
        ? `<span class="text-[var(--ml-state-error)]" title="${esc(c.error_message)}">${esc(String(c.error_message).slice(0, 60))}</span>`
        : (c.operation_id ? `<span class="font-mono text-xs text-[var(--ml-ink-3)]">${esc(c.operation_id.slice(0, 12))}…</span>` : "");
      return `<tr class="transition-colors hover:bg-[var(--ml-surface-2)]">
        <td class="${TD} font-mono">${esc(c.created_at.replace("T", " ").replace(/\..*$/, ""))}</td>
        <td class="${TD} font-mono">${esc(c.actor.slice(0, 8))}…</td>
        <td class="${TD}">${esc(c.tool_name || "")}</td>
        <td class="${TD}"><span class="${BADGE} ${bad ? "bg-[var(--ml-state-error)]/10 text-[var(--ml-state-error)]" : "bg-[var(--ml-state-success)]/10 text-[var(--ml-state-success)]"}">${bad ? "error" : "success"}</span></td>
        <td class="${TD} font-mono">${c.duration_ms === null || c.duration_ms === undefined ? "—" : c.duration_ms}</td>
        <td class="${TD} whitespace-normal max-w-[360px]">${note}</td>
      </tr>`;
    })
    .join("");
}
