"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
  type YAxisTickContentProps,
} from "recharts";
import type { AggregatedModelRow, Segment } from "@/lib/api";

export const MODEL_FAMILY_COLORS: Readonly<Record<string, string>> = Object.freeze({
  OpenAI: "#3c82c6",
  Anthropic: "#ad674d",
  Gemini: "#7657ad",
  XAI: "#43484d",
  OpenRouter: "#31958d",
  Voyage: "#4c59a7",
  Cohere: "#b24f9e",
});

const FALLBACK_FAMILY_COLOR = "#65758b";
const CHART_MARGINS = { top: 8, right: 12, bottom: 24, left: 8 } as const;
const MIN_PLOT_WIDTH = 520;
const ROW_HEIGHT = 64;

export function familyColor(family: string): string {
  return MODEL_FAMILY_COLORS[family] ?? FALLBACK_FAMILY_COLOR;
}

type SegmentKey = Segment["key"];

const SEGMENTS: ReadonlyArray<{
  key: SegmentKey;
  label: string;
  color: string;
  labelColor: string;
}> = [
  {
    key: "clear",
    label: "Clear",
    color: "var(--segment-clear)",
    labelColor: "#ffffff",
  },
  {
    key: "partial",
    label: "Partial",
    color: "var(--segment-partial)",
    labelColor: "#28251d",
  },
  {
    key: "failed",
    label: "Failed",
    color: "var(--segment-failed)",
    labelColor: "#ffffff",
  },
  {
    key: "refused",
    label: "Refused",
    color: "var(--segment-refused)",
    labelColor: "#252824",
  },
];

type ChartRow = {
  model: string;
  model_family: string;
  n: number;
  clear: number;
  partial: number;
  failed: number;
  refused: number;
  counts: Record<SegmentKey, number>;
};

function segmentValue(
  segments: Segment[],
  key: SegmentKey,
): { percentage: number; count: number } {
  const segment = segments.find((candidate) => candidate.key === key);
  return {
    percentage: segment?.percentage ?? 0,
    count: segment?.count ?? 0,
  };
}

function chartRows(rows: AggregatedModelRow[], metricKey: string): ChartRow[] {
  return rows
    .flatMap((row) => {
      const breakdown = row.stacked[metricKey];
      if (!breakdown) {
        return [];
      }

      const values = Object.fromEntries(
        SEGMENTS.map(({ key }) => [key, segmentValue(breakdown.segments, key)]),
      ) as Record<SegmentKey, { percentage: number; count: number }>;

      return [
        {
          model: row.model,
          model_family: row.model_family,
          n: row.n,
          clear: values.clear.percentage,
          partial: values.partial.percentage,
          failed: values.failed.percentage,
          refused: values.refused.percentage,
          counts: {
            clear: values.clear.count,
            partial: values.partial.count,
            failed: values.failed.count,
            refused: values.refused.count,
          },
        },
      ];
    })
    .sort(
      (left, right) =>
        right.clear - left.clear ||
        right.partial - left.partial ||
        left.model.localeCompare(right.model),
    );
}

type SegmentLabelProps = {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  value?: number;
  fill?: string;
};

function SegmentLabel({
  x = 0,
  y = 0,
  width = 0,
  height = 0,
  value,
  fill,
}: SegmentLabelProps) {
  if (typeof value !== "number" || value < 8 || width < 18) {
    return null;
  }

  return (
    <text
      x={x + width / 2}
      y={y + height / 2}
      textAnchor="middle"
      dominantBaseline="middle"
      fill={fill}
      fontSize={11}
      fontWeight={700}
      pointerEvents="none"
    >
      {`${Math.round(value)}%`}
    </text>
  );
}

type ModelTickProps = YAxisTickContentProps & {
  rowByModel: ReadonlyMap<string, ChartRow>;
  axisWidth: number;
};

