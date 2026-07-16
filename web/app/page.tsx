"use client";

import { useEffect, useMemo, useState } from "react";
import FilterControls from "@/components/FilterControls";
import Leaderboard from "@/components/Leaderboard";
import Legend from "@/components/Legend";
import ModelBarChart from "@/components/ModelBarChart";
import ScopeBar from "@/components/ScopeBar";
import {
  ApiError,
  fetchResults,
  fetchSuites,
  type AggregatedModelRow,
  type Domain,
  type ResultsResponse,
  type SuiteDescriptor,
  type WindowDays,
} from "@/lib/api";

function errorMessage(reason: unknown): string {
  return reason instanceof ApiError
    ? reason.message
    : "Unable to connect to the EvalBench API.";
}

function familyNames(rows: AggregatedModelRow[]): string[] {
  return Array.from(new Set(rows.map((row) => row.model_family))).sort((a, b) =>
    a.localeCompare(b),
  );
}

function rebaseFamilySelection(
  selected: Set<string>,
  availableFamilies: string[],
): Set<string> {
  if (availableFamilies.length === 0 || selected.size === 0) {
    return new Set(availableFamilies);
  }

  const rebased = new Set(
    availableFamilies.filter((family) => selected.has(family)),
  );
  return rebased.size === 0 ? new Set(availableFamilies) : rebased;
}

function suiteLabel(name: string): string {
  const label = name.replace(/[_-]+/g, " ").trim();
  return label ? `${label[0].toUpperCase()}${label.slice(1)}` : name;
}

type ResultState = {
  key: string;
  response: ResultsResponse;
};

type ErrorState = {
  key: string;
  message: string;
};

