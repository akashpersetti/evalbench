# Phase 3 Task 2 Implementer Report

## Changed files

- `web/components/ScopeBar.tsx`: added the horizontally scrollable domain tab row, localized record count, and accessible filter visibility toggle.
- `web/components/FilterControls.tsx`: added rate-mode radio buttons, native time-window select, deterministic family-color chips, and pressed-state semantics.
- `web/components/Legend.tsx`: added the fixed Clear / Partial / Failed / Refused legend order using shared segment CSS variables.
- `web/app/page.tsx`: replaced the Task 1 smoke state with dashboard scope/filter state, first-suite selection, abortable unrestricted and conditional restricted results fetches, family-history preservation, suite-change family reset, and loading/error/empty result states.

No backend files or `web/lib/api.ts` were changed.

## Verification

- `npm --prefix web run lint` — passed.
- `npm --prefix web run build` — passed.
- `git diff --check` — passed.
- `make api` and curl requests to `/suites` and `/results` — passed after the reloader completed startup; filter query smoke covered software/7d, finance/90d with refusals excluded, and legal with repeated family parameters.
- `npm --prefix web run dev -- --hostname 127.0.0.1 --port 3000` and curl to `/` — passed.
- Playwright screenshot attempt — unavailable because the environment lacks `libatk-1.0.so.0`; the browser process could not launch.

## Implementation notes

- The unrestricted result is always requested for the active suite/domain/window/rate combination and supplies both the record count and the union of family names.
- A restricted result request is made only for a strict, non-empty selected-family subset; selecting every available family is normalized to an unrestricted query.
- Family names are retained by active suite/domain/window key, including names seen under another rate mode, so toggling refusal handling does not remove chips.
- Result and error state is keyed to its request so aborted or stale responses cannot replace the current filter state.

## Commit

Feature commit: `feat: add dashboard scope and filters`

## Phase 3 Task 2 review follow-up

- `web/app/page.tsx` now rebases the family selection against the active suite/domain/window family list before deriving either chip state or the `families` query. A stale selection with no current-family intersection becomes all current families, rather than rendering no selected chips while serializing an unrestricted request; a non-empty subset remains restricted, and only all current families serializes as `families=[]`.
- `web/components/FilterControls.tsx` now receives the rebased current-scope selection and renders each chip directly from membership, keeping its pressed state aligned with the serialized query.
- `web/components/Legend.tsx` now uses a labeled semantic section with a list of legend items.

Follow-up verification:

- `npm --prefix web run lint` — passed.
- `npm --prefix web run build` — passed.
- `git diff --check` — passed.
