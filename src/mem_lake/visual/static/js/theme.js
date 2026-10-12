// ECharts 深色主题公共配置：token 化颜色 + tooltip/legend/axis 统一适配。
// 各视图 spread baseChartOption() 后覆盖自身特有配置，避免每页重复硬编码。

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// 数据可视化调色板（设计稿 dataVizPalette）
export const DATA_VIZ_PALETTE = ["#2b7fff", "#22d3ee", "#2dd4bf", "#a78bfa", "#fbbf24"];

// 节点类型色（--ml-viz-*）：key 与后端节点 type 一致
export const TYPE_COLORS = {
  ProjectProfile: "#5470c6", Requirement: "#91cc75", CodeSnippet: "#fac858",
  Solution: "#ee6666", DesignIntent: "#73c0de", Decision: "#3ba272", Pitfall: "#fc8451",
};

// 节点类型中文展示名（仅展示层映射，后端 type 契约不变；无映射回退原文）
export const TYPE_LABELS = {
  ProjectProfile: "项目档案",
  Requirement: "需求",
  CodeSnippet: "代码片段",
  Solution: "解决方案",
  DesignIntent: "设计意图",
  Decision: "决策",
  Pitfall: "避坑经验",
};

export function typeLabel(t) {
  return TYPE_LABELS[t] || t;
}

export function baseChartOption() {
  const surface = cssVar("--ml-surface") || "#14161c";
  const ink = cssVar("--ml-ink") || "#f0f1f5";
  const ink2 = cssVar("--ml-ink-2") || "#a0a8b8";
  const line = cssVar("--ml-line") || "#272a35";
  return {
    backgroundColor: "transparent",
    textStyle: { fontFamily: 'Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif' },
    color: DATA_VIZ_PALETTE,
    tooltip: {
      backgroundColor: surface, borderColor: line,
      textStyle: { color: ink },
    },
    legend: { textStyle: { color: ink2 }, itemWidth: 10, itemHeight: 10 },
  };
}

// 柱/线图坐标轴通用深色样式
export function darkAxis(extra = {}) {
  const ink2 = cssVar("--ml-ink-2") || "#a0a8b8";
  const line = cssVar("--ml-line") || "#272a35";
  return {
    axisLine: { lineStyle: { color: line } },
    axisLabel: { color: ink2 },
    splitLine: { lineStyle: { color: line, type: "dashed" } },
    ...extra,
  };
}

// 统一图表初始化：挂 ResizeObserver 让画布跟随容器尺寸变化。
// 必要性：Tailwind browser 为运行时编译，视图渲染后布局类（如两列 grid）
// 可能晚于 echarts.init 生效，容器宽度变化需 chart.resize() 跟随，
// 否则首次进入图表空白/错位。dispose 时自动断开观察避免泄漏。
export function initChart(el) {
  const chart = echarts.init(el);
  const ro = new ResizeObserver(() => chart.resize());
  ro.observe(el);
  const origDispose = chart.dispose.bind(chart);
  chart.dispose = () => {
    ro.disconnect();
    origDispose();
  };
  return chart;
}
