import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ChartConfiguration } from "../types";

// Fallback palette when the LLM does not specify colors.
const FALLBACK_COLORS = ["#6366f1", "#22c55e", "#f59e0b", "#ef4444", "#3b82f6", "#a855f7"];

interface DynamicChartProps {
  config: ChartConfiguration;
}

/**
 * Deterministically compute chart dimensions from the data shape.
 *
 * Width  — each category (bar group / line point) needs comfortable horizontal
 *          space. For bar charts we budget ~72 px per group * number of series;
 *          for line charts ~48 px per point. We clamp between 480 and 1100 px.
 *
 * Height — base of 300 px plus 40 px for every extra series beyond the first,
 *          so multi-series charts are never cramped. Clamped 300–560 px.
 */
function computeChartDimensions(
  chartType: "bar" | "line",
  rowCount: number,
  seriesCount: number,
): { width: number; height: number } {
  const pxPerGroup = chartType === "bar" ? 72 * seriesCount : 48;
  const rawWidth = rowCount * pxPerGroup + 80; // +80 for Y-axis gutter
  const width = Math.min(1100, Math.max(480, rawWidth));
  const height = Math.min(560, Math.max(300, 300 + (seriesCount - 1) * 40));
  return { width, height };
}

export function DynamicChart({ config }: DynamicChartProps) {
  const { chart_type, title, data, x_axis_key, data_keys, colors } = config;
  const resolvedColors = (i: number) =>
    (colors && colors[i]) ?? FALLBACK_COLORS[i % FALLBACK_COLORS.length];

  const { width: chartWidth, height: chartHeight } = computeChartDimensions(
    chart_type,
    data.length,
    data_keys.length,
  );

  const sharedProps = {
    data,
    margin: { top: 8, right: 24, left: 0, bottom: 4 },
  };

  const axes = (
    <>
      <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
      <XAxis dataKey={x_axis_key} tick={{ fontSize: 12 }} />
      <YAxis tick={{ fontSize: 12 }} width={40} />
      <Tooltip />
      <Legend />
    </>
  );

  return (
    <div className="dynamic-chart">
      {title && <p className="dynamic-chart__title">{title}</p>}
      {/* minWidth lets the container scroll horizontally on small screens
          rather than crushing the bars into unreadable slivers */}
      <div style={{ minWidth: chartWidth, width: "100%" }}>
      <ResponsiveContainer width="100%" height={chartHeight}>
        {chart_type === "bar" ? (
          <BarChart {...sharedProps}>
            {axes}
            {data_keys.map((key, i) => (
              <Bar key={key} dataKey={key} fill={resolvedColors(i)} radius={[3, 3, 0, 0]} />
            ))}
          </BarChart>
        ) : (
          <LineChart {...sharedProps}>
            {axes}
            {data_keys.map((key, i) => (
              <Line
                key={key}
                type="monotone"
                dataKey={key}
                stroke={resolvedColors(i)}
                strokeWidth={2}
                dot={false}
              />
            ))}
          </LineChart>
        )}
      </ResponsiveContainer>
      </div>
    </div>
  );
}
