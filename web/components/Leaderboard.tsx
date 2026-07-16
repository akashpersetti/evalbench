"use client";

import { useMemo, useState } from "react";
import type {
  AggregatedModelRow,
  DisplayMetric,
  Estimate,
  SuiteDescriptor,
} from "@/lib/api";
import { familyColor } from "@/components/ModelBarChart";

export type SortKey = {
  kind: "metric" | "derived";
  key: string;
};

type ValueFormat = DisplayMetric["format"] | "milliseconds";
type SortDirection = "asc" | "desc";

type LeaderboardColumn = SortKey & {
  label: string;
  format: ValueFormat;
  higherIsBetter: boolean;
};

const DERIVED_LABELS: Readonly<Record<string, string>> = {
  cost_adjusted_quality: "Cost-adjusted quality",
  p95_latency_ms: "p95 latency",
};

function humanizeKey(key: string): string {
  return key.replace(/[_-]+/g, " ").trim();
}

function formatDecimal(value: number, maximumFractionDigits: number): string {
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits,
    useGrouping: true,
  }).format(value);
}

function formatCurrency(value: number): string {
  if (value === 0) {
    return "$0.00";
  }

  const absoluteValue = Math.abs(value);
  const decimalPlaces =
    absoluteValue >= 0.01
      ? 2
      : Math.min(12, Math.max(2, Math.ceil(-Math.log10(absoluteValue)) + 2));
  const formatted = new Intl.NumberFormat("en-US", {
    currency: "USD",
    maximumFractionDigits: decimalPlaces,
    minimumFractionDigits: Math.min(2, decimalPlaces),
    style: "currency",
    useGrouping: true,
  }).format(value);

  // Keep a non-zero cost visible when it is smaller than the formatter's
  // precision. This is intentionally rare, but avoids turning a real estimate
  // into a misleading "$0" in a cost comparison.
  const numericPart = formatted.replace(/[^0-9.-]/g, "");
  if (Number(numericPart) === 0) {
    return `$${value.toExponential(2)}`;
  }

  return formatted;
}

export function formatValue(value: number, format: ValueFormat): string {
  switch (format) {
    case "percent":
      return `${formatDecimal(value * 100, 1)}%`;
    case "currency":
      return formatCurrency(value);
    case "milliseconds":
      return `${formatDecimal(value, 3)} ms`;
    case "number":
      return formatDecimal(value, 3);
  }
}

export function estimateLabel(
  estimate: Estimate,
  format: ValueFormat,
): string {
  const sampleSize = Number.isFinite(estimate.n) ? estimate.n : 0;

  if (sampleSize <= 0 || estimate.mean === null) {
    return `—\nn=${sampleSize}`;
  }

  const interval =
    estimate.ci_low !== null && estimate.ci_high !== null
      ? `95% CI ${formatValue(estimate.ci_low, format)}–${formatValue(estimate.ci_high, format)}`
      : "95% CI —";

  return `${formatValue(estimate.mean, format)}\n${interval}\nn=${sampleSize}`;
}

function derivedFormat(key: string): ValueFormat {
  return key === "p95_latency_ms" ? "milliseconds" : "number";
}

function makeColumns(
  suite: SuiteDescriptor,
  rows: AggregatedModelRow[],
): LeaderboardColumn[] {
  const metadata = new Map(
    suite.display_metrics.map((metric) => [metric.key, metric]),
  );
  const columns: LeaderboardColumn[] = [];
  const seenMetricKeys = new Set<string>();

  for (const metric of suite.display_metrics) {
    columns.push({
      kind: "metric",
      key: metric.key,
      label: metric.label,
      format: metric.format,
      higherIsBetter: metric.higher_is_better,
    });
    seenMetricKeys.add(metric.key);
  }

  for (const key of suite.metric_keys) {
    if (seenMetricKeys.has(key)) {
      continue;
    }

    const metric = metadata.get(key);
    columns.push({
      kind: "metric",
      key,
      label: metric?.label ?? humanizeKey(key),
      format: metric?.format ?? "number",
      // Undeclared display metadata has no direction. Descending is the
      // least surprising stable default for a numeric metric.
      higherIsBetter: metric?.higher_is_better ?? true,
    });
    seenMetricKeys.add(key);
  }

  const derivedKeys = Array.from(
    new Set(rows.flatMap((row) => Object.keys(row.derived))),
  ).sort((left, right) => left.localeCompare(right));

  return [
    ...columns,
    ...derivedKeys.map((key) => ({
      kind: "derived" as const,
      key,
      label: DERIVED_LABELS[key] ?? humanizeKey(key),
      format: derivedFormat(key),
      higherIsBetter: key === "p95_latency_ms" ? false : true,
    })),
  ];
}

function sameSortKey(left: SortKey, right: SortKey): boolean {
  return left.kind === right.kind && left.key === right.key;
}