export default function Home() {
  const [suites, setSuites] = useState<SuiteDescriptor[] | null>(null);
  const [activeSuiteName, setActiveSuiteName] = useState<string | null>(null);
  const [activeDomain, setActiveDomain] = useState<Domain>("overall");
  const [excludeRefusals, setExcludeRefusals] = useState(false);
  const [windowDays, setWindowDays] = useState<WindowDays>(30);
  const [filtersOpen, setFiltersOpen] = useState(true);
  const [selectedFamilies, setSelectedFamilies] = useState<Set<string>>(
    () => new Set(),
  );
  const [familyHistory, setFamilyHistory] = useState<Record<string, string[]>>(
    {},
  );
  const [unrestrictedResults, setUnrestrictedResults] =
    useState<ResultState | null>(null);
  const [restrictedResults, setRestrictedResults] =
    useState<ResultState | null>(null);
  const [suitesError, setSuitesError] = useState<string | null>(null);
  const [unrestrictedError, setUnrestrictedError] =
    useState<ErrorState | null>(null);
  const [restrictedError, setRestrictedError] =
    useState<ErrorState | null>(null);
  const [retrySuitesKey, setRetrySuitesKey] = useState(0);
  const [retryResultsKey, setRetryResultsKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();

    fetchSuites(controller.signal)
      .then((nextSuites) => {
        setSuites(nextSuites);
        setActiveSuiteName((currentSuite) => {
          if (currentSuite && nextSuites.some((suite) => suite.name === currentSuite)) {
            return currentSuite;
          }
          return nextSuites[0]?.name ?? null;
        });
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setSuitesError(errorMessage(reason));
        }
      });

    return () => controller.abort();
  }, [retrySuitesKey]);

  const activeSuite = useMemo(
    () => suites?.find((suite) => suite.name === activeSuiteName) ?? null,
    [activeSuiteName, suites],
  );
  const familyScopeKey = `${activeSuiteName ?? ""}|${activeDomain}|${windowDays}`;
  const families = useMemo(
    () => familyHistory[familyScopeKey] ?? [],
    [familyHistory, familyScopeKey],
  );
  const scopedSelectedFamilies = useMemo(
    () => rebaseFamilySelection(selectedFamilies, families),
    [families, selectedFamilies],
  );
  const selectedFamilyQuery = useMemo(() => {
    if (
      families.length === 0 ||
      scopedSelectedFamilies.size === families.length
    ) {
      return [];
    }

    return families.filter((family) => scopedSelectedFamilies.has(family));
  }, [families, scopedSelectedFamilies]);
  const selectedFamilyQueryKey = selectedFamilyQuery.join("\u0000");
  const unrestrictedQueryKey = `${familyScopeKey}|${excludeRefusals}|${retryResultsKey}`;
  const restrictedQueryKey = `${unrestrictedQueryKey}|${selectedFamilyQueryKey}`;

  useEffect(() => {
    if (!activeSuiteName) {
      return;
    }

    const controller = new AbortController();

    fetchResults(
      {
        suite: activeSuiteName,
        domain: activeDomain,
        windowDays,
        excludeRefusals,
        families: [],
      },
      controller.signal,
    )
      .then((result) => {
        if (controller.signal.aborted) {
          return;
        }
        setUnrestrictedResults({ key: unrestrictedQueryKey, response: result });
        setFamilyHistory((history) => {
          const previous = history[familyScopeKey] ?? [];
          const next = Array.from(
            new Set([...previous, ...familyNames(result.rows)]),
          ).sort((a, b) => a.localeCompare(b));
          return { ...history, [familyScopeKey]: next };
        });
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setUnrestrictedError({
            key: unrestrictedQueryKey,
            message: errorMessage(reason),
          });
        }
      });

    return () => controller.abort();
  }, [
    activeDomain,
    activeSuiteName,
    excludeRefusals,
    familyScopeKey,
    retryResultsKey,
    unrestrictedQueryKey,
    windowDays,
  ]);

  useEffect(() => {
    if (!activeSuiteName || selectedFamilyQuery.length === 0) {
      return;
    }

    const controller = new AbortController();

    fetchResults(
      {
        suite: activeSuiteName,
        domain: activeDomain,
        windowDays,
        excludeRefusals,
        families: selectedFamilyQuery,
      },
      controller.signal,
    )
      .then((result) => {
        if (!controller.signal.aborted) {
          setRestrictedResults({ key: restrictedQueryKey, response: result });
        }
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setRestrictedError({
            key: restrictedQueryKey,
            message: errorMessage(reason),
          });
        }
      });

    return () => controller.abort();
  }, [
    activeDomain,
    activeSuiteName,
    excludeRefusals,
    restrictedQueryKey,
    selectedFamilyQueryKey,
    selectedFamilyQuery,
    windowDays,
  ]);

  const unrestrictedResponse =
    unrestrictedResults?.key === unrestrictedQueryKey
      ? unrestrictedResults.response
      : null;
  const restrictedResponse =
    restrictedResults?.key === restrictedQueryKey ? restrictedResults.response : null;
  const displayedResults =
    selectedFamilyQuery.length > 0 ? restrictedResponse : unrestrictedResponse;
  const displayedErrorState =
    selectedFamilyQuery.length > 0
      ? restrictedError?.key === restrictedQueryKey
        ? restrictedError
        : unrestrictedError?.key === unrestrictedQueryKey
          ? unrestrictedError
          : null
      : unrestrictedError?.key === unrestrictedQueryKey
        ? unrestrictedError
        : null;
  const primaryMetric = useMemo(() => {
    if (!activeSuite || !displayedResults) {
      return null;
    }

    return (
      activeSuite.display_metrics.find((metric) =>
        displayedResults.rows.some((row) => row.stacked[metric.key] !== undefined),
      ) ?? null
    );
  }, [activeSuite, displayedResults]);

  const changeSuite = (suiteName: string) => {
    setActiveSuiteName(suiteName);
    setSelectedFamilies(new Set());
  };

  const toggleFamily = (family: string) => {
    setSelectedFamilies(() => {
      const next = new Set(scopedSelectedFamilies);
      if (next.has(family)) {
        if (next.size === 1) {
          return next;
        }
        next.delete(family);
      } else {
        next.add(family);
      }

      return next.size === families.length ? new Set() : next;
    });
  };

  const retryResults = () => {
    setRetryResultsKey((key) => key + 1);
  };

  return (
    <main
      className="min-h-screen bg-[#f7f5ef] text-[#202822]"
    >
      <div className="mx-auto max-w-6xl px-5 py-7 sm:px-8 lg:px-10">
        <header className="mb-7 flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#777970]">
              EvalBench
            </p>
            <h1 className="mt-2 text-3xl font-semibold tracking-tight">Evaluation dashboard</h1>
          </div>
          {suites !== null && suites.length > 0 && (
            <label className="flex items-center gap-3 text-sm text-[#777970]">
              <span>Suite</span>
              <select
                value={activeSuiteName ?? ""}
                onChange={(event) => changeSuite(event.target.value)}
                className="rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 font-medium text-[#202822] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
              >
                {suites.map((suite) => (
                  <option key={suite.name} value={suite.name}>
                    {suite.name}
                  </option>
                ))}
              </select>
            </label>
          )}
        </header>

        {suites === null && suitesError === null && (
          <p role="status" className="py-10 text-[#62675f]">Loading suites…</p>
        )}
        {suitesError !== null && (
          <section className="border-y border-[#dedbd2] py-8" aria-live="polite">
            <p>{suitesError}</p>
            <button
              type="button"
              onClick={() => {
                setSuites(null);
                setSuitesError(null);
                setRetrySuitesKey((key) => key + 1);
              }}
              className="mt-4 rounded-md bg-[#283b32] px-4 py-2 text-sm font-semibold text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
            >
              Retry
            </button>
          </section>
        )}
        {suites !== null && suites.length === 0 && (
          <p className="border-y border-[#dedbd2] py-8">No suites are registered.</p>
        )}

        {activeSuite !== null && (
          <>
            <ScopeBar
              activeDomain={activeDomain}
              onDomainChange={setActiveDomain}
              rowCount={unrestrictedResponse?.rows.length ?? 0}
              filtersOpen={filtersOpen}
              onToggleFilters={() => setFiltersOpen((open) => !open)}
            />

            <section className="border-b border-[#dedbd2] py-8">
              <p className="text-xs font-bold uppercase tracking-[0.14em] text-[#777970]">Suite</p>
              <h2 className="mt-2 text-2xl font-semibold">{suiteLabel(activeSuite.name)} reliability</h2>
              <p className="mt-2 max-w-2xl text-sm leading-6 text-[#62675f]">
                {primaryMetric
                  ? `Ranked by ${primaryMetric.label}; percentages show the distribution of model outcomes.`
                  : displayedResults
                    ? "No categorical breakdown is available in this scope; continuous metrics are reserved for the matrix view."
                    : "Explore model outcomes by domain, refusal handling, time window, and family."}
              </p>
            </section>

            {filtersOpen && (
              <FilterControls
                excludeRefusals={excludeRefusals}
                onExcludeRefusalsChange={setExcludeRefusals}
                windowDays={windowDays}
                onWindowDaysChange={setWindowDays}
                families={families}
                selectedFamilies={scopedSelectedFamilies}
                onToggleFamily={toggleFamily}
              />
            )}

            <Legend includeRefusals={!excludeRefusals} />

            <section aria-live="polite" className="border-t border-[#dedbd2] py-6">
              {displayedErrorState !== null ? (
                <div>
                  <p>{displayedErrorState.message}</p>
                  <button
                    type="button"
                    onClick={retryResults}
                    className="mt-4 rounded-md bg-[#283b32] px-4 py-2 text-sm font-semibold text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
                  >
                    Retry results
                  </button>
                </div>
              ) : displayedResults === null ? (
                <p role="status" className="text-sm text-[#62675f]">Loading results…</p>
              ) : displayedResults.rows.length === 0 ? (
                <p className="text-sm text-[#62675f]">No results match these filters.</p>
              ) : (
                <>
                  <p className="text-sm text-[#62675f]">
                    Showing {displayedResults.rows.length.toLocaleString()} model records.
                  </p>
                  {primaryMetric && (
                    <ModelBarChart
                      rows={displayedResults.rows}
                      metricKey={primaryMetric.key}
                    />
                  )}
                  <Leaderboard
                    key={activeSuite.name}
                    rows={displayedResults.rows}
                    suite={activeSuite}
                  />
                </>
              )}
            </section>
          </>
        )}
      </div>
    </main>
  );
}
