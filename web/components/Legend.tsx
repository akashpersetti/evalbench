type LegendProps = {
  includeRefusals: boolean;
};

const LEGEND_ITEMS = [
  { key: "clear", label: "Clear" },
  { key: "partial", label: "Partial" },
  { key: "failed", label: "Failed" },
  { key: "refused", label: "Refused" },
] as const;

export default function Legend({ includeRefusals }: LegendProps) {
  const items = includeRefusals
    ? LEGEND_ITEMS
    : LEGEND_ITEMS.filter((item) => item.key !== "refused");

  return (
    <section
      aria-labelledby="chart-legend-label"
      className="py-5 text-sm text-[#62675f]"
    >
      <h2 id="chart-legend-label" className="sr-only">
        Chart legend
      </h2>
      <ul className="flex flex-wrap items-center gap-x-5 gap-y-2">
        {items.map((item) => (
          <li key={item.key} className="inline-flex items-center gap-2">
            <span
              aria-hidden="true"
              className="h-3 w-3 rounded-sm"
              style={{ backgroundColor: `var(--segment-${item.key}, #65758b)` }}
            />
            {item.label}
          </li>
        ))}
      </ul>
    </section>
  );
}