function estimateForRow(
  row: AggregatedModelRow,
  column: SortKey,
): Estimate | null {
  return (column.kind === "metric" ? row.metrics[column.key] : row.derived[column.key]) ?? null;
}

function isSortableEstimate(
  estimate: Estimate | null,
): estimate is Estimate & { mean: number } {
  return estimate !== null && estimate.mean !== null && estimate.n > 0;
}

function sortRows(
  rows: AggregatedModelRow[],
  column: SortKey,
  direction: SortDirection,
): AggregatedModelRow[] {
  return [...rows].sort((left, right) => {
    const leftEstimate = estimateForRow(left, column);
    const rightEstimate = estimateForRow(right, column);
    const leftSortable = isSortableEstimate(leftEstimate);
    const rightSortable = isSortableEstimate(rightEstimate);

    // Missing and n=0 estimates are always last, regardless of direction.
    if (leftSortable !== rightSortable) {
      return leftSortable ? -1 : 1;
    }

    if (leftSortable && rightSortable) {
      const difference = leftEstimate.mean - rightEstimate.mean;
      if (difference !== 0) {
        return direction === "asc" ? difference : -difference;
      }
    }

    return left.model.localeCompare(right.model);
  });
}

type LeaderboardProps = {
  rows: AggregatedModelRow[];
  suite: SuiteDescriptor;
};

export default function Leaderboard({ rows, suite }: LeaderboardProps) {
  const columns = useMemo(() => makeColumns(suite, rows), [rows, suite]);
  const firstColumn = columns[0];
  const [sort, setSort] = useState<{
    key: SortKey;
    direction: SortDirection;
  } | null>(() =>
    firstColumn
      ? {
          key: { kind: firstColumn.kind, key: firstColumn.key },
          direction: firstColumn.higherIsBetter ? "desc" : "asc",
        }
      : null,
  );

  const sortedRows = useMemo(() => {
    if (!sort) {
      return rows;
    }

    return sortRows(rows, sort.key, sort.direction);
  }, [rows, sort]);

  const onSort = (column: LeaderboardColumn) => {
    const key = { kind: column.kind, key: column.key } as SortKey;
    setSort((current) => ({
      key,
      direction:
        current && sameSortKey(current.key, key)
          ? current.direction === "desc"
            ? "asc"
            : "desc"
          : column.higherIsBetter
            ? "desc"
            : "asc",
    }));
  };

  if (columns.length === 0) {
    return null;
  }

  return (
    <section className="leaderboard" aria-labelledby="leaderboard-heading">
      <div className="leaderboard__intro">
        <div>
          <p className="leaderboard__eyebrow">Continuous metrics</p>
          <h3 id="leaderboard-heading" className="leaderboard__title">
            Metric leaderboard
          </h3>
        </div>
        <p className="leaderboard__hint">
          Select a column to sort. Intervals are 95% confidence estimates.
        </p>
      </div>

      <div className="leaderboard__scroll">
        <table className="leaderboard__table">
          <caption className="sr-only">
            Sortable model metric leaderboard for the {suite.name} suite
          </caption>
          <thead>
            <tr>
              <th className="leaderboard__model-header" scope="col">
                Model
              </th>
              {columns.map((column) => {
                const isActive = sort !== null && sameSortKey(sort.key, column);
                const ariaSort = isActive
                  ? sort.direction === "asc"
                    ? "ascending"
                    : "descending"
                  : undefined;
                const directionLabel = isActive
                  ? sort.direction === "asc"
                    ? "ascending"
                    : "descending"
                  : "not sorted";

                return (
                  <th
                    key={`${column.kind}:${column.key}`}
                    className="leaderboard__metric-header"
                    scope="col"
                    aria-sort={ariaSort}
                  >
                    <button
                      type="button"
                      onClick={() => onSort(column)}
                      title={`Sort by ${column.label}`}
                      aria-label={`${column.label}, ${directionLabel}`}
                    >
                      <span>{column.label}</span>
                      <span className="leaderboard__sort-indicator" aria-hidden="true">
                        {isActive ? (sort.direction === "asc" ? "↑" : "↓") : "↕"}
                      </span>
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {sortedRows.map((row) => (
              <tr key={row.model}>
                <th className="leaderboard__model-cell" scope="row">
                  <span
                    className="leaderboard__family-dot"
                    aria-hidden="true"
                    style={{ backgroundColor: familyColor(row.model_family) }}
                  />
                  <span className="leaderboard__model-name" title={row.model}>
                    {row.model}
                  </span>
                  <span className="leaderboard__row-sample">n={row.n}</span>
                </th>
                {columns.map((column) => {
                  const estimate = estimateForRow(row, column);
                  const label = estimate
                    ? estimateLabel(estimate, column.format)
                    : `—\nn=0`;

                  return (
                    <td key={`${column.kind}:${column.key}`} className="leaderboard__value-cell">
                      <span className="leaderboard__estimate">{label}</span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
