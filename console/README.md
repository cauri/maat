# Maat operator console (v2)

> **Status:** vertical slice landed — app shell (#303), command/query API (#304), the data-table
> primitive (#305), and the **Stories** room with its workspace + inline correction (#308). The
> remaining rooms are placeholders pending their issues. **Epic:** [#302](https://github.com/cauri/maat/issues/302) (P13). **Decision:** `DECISIONS.md` D33.
>
> This is the **authoritative stack + architecture** for the console rebuild. Any agent picking up a P13
> issue starts here. Keep it current as the app lands.

## What this is

The operator console for the Maat veracity engine — **for the operator, not end-users**. A from-scratch
rebuild that replaces the inline-HTML FastAPI surface (`python/maat/web/app.py`) and **supersedes** the
"split the 4,181-line file" path ([#292](https://github.com/cauri/maat/issues/292)).

**The console is not the product.** The product is the iOS app (`apple/Maat`): a news **reader**
(Stories, with a plain-language credibility read) plus a **source-reliability ledger** (Sources, truth
over time). The console is where the operator *verifies and corrects* what that app delivers, and *runs
the engine* that produces it. `apple/Maat` is the design-language source of truth — match its plain
language on the product surfaces.

## The two altitudes (the organising idea)

- **Product mirror** — verify what users see, in the app's own language: **Stories** (credibility) and
  **Sources** (reliability).
- **Engine room** — inspect / run / shape what produces it: **Claims**, the **Graph**, **Pipeline**
  health, **Tuning**.
- Plus **Overview**, **Feedback**, **Business**, an **Audit** drawer, and **Sia** (the collaborator)
  on every page.

| Room | Issue | Altitude / role |
|------|-------|-----------------|
| Overview | [#307](https://github.com/cauri/maat/issues/307) | landing dashboard — KPIs, "needs you", trends, de-US readout |
| Stories | [#308](https://github.com/cauri/maat/issues/308) | product · credibility — reader view + why + inline correction |
| Sources | [#309](https://github.com/cauri/maat/issues/309) | product · reliability — one canonical tier + trajectory |
| Claims | [#310](https://github.com/cauri/maat/issues/310) | engine · the claim inspector (**not** "feed") |
| Pipeline | [#311](https://github.com/cauri/maat/issues/311) | engine · health & ops (Activity+Quality+Calibration+Updates) |
| Tuning | [#312](https://github.com/cauri/maat/issues/312) | engine · prompts + config + policy, sign-off-gated |
| Feedback | [#313](https://github.com/cauri/maat/issues/313) | inputs · triage + coordinated-attack detection |
| Business | [#314](https://github.com/cauri/maat/issues/314) | spend + acquisition |
| Graph | [#315](https://github.com/cauri/maat/issues/315) | cross-cutting · corroboration-graph explorer (room + lens) |
| Audit | [#316](https://github.com/cauri/maat/issues/316) | cross-cutting · global change log from the event stream |
| **Sia** | [#306](https://github.com/cauri/maat/issues/306) | the collaborator, everywhere |

Foundation: app shell [#303](https://github.com/cauri/maat/issues/303), command/query API
[#304](https://github.com/cauri/maat/issues/304), live data-table primitive
[#305](https://github.com/cauri/maat/issues/305).

## Stack

| Layer | Choice | Role |
|-------|--------|------|
| Framework | **Next.js** (App Router) | the app; Node route handlers host Sia's AI-SDK orchestration |
| UI | **shadcn/ui** (Radix + Tailwind) | components |
| Tables | **TanStack Table** | the shared list primitive — sort / group / filter / saved views (#305) |
| Data / cache | **TanStack Query** | fetching + caching against the command/query API |
| Charts | **Tremor / Recharts**; **visx** | dashboards + trends; bespoke plots (trajectories, calibration) |
| Graph | **React Flow** (small / flow); **sigma.js** or **Cytoscape** (large) | the corroboration graph (#315) |
| AI (Sia) | **Vercel AI SDK** (`ai`) | streaming + tool-calling; Claude as the model |
| Command palette | **cmdk** | ⌘K — navigate + run any action |
| Live | **SSE** | subscribe to the event stream for live projections |
| Auth | existing **WireGuard** network + **Google OIDC** allowlist | D31/D32, at `admin.maat.press` |

## Architecture — the Python/JS split

**The veracity + event logic stays in Python; the JS app is UI + Sia only.**

```
browser ──┬─ Next.js console (shadcn UI + Sia via AI SDK)         [console/]
          │        │  queries / commands (HTTP) + SSE
          ▼        ▼
   FastAPI command/query API  ──────────────────────────────────  [python/maat/…]
          │  read projections                │  emit ADMIN_* (events.py)
          ▼                                   ▼
   Postgres projections  ◀── folds ──  append-only event log  ◀──  NATS / kernel / agents
```

- **Command/query API (#304)** = new FastAPI routes that **reuse `python/maat/events.py`** (emit
  `ADMIN_*`) and **read the existing Postgres projections**. One source of truth for the event spine
  (D5 / D28), right next to the kernel and agents.
- **`console/` Next.js app** = the UI (shadcn) and Sia's orchestration (AI SDK in Node route handlers).
- **Sia's tools are HTTP calls to the command API** — she runs the *exact same audited path* a human
  operator does. No second backend; Node never touches Postgres/NATS directly.
- **Served at `admin.maat.press`** behind Caddy + WireGuard + Google SSO (D31/D32).

## Conventions every agent must follow

1. **Every mutation is an audited `ADMIN_*` event** (D5/D28). The UI and Sia never write state except by
   emitting a command. Audit, replay, and A/B-on-replay fall out for free.
2. **Propose-don't-apply for the veracity core** (D28/D18): gate floor, scoring, and prompts need
   explicit **sign-off**. Reuse the "minor vs needs-sign-off" split. Sia *stages* a change and shows the
   diff (+ golden-test / A/B-on-replay result); a human applies.
3. **Plain language on the product surfaces** (D25/D26). Stories/Sources show words, tiers and
   trajectories ("Well corroborated"; a reliability tier + sparkline) — **not** raw score-bars or
   percentages. Operator-only engine rooms (Claims, Pipeline) may show numbers.
4. **It is the Claims page — never "feed".** "Feed" is only the app's reader. Claims is operator-only;
   users never see the article→claim firehose.
5. **One canonical reliability number.** Sources merges today's Sources + Reputation — never reintroduce
   two different reputation values for the same outlet.
6. **Sia is a collaborator, not an assistant.** A named teammate with a point of view who co-owns
   corrections. Her runtime persona/prompt is **co-designed with cauri** (D29 + `docs/prompt-template.md`)
   — do not write it solo.
7. **Myth-named agents** (D1): Sia (*insight / perception*) joins Anubis / Thoth / Assessor / Ammit.
   Note Sia ≠ the existing **Thoth** (scribe/store) — no collision.

## Repo layout

- `console/` — this app (Next.js). Scaffolded by #303.
- `python/maat/` — the FastAPI **command/query API** (#304): a new module (e.g. `console_api`) reusing
  `events.py` + projections. Do **not** extend `python/maat/web/app.py` for new console work — it is the
  legacy surface being replaced.

## Build order

**Vertical slice first** — it proves the three hardest, most-reused pieces at once (live table, the
command/query contract, and Sia making a real change under sign-off):

1. **#304** command/query API (read projections + emit `ADMIN_*`)
2. **#306** Sia (actionable, sign-off-gated) — tools = the command API
3. **#308** Stories room + Story workspace — over the **#303** shell + **#305** table

Then each remaining room repeats the proven pattern: **#307** Overview, **#309** Sources, **#310**
Claims, **#311** Pipeline, **#312** Tuning, **#313** Feedback, **#314** Business, **#315** Graph,
**#316** Audit.

**Dovetails:** P10 ([#182](https://github.com/cauri/maat/issues/182), fill the stub backends) and P11
([#181](https://github.com/cauri/maat/issues/181), operator enactment & sign-off — Tuning + Sia depend
on it).

## Getting started

```bash
cd console
npm install            # one-time
npm run dev            # dev server on http://localhost:3000 (use localhost, not 127.0.0.1 — see below)
npm run lint           # eslint
npm run typecheck      # tsc --noEmit
npm run build          # production build (Turbopack, standalone output)
```

Open **http://localhost:3000** — `/` redirects to `/overview`. Every room is reachable from the rail or
the ⌘K palette. With no `MAAT_ADMIN_SESSION_SECRET` set the admin gate **falls open** (you're a "Local
operator"), exactly like the Python side in dev.

> Use `localhost`, not `127.0.0.1`, in dev: Next 16 blocks cross-origin `_next` dev resources from a bare
> IP, which silently breaks HMR/hydration. (Production is unaffected.)

### What's here (#303)

- **App shell** — rail nav grouped by the two altitudes (Product mirror · Engine room · Inputs), a topbar
  with live status, and the cross-cutting surfaces: **⌘K** palette (cmdk), **Audit** drawer, and the
  **Sia** dock (a placeholder until #306 — no persona is authored here; that's co-designed with cauri).
- **Data layer** — TanStack Query provider; an **SSE** client (`hooks/use-event-stream.ts`) that
  auto-reconnects with backoff and drives the live indicator + Audit drawer. It targets the #304 endpoint
  (`NEXT_PUBLIC_CONSOLE_SSE_PATH`, default `/console/api/events`) and reports "offline" until that lands.
- **Theming** — `next-themes`, dark by default; rail-collapse persisted via a cookie so the server renders
  the right width (no flash).
- **Rooms** — all nine are placeholders that link to their tracking issue; each is replaced by its own
  P13 issue.

### Auth — reusing the existing gate (D31/D32)

The console does **not** stand up a second identity system. The FastAPI app
(`python/maat/serving/admin_auth.py`) owns the Google-OIDC dance and issues a stateless, HMAC-signed
`maat_admin` cookie. This app:

- verifies that **same** cookie in Edge middleware (`src/proxy.ts`) using the **same**
  `MAAT_ADMIN_SESSION_SECRET` (a faithful port in `src/lib/admin-token.ts`), redirecting to the
  FastAPI-owned `/admin/login` when absent;
- reads it again server-side (`src/lib/admin-session.ts`) to show the signed-in operator.

Set the same `MAAT_ADMIN_*` env on both processes. See `.env.example`. ⚠️ `src/lib/admin-token.ts` mirrors
the Python `verify_cookie`; coordinate with **#282** (admin-auth hardening) before changing either side.

### Deploy + cutover

`Dockerfile` builds the standalone server (loopback `:3100`). The console is **not** yet fronted at
`admin.maat.press` — that's the **#292 cutover** (last in the lane), which points Caddy at this app, routes
`/admin/*` + the command/query API to FastAPI, and retires the inline-HTML console in
`python/maat/web/app.py`. Until then the box keeps serving the legacy console so the operator's working
tools (e.g. Prompts) stay live. CI lints + type-checks + builds the console on every PR (`.github/workflows/ci.yml`).

## References

- **Product:** `BRIEF.md`; the iOS app `apple/Maat` (design-language source of truth).
- **Decisions:** `DECISIONS.md` — D33 (this), D28 (console = events, propose-don't-apply), D29 (editable
  prompts), D31/D32 (admin auth + URL), D5 (event sourcing), D25/D26 (reader + reputation co-equal).
- **Plan / backlog:** `PLAN.md`; epic [#302](https://github.com/cauri/maat/issues/302) and sub-issues
  #303–#316.
