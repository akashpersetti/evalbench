import type { WindowDays } from "@/lib/api";
import { familyColor } from "@/components/ModelBarChart";

type FilterControlsProps = {
  excludeRefusals: boolean;
  onExcludeRefusalsChange: (value: boolean) => void;
  windowDays: WindowDays;
  onWindowDaysChange: (value: WindowDays) => void;
  families: string[];
  selectedFamilies: Set<string>;
  onToggleFamily: (family: string) => void;
};

function parseWindowDays(value: string): WindowDays {
  if (value === "all") {
    return "all";
  }
  if (value === "7" || value === "30" || value === "90") {
    return Number(value) as 7 | 30 | 90;
  }
  return 30;
}

export default function FilterControls({
  excludeRefusals,
  onExcludeRefusalsChange,
  windowDays,
  onWindowDaysChange,
  families,
  selectedFamilies,
  onToggleFamily,
}: FilterControlsProps) {
  return (
    <section className="border-b border-[#dedbd2] py-5" aria-label="Filters">
      <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
        <div className="flex flex-col gap-2" role="group" aria-labelledby="rate-mode-label">
          <span id="rate-mode-label" className="text-xs font-semibold uppercase tracking-[0.12em] text-[#777970]">
            Rate mode
          </span>
          <div className="inline-flex w-fit rounded-md border border-[#cbc8be] p-1" role="radiogroup" aria-labelledby="rate-mode-label">
            <button
              type="button"
              role="radio"
              aria-checked={!excludeRefusals}
              onClick={() => onExcludeRefusalsChange(false)}
              className={`rounded px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32] ${
                !excludeRefusals
                  ? "bg-[#283b32] font-semibold text-white"
                  : "text-[#62675f] hover:bg-[#ebe9e1]"
              }`}
            >
              All attempts
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={excludeRefusals}
              onClick={() => onExcludeRefusalsChange(true)}
              className={`rounded px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32] ${
                excludeRefusals
                  ? "bg-[#283b32] font-semibold text-white"
                  : "text-[#62675f] hover:bg-[#ebe9e1]"
              }`}
            >
              Exclude refusals
            </button>
          </div>
        </div>

        <label className="flex w-fit flex-col gap-2 text-xs font-semibold uppercase tracking-[0.12em] text-[#777970]">
          <span>Time window</span>
          <select
            value={String(windowDays)}
            onChange={(event) => onWindowDaysChange(parseWindowDays(event.target.value))}
            className="min-w-28 rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 text-sm font-medium normal-case tracking-normal text-[#202822] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
          >
            <option value="7">7d</option>
            <option value="30">30d</option>
            <option value="90">90d</option>
            <option value="all">all</option>
          </select>
        </label>
      </div>

      <div className="mt-5" role="group" aria-labelledby="family-filter-label">
        <span id="family-filter-label" className="text-xs font-semibold uppercase tracking-[0.12em] text-[#777970]">
          Model family
        </span>
        {families.length > 0 ? (
          <div className="mt-3 flex flex-wrap gap-2">
            {families.map((family) => {
              const selected = selectedFamilies.has(family);

              return (
                <button
                  key={family}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => onToggleFamily(family)}
                  className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32] ${
                    selected
                      ? "border-[#aeb9af] bg-[#f0f3ee] text-[#283b32]"
                      : "border-[#d5d2c9] bg-transparent text-[#777970]"
                  }`}
                >
                  <span
                    aria-hidden="true"
                    className="h-2.5 w-2.5 rounded-full"
                    style={{ backgroundColor: familyColor(family) }}
                  />
                  {family}
                </button>
              );
            })}
          </div>
        ) : (
          <p className="mt-3 text-sm text-[#777970]">No model families in this scope.</p>
        )}
      </div>
    </section>
  );
}