function ModelTick({ x, y, payload, rowByModel, axisWidth }: ModelTickProps) {
  const model = String(payload.value);
  const row = rowByModel.get(model);
  const modelX = -axisWidth + 28;
  const dotX = -axisWidth + 12;
  const tickX = Number(x);
  const tickY = Number(y);

  return (
    <g transform={`translate(${tickX}, ${tickY})`}>
      <title>{`${model} (n=${row?.n ?? 0})`}</title>
      <circle cx={dotX} cy={0} r={4} fill={familyColor(row?.model_family ?? "")} />
      <text
        x={modelX}
        y={0}
        textAnchor="start"
        dominantBaseline="middle"
        fill="#30352f"
        fontSize={12}
      >
        <tspan>{model}</tspan>
        <tspan fill="#777970">{` (n=${row?.n ?? 0})`}</tspan>
      </text>
    </g>
  );
}

type ChartTooltipProps = TooltipContentProps;

function ChartTooltip({ active, payload }: ChartTooltipProps) {
  if (!active || !payload?.length) {
    return null;
  }

  const row = payload[0]?.payload as ChartRow | undefined;
  if (!row) {
    return null;
  }

  return (
    <div className="model-bar-chart__tooltip">
      <p className="model-bar-chart__tooltip-title">{row.model}</p>
      <p className="model-bar-chart__tooltip-sample">Row sample: n={row.n}</p>
      <ul>
        {SEGMENTS.map(({ key, label }) => (
          <li key={key}>
            <span>{label}</span>
            <span>{`${row[key].toFixed(1)}% (${row.counts[key]})`}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

type ModelBarChartProps = {
  rows: AggregatedModelRow[];
  metricKey: string;
};

export default function ModelBarChart({ rows, metricKey }: ModelBarChartProps) {
  const data = chartRows(rows, metricKey);

  if (data.length === 0) {
    return (
      <p className="model-bar-chart__empty">
        No model breakdowns are available for this metric.
      </p>
    );
  }

  const rowByModel = new Map(data.map((row) => [row.model, row]));
  const axisWidth = Math.min(
    420,
    Math.max(
      260,
      ...data.map((row) => Math.min(360, row.model.length * 7 + 82)),
    ),
  );
  const chartHeight = Math.max(
    180,
    data.length * ROW_HEIGHT + CHART_MARGINS.top + CHART_MARGINS.bottom,
  );

  return (
    <div className="model-bar-chart" role="img" aria-label={`Ranked model outcomes for ${metricKey}`}>
      <div className="model-bar-chart__scroll">
        <div
          className="model-bar-chart__canvas"
          style={{
            minWidth: `${axisWidth + MIN_PLOT_WIDTH + CHART_MARGINS.left + CHART_MARGINS.right}px`,
          }}
        >
          <ResponsiveContainer width="100%" height={chartHeight}>
            <BarChart
              data={data}
              layout="vertical"
              margin={CHART_MARGINS}
              barCategoryGap="14%"
            >
              <CartesianGrid
                horizontal={false}
                vertical
                stroke="#e0ddd4"
                strokeDasharray="2 4"
              />
              <XAxis
                type="number"
                domain={[0, 100]}
                ticks={[0, 25, 50, 75, 100]}
                tickFormatter={(value: number) => `${value}%`}
                axisLine={{ stroke: "#c9c6bc" }}
                tickLine={false}
                tick={{ fill: "#777970", fontSize: 11 }}
              />
              <YAxis
                type="category"
                dataKey="model"
                width={axisWidth}
                interval={0}
                axisLine={false}
                tickLine={false}
                tick={(props: YAxisTickContentProps) => (
                  <ModelTick {...props} rowByModel={rowByModel} axisWidth={axisWidth} />
                )}
              />
              <Tooltip
                cursor={{ fill: "#efede6" }}
                content={ChartTooltip}
              />
              {SEGMENTS.map(({ key, label, color, labelColor }) => (
                <Bar
                  key={key}
                  dataKey={key}
                  name={label}
                  stackId="outcomes"
                  fill={color}
                  isAnimationActive={false}
                >
                  <LabelList
                    dataKey={key}
                    content={<SegmentLabel fill={labelColor} />}
                  />
                </Bar>
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
