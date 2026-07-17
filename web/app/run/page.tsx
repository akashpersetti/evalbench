"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  fetchRun,
  fetchRunStatus,
  fetchSuites,
  requestMagicLink,
  startRun,
  verifyMagicLink,
  type Domain,
  type MetricRecord,
  type RunStatus,
  type SuiteDescriptor,
} from "@/lib/api";

const DOMAINS: Domain[] = [
  "overall",
  "software",
  "finance",
  "legal",
  "medical",
  "physics",
];

const OWNER_EMAIL = "ahadagal@alumni.iu.edu";

function errorMessage(reason: unknown): string {
  return reason instanceof ApiError
    ? reason.message
    : "Unable to reach the EvalBench API.";
}

export default function RunPage() {
  const [token, setToken] = useState("");
  const [authLoading, setAuthLoading] = useState(true);
  const [authError, setAuthError] = useState("");
  const [linkSent, setLinkSent] = useState(false);
  const [sendingLink, setSendingLink] = useState(false);
  const [email, setEmail] = useState(OWNER_EMAIL);
  const authInitialized = useRef(false);

  const [suites, setSuites] = useState<SuiteDescriptor[]>([]);
  const [suite, setSuite] = useState("");
  const [domain, setDomain] = useState<Domain>("overall");
  const [modelsInput, setModelsInput] = useState("openai/gpt-4o");
  const [judgeModel, setJudgeModel] = useState("");
  const [submitError, setSubmitError] = useState("");

  const [runId, setRunId] = useState<string | null>(null);
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [records, setRecords] = useState<MetricRecord[] | null>(null);

  useEffect(() => {
    if (authInitialized.current) return;
    authInitialized.current = true;

    async function initializeAuth() {
      const url = new URL(window.location.href);
      const magicToken = url.searchParams.get("magic");

      if (magicToken) {
        try {
          const adminToken = await verifyMagicLink(magicToken);
          localStorage.setItem("run_token", adminToken);
          setToken(adminToken);
        } catch {
          setAuthError("This magic link is invalid or has expired.");
        } finally {
          url.searchParams.delete("magic");
          history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
          setAuthLoading(false);
        }
        return;
      }

      const saved = localStorage.getItem("run_token");
      if (saved) {
        setToken(saved);
      }
      setAuthLoading(false);
    }

    void initializeAuth();
  }, []);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    fetchSuites(controller.signal)
      .then((loaded) => {
        setSuites(loaded);
        setSuite((current) => current || loaded[0]?.name || "");
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [token]);

  useEffect(() => {
    if (!runId || status?.status === "done" || status?.status === "error") return;

    const interval = setInterval(() => {
      fetchRunStatus(runId)
        .then(setStatus)
        .catch(() => undefined);
    }, 3000);

    return () => clearInterval(interval);
  }, [runId, status?.status]);

  useEffect(() => {
    if (status?.status !== "done" || !runId) return;
    fetchRun(runId)
      .then(setRecords)
      .catch(() => undefined);
  }, [status?.status, runId]);

  async function handleMagicLinkRequest(event: React.FormEvent) {
    event.preventDefault();
    setAuthError("");
    setSendingLink(true);
    try {
      await requestMagicLink(email);
      setLinkSent(true);
    } catch {
      setAuthError("Unable to send a magic link. Please try again.");
    } finally {
      setSendingLink(false);
    }
  }

  function handleSignOut() {
    localStorage.removeItem("run_token");
    setToken("");
    setRunId(null);
    setStatus(null);
    setRecords(null);
    setLinkSent(false);
  }

  const handleSubmit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setSubmitError("");

      const models = modelsInput
        .split(",")
        .map((model) => model.trim())
        .filter(Boolean);

      if (models.length === 0) {
        setSubmitError("Enter at least one model.");
        return;
      }

      try {
        const newRunId = await startRun(
          { suite, domain, models, judgeModel: judgeModel.trim() || undefined },
          token,
        );
        setRunId(newRunId);
        setStatus({ run_id: newRunId, status: "pending", completed: 0, total: 0 });
        setRecords(null);
      } catch (reason) {
        setSubmitError(errorMessage(reason));
      }
    },
    [suite, domain, modelsInput, judgeModel, token],
  );

  if (authLoading) {
    return (
      <main className="min-h-screen bg-[#f7f5ef] text-[#202822]">
        <div className="mx-auto max-w-2xl px-5 py-16">
          <p className="text-sm text-[#62675f]">Loading…</p>
        </div>
      </main>
    );
  }

  if (!token) {
    return (
      <main className="min-h-screen bg-[#f7f5ef] text-[#202822]">
        <div className="mx-auto max-w-2xl px-5 py-16">
          <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#777970]">
            EvalBench
          </p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight">Run a suite</h1>
          <p className="mt-2 text-sm text-[#62675f]">
            Sign in with a magic link to trigger a run.
          </p>

          {authError && <p className="mt-4 text-sm text-[#bd6b65]">{authError}</p>}

          {linkSent ? (
            <p className="mt-6 text-sm text-[#62675f]">
              Check your email for a sign-in link.
            </p>
          ) : (
            <form onSubmit={handleMagicLinkRequest} className="mt-6 flex gap-3">
              <input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                className="flex-1 rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
              />
              <button
                type="submit"
                disabled={sendingLink}
                className="rounded-md bg-[#283b32] px-4 py-2 text-sm font-semibold text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32] disabled:opacity-50"
              >
                {sendingLink ? "Sending…" : "Send sign-in link"}
              </button>
            </form>
          )}
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-[#f7f5ef] text-[#202822]">
      <div className="mx-auto max-w-2xl px-5 py-10">
        <header className="mb-7 flex items-end justify-between gap-4">
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#777970]">
              EvalBench
            </p>
            <h1 className="mt-2 text-3xl font-semibold tracking-tight">Run a suite</h1>
          </div>
          <button
            type="button"
            onClick={handleSignOut}
            className="text-sm text-[#777970] underline"
          >
            Sign out
          </button>
        </header>

        <form onSubmit={handleSubmit} className="space-y-4 border-y border-[#dedbd2] py-6">
          <label className="block text-sm">
            <span className="mb-1 block text-[#777970]">Suite</span>
            <select
              value={suite}
              onChange={(event) => setSuite(event.target.value)}
              className="w-full rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
            >
              {suites.map((descriptor) => (
                <option key={descriptor.name} value={descriptor.name}>
                  {descriptor.name}
                </option>
              ))}
            </select>
          </label>

          <label className="block text-sm">
            <span className="mb-1 block text-[#777970]">Domain</span>
            <select
              value={domain}
              onChange={(event) => setDomain(event.target.value as Domain)}
              className="w-full rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
            >
              {DOMAINS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>

          <label className="block text-sm">
            <span className="mb-1 block text-[#777970]">
              Models (comma-separated)
            </span>
            <input
              type="text"
              value={modelsInput}
              onChange={(event) => setModelsInput(event.target.value)}
              placeholder="openai/gpt-4o,anthropic/claude-sonnet-4-5"
              className="w-full rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
            />
          </label>

          <label className="block text-sm">
            <span className="mb-1 block text-[#777970]">
              Judge model (optional)
            </span>
            <input
              type="text"
              value={judgeModel}
              onChange={(event) => setJudgeModel(event.target.value)}
              placeholder="anthropic/claude-sonnet-4-5"
              className="w-full rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
            />
          </label>

          {submitError && <p className="text-sm text-[#bd6b65]">{submitError}</p>}

          <button
            type="submit"
            disabled={!suite || (status !== null && status.status !== "done" && status.status !== "error")}
            className="rounded-md bg-[#283b32] px-4 py-2 text-sm font-semibold text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32] disabled:opacity-50"
          >
            Run
          </button>
        </form>

        {status && (
          <section className="mt-6 border-b border-[#dedbd2] py-6">
            <p className="text-xs font-bold uppercase tracking-[0.14em] text-[#777970]">
              Status
            </p>
            {status.status === "error" ? (
              <p className="mt-2 text-sm text-[#bd6b65]">
                Run failed: {status.error}
              </p>
            ) : status.status === "done" ? (
              <p className="mt-2 text-sm text-[#6f9f76]">
                Run complete — {status.total} records.
              </p>
            ) : (
              <p className="mt-2 text-sm text-[#62675f]">
                {status.status === "pending" ? "Starting…" : "Running…"}{" "}
                {status.completed} of {status.total || "?"} complete.
              </p>
            )}
          </section>
        )}

        {records && records.length > 0 && (
          <section className="mt-6">
            <p className="text-xs font-bold uppercase tracking-[0.14em] text-[#777970]">
              Results
            </p>
            <ul className="mt-3 space-y-2 text-sm">
              {records.map((record) => (
                <li
                  key={record.id}
                  className="border-b border-[#e4e1d9] py-2 text-[#30352f]"
                >
                  {record.model} · {record.task_id} ·{" "}
                  {record.error ? `error: ${record.error}` : "ok"}
                </li>
              ))}
            </ul>
            <a
              href={`/?suite=${encodeURIComponent(suite)}`}
              className="mt-4 inline-block text-sm text-[#283b32] underline"
            >
              View in the main dashboard
            </a>
          </section>
        )}
      </div>
    </main>
  );
}
