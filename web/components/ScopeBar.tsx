import type { Domain } from "@/lib/api";

type ScopeBarProps = {
  activeDomain: Domain;
  onDomainChange: (domain: Domain) => void;
  rowCount: number;
  filtersOpen: boolean;
  onToggleFilters: () => void;
};

const DOMAINS: Array<{ value: Domain; label: string }> = [
  { value: "overall", label: "Overall" },
  { value: "software", label: "Software" },
  { value: "finance", label: "Finance" },
  { value: "legal", label: "Legal" },
  { value: "medical", label: "Medical" },
  { value: "physics", label: "Physics" },
];

export default function ScopeBar({
  activeDomain,
  onDomainChange,
  rowCount,
  filtersOpen,
  onToggleFilters,
}: ScopeBarProps) {
  return (
    <section className="border-y border-[#dedbd2]" aria-label="Dashboard scope">
      <div className="flex min-w-0 items-center justify-between gap-5">
        <div className="min-w-0 flex-1 overflow-x-auto" role="tablist" aria-label="Domain">
          <div className="flex min-w-max items-stretch gap-1">
            {DOMAINS.map((domain) => {
              const isActive = activeDomain === domain.value;

              return (
                <button
                  key={domain.value}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  onClick={() => onDomainChange(domain.value)}
                  className={`border-b-2 px-3 py-4 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[#283b32] ${
                    isActive
                      ? "border-[#283b32] text-[#202822]"
                      : "border-transparent text-[#777970] hover:text-[#202822]"
                  }`}
                >
                  {domain.label}
                </button>
              );
            })}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-4 py-2 text-sm">
          <span className="whitespace-nowrap text-[#777970]">
            <strong className="font-semibold text-[#202822]">{rowCount.toLocaleString()}</strong>{" "}
            records
          </span>
          <button
            type="button"
            aria-expanded={filtersOpen}
            onClick={onToggleFilters}
            className="whitespace-nowrap rounded-sm px-1 py-2 font-semibold text-[#283b32] underline decoration-[#a9b7aa] underline-offset-4 hover:decoration-[#283b32] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
          >
            {filtersOpen ? "Hide Filters" : "Show Filters"}
          </button>
        </div>
      </div>
    </section>
  );
}
