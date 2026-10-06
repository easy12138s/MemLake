import { api } from "/static/js/app.js";

const charts = [];

export async function render(root) {
  charts.splice(0).forEach((c) => c.dispose());
  root.innerHTML = `
    <h2>总览</h2>
    <div id="ov-cards" class="cards"></div>
    <div class="charts">
      <div id="chart-nodes" class="chart"></div>
      <div id="chart-edges" class="chart"></div>
    </div>`;
  const data = await api("/api/overview");
  const activeKeys = Object.values(data.keys).reduce((s, r) => s + (r.active || 0), 0);
  const cards = [
    ["待审批批次", data.batches_pending],
    ["Key（active）", activeKeys],
    ["System 域", data.systems.systems],
    ["项目挂载", data.systems.project_mounts],
    ["孤儿节点", data.quality.orphan_nodes],
    ["重嵌 running", data.reindex_tasks.running || 0],
    ["Embedding", data.health.embedding ? "正常" : "不可达"],
  ];
  root.querySelector("#ov-cards").innerHTML = cards
    .map(([k, v]) => `<div class="card"><span>${k}</span><b>${v}</b></div>`)
    .join("");
  pie(root.querySelector("#chart-nodes"), "节点分布", data.graph.nodes_by_type);
  pie(root.querySelector("#chart-edges"), "边分布", data.graph.edges_by_type);
  return () => charts.forEach((c) => c.dispose());
}

function pie(el, title, kv) {
  const chart = echarts.init(el);
  charts.push(chart);
  chart.setOption({
    title: { text: title, left: "center" },
    tooltip: { trigger: "item" },
    series: [{ type: "pie", radius: ["35%", "65%"],
      data: Object.entries(kv).map(([name, value]) => ({ name, value })) }],
  });
}
