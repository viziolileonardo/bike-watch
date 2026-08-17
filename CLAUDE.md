# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A live, single-file monitor (`bikewatch.py`) that sweeps marketplace listings for the user's stolen bike (Swift UniVox Comp, details/crime ref in `config.json`). It runs **every 20 minutes in GitHub Actions** (`.github/workflows/sweep.yml`, **public** repo `viziolileonardo/bike-watch` — public for the unlimited free Actions minutes, so everything committed is world-readable); each cloud run commits `state.json` + the report back to `main`, so the cloud owns the state. The ntfy topic (effectively the notification password) must never appear in any committed file: it lives in the `NTFY_TOPIC` Actions secret and reaches the script as the `BIKEWATCH_NTFY_TOPIC` env var (same var for local runs). This directory is a checkout of that repo — `git pull` before working, and expect upstream sweep commits roughly hourly. The old local launchd agents (`com.leonardo.bikewatch`, `.awake`) are retired/disabled but their plists remain in `~/Library/LaunchAgents`. The README covers user-facing behavior in depth — read it before changing detection logic.

**This is production.** A plain `python3 bikewatch.py` does real network fetches, can push real notifications to the user's phone (ntfy), and mutates `state.json` — which now conflicts with the cloud's commits. When testing changes, work against a copy of the directory or temporarily blank `notify` in a test config; never leave a locally-mutated `state.json` uncommitted. Note that a run with an *empty* state is a "baseline run" that suppresses notifications — useful for testing, but it records every current listing quietly. Pushing to `main` deploys immediately (the next scheduled sweep runs whatever is on `main`).

## Commands

```sh
gh workflow run sweep                     # trigger a cloud sweep now
gh run list --workflow sweep --limit 5    # recent sweeps
gh run view --log                         # a sweep's full log output
git pull && open reports/findings.html    # latest report
python3 bikewatch.py                      # local sweep — dev only, see caveat above
python3 bikewatch.py --test-notify        # test notifications without sweeping
gh workflow run sweep -f test_notify=true # same, but from the cloud runner

gh workflow disable sweep                 # pause the schedule
gh workflow enable sweep                  # resume
```

There is no test suite, linter, or build. Verification is a manual sweep plus reading the log and report.

## Hard constraints

- **Stdlib only, no pip installs** — so a bare `python3` anywhere (cloud runner or Mac) runs it. Pillow is the one optional import (`blue_score` returns None without it; the workflow installs it best-effort); keep any new dependency optional with the same degrade-gracefully pattern.
- **Must stay Linux-clean** — sweeps run on `ubuntu-latest`. macOS-only tools (`osascript`, `terminal-notifier`) must stay behind `shutil.which()` guards, as in `notify_macos()`.
- **All HTTP goes through `http_get()`, which shells out to system `curl`** — Gumtree's bot manager blocks Python's TLS fingerprint but accepts curl. Don't "simplify" this to urllib/requests. The persistent cookie jar (`cookies.txt`) is attached **only** for Gumtree (`jar=True`): Kleinanzeigen's A/B cookies flip its search pages to a JS-rendered variant with zero parseable listings, which once silently killed that source — never share the jar across sources.
- Scraper sources (Gumtree, Kleinanzeigen, Marktplaats) parse live page HTML with regexes; only eBay uses an official API. Site layout changes silently break a source — that's what the per-source health tracking in `update_health()` exists to catch. If you touch a scraper, verify against a real fetch.

## Architecture (single file, `bikewatch.py`)

Data flow in `main()`: each enabled source's `search_*(cfg)` returns dicts with a common shape (`id` prefixed `source:`, title, description, location, price, image, …) → dedupe against `state.json` by id *and* by (title, price) fingerprint to suppress relistings → `classify()` returns `"alert"` (alert-term hit or one-edit fuzzy match), `"digest"` (kept for eyeballing if in price band), or None (dropped) → fuzzy relist suppression (`is_relisting()`, digest-level only) → `is_hot()` upgrades cheap quality road bikes near the theft area to alerts → `rule_close()` (free mechanical prefilter: named other-brand complete bikes) then `ai_verdict()` (Claude via `BIKEWATCH_ANTHROPIC_KEY`; cached system prefix, thinking disabled; fail-open, univox hits exempt, capped per sweep) mark implausible findings — alert *and* digest — `fp`: kept in state for dedupe/evidence but auto-closed, hidden from the report, never pushed; `--triage-backlog` bulk-reviews everything unreviewed → `blue_score()` colour-scores thumbnails (capped per sweep) → notifications (max 3 individual alerts + overflow summary; digest is report-only, no push) → `write_report()` regenerates the whole HTML report from state → atomic `save_state()`.

Key mechanics that span functions:

- **Per-source config override**: `classify()` uses the source's own `alert_terms`/price band from `config.json` when present, else global. This is deliberate — EU sources alert only on "univox" because "Swift" bikes are common there. The `key_by_source` map in `main()` links display names back to config keys; keep it in sync when adding a source.
- **Catch-all breadth**: every marketplace source also sweeps its newest listings keyword-free (Gumtree London pages, Kleinanzeigen price-banded, Marktplaats racefietsen category, eBay price-banded `broad_queries`). Alert terms run against everything; only `ROADISH_RE`-matching catch-all items enter the digest (query label prefix `(catch-all` is what triggers that gate in `main()`).
- **Gumtree backoff**: a bot challenge (page contains `kramericaindustries`) writes `gumtree_backoff.json` and skips Gumtree for an hour; fetches within a sweep are paced 8s apart; the fetch order rotates each sweep so challenge-shortened runs don't starve the same tail queries. `update_health()` counts a backoff skip *and* a challenge-shortened run as coverage gaps (partial results must not reset the streak).
- **Health/heartbeat**: `update_health()` warns after ~2h of an enabled source contributing zero results for *any* reason (empty/crashed/backoff-loop/never-worked — each is a coverage gap), re-warns daily while dark, and sends a quiet daily check-in after 09:00; `send_status()` additionally pings an hourly min-priority "what was checked and why it stayed quiet" status — so silence is never ambiguous. Alerts also open a GitHub issue (`notify_github_issue`, cloud-only) as a redundant channel to ntfy.
- **Crash/concurrency safety**: `.lock` + flock prevents overlapping sweeps; state writes are atomic via temp-file rename; a corrupt `state.json` is set aside as `.json.corrupt` and the run restarts quietly as a baseline.
- **Report state**: "reviewed" checkboxes persist in the *browser's* localStorage keyed by listing id, not in `state.json` — regenerating the report doesn't lose them.

Adding a source = a `search_*` function returning the common dict shape, an entry in `sources` and `key_by_source` in `main()`, and a config block with `enabled`. Exception: Shopify shops (Cycle Exchange, MyNextBike, …) are config-only — add a `{name, base_url}` entry under `shopify.shops`; `search_shopify` reads each shop's public `/products.json` and `key_by_source` is extended dynamically. Shopify shops are `alert_only`: consignment inventories never enter the digest, only alert-term hits surface.

## Files on disk

Committed (world-readable — the repo is public): `config.json` (queries, alert terms; the ntfy topic is deliberately blank here — secret, see above), `state.json` (all findings ever seen + health; written by the cloud), `gumtree_backoff.json`, `reports/findings.html` (fully regenerated each sweep), `.github/workflows/sweep.yml`. Gitignored/local-only: `cookies.txt`, `.lock`, `bikewatch.log`, `launchd.log`, and `recovery/` (police/CCTV correspondence — not code, don't touch it, never commit it).
