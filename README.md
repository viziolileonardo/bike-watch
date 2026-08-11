# BikeWatch

Monitors London marketplace listings for a stolen **Swift UniVox Comp (Blue
Steel)** — carbon frame, Shimano 105 hydraulic disc, DT Swiss E1800 wheels.
Stolen outside Cottons Shoreditch, 10 Aug 2026, 19:30–20:50.

## How it works

`bikewatch.py` runs **every 20 minutes in the cloud** via GitHub Actions
(`viziolileonardo/bike-watch`, public repo — public = unlimited free Actions
minutes; the ntfy topic, which is effectively the notification password,
lives only in the `NTFY_TOPIC` Actions secret and your phone's ntfy app,
never in a committed file) — sweeps continue with the laptop asleep, off, or
abroad. Each sweep the workflow commits `state.json` and the report back to
the repo, so nothing is lost between runs. Each sweep:

1. Searches **Gumtree** (London, bicycles category), **Kleinanzeigen** (DE)
   and **Marktplaats** (NL) — the main EU laundering routes for high-value
   UK-stolen bikes — and optionally **eBay** (official Browse API: UK broad
   queries + worldwide exact-term queries) for every query in `config.json`.
   Gumtree rate-limits: the script paces its requests and backs off for an
   hour if served a bot challenge (`gumtree_backoff.json`).
2. Dedupes against `state.json` so you only hear about *new* listings.
3. Classifies each new listing:
   - **ALERT** — title/description contains an alert term: instant macOS
     notification (+ Telegram if configured), capped at 3 per sweep with a
     summary beyond that. UK sources alert on `univox`/`swift`; EU sources
     only on `univox` (Swift-brand bikes are common there, and "Univox" is
     also a guitar-amp brand — hence per-source `alert_terms`).
   - **digest** — broad match (e.g. any carbon/105/disc road bike in London
     within the plausible resale price band £300–£2,500): collected quietly.
     Thieves usually strip the brand from listings, so eyeball these.
4. Regenerates `reports/findings.html` — review everything with photos:
   `git pull && open reports/findings.html`

Detection details (pressure-tested):
- Alert terms match fuzzily: one-letter typos of "univox" (unibox, univax…)
  still alert. "swift" stays exact-match (edit distance 1 reaches "shift").
- A **catch-all sweep** fetches the 60 newest London bicycle listings
  (£200–£3,000) every cycle regardless of keywords, so brand-stripped
  titles ("racing bicycle barely used") still enter the digest.
- Relistings (same title + price under a new listing ID) are suppressed.
- Rows near the theft area (Shoreditch/Hackney/E1/E2/E8/E9…) are flagged
  **NEAR THEFT** in the report. Tick rows to mark them reviewed (persists
  in your browser); alerts are pinned to the top.
- Sweeps run in GitHub's cloud, so the Mac's sleep state is irrelevant. The
  original launchd agents (`com.leonardo.bikewatch` + the `caffeinate` awake
  agent) are retired but their plists remain in `~/Library/LaunchAgents` —
  `launchctl enable` + `bootstrap` brings local sweeping back if ever needed.
- Rare terms ("univox", "swift univox", "dt swiss e1800") are searched
  **UK-wide**, not just London — thieves relist from anywhere.
- **HOT rule**: a quality road bike (carbon/105/hydraulic/ultegra) at
  £300–£1,500 located near the theft area is upgraded to an alert.
- **BLUE chip**: thumbnails are colour-scored (Pillow); listings whose photo
  is ≥8% muted-blue pixels get a BLUE chip and an "only blue-ish" report
  filter. Assists eyeballing only — nothing is dropped for lacking blue.
- **Self-health**: if a source that used to return results comes back empty
  for ~2 h straight you get a ⚠️ push (layout change or block); a quiet
  daily check-in after 09:00 confirms the monitor is alive — silence is
  never ambiguous. Concurrent runs are prevented by a lockfile; state
  writes are atomic; a corrupt state file restarts quietly without a
  notification flood.

## Commands

```sh
gh workflow run sweep                     # trigger a cloud sweep now
gh run list --workflow sweep --limit 5    # recent sweeps (status + timing)
gh run view --log                         # read a sweep's log
git pull && open reports/findings.html    # review all tracked listings
python3 bikewatch.py --test-notify        # test notifications (local)
gh workflow run sweep -f test_notify=true # test notifications (from the cloud)
```

Avoid plain `python3 bikewatch.py` locally now except for development — it
mutates `state.json`, which the cloud also owns, and the two will conflict
on the next `git pull`.

Pause/resume the schedule:

```sh
gh workflow disable sweep    # pause
gh workflow enable sweep     # resume
```

## Enabling eBay (recommended — 5 minutes)

eBay blocks scrapers, but their official API is free and reliable:

1. Register at https://developer.ebay.com (free, instant for personal use).
2. Create an app → copy the **App ID (Client ID)** and **Cert ID (Client
   Secret)** for the *Production* environment.
3. In `config.json` set `ebay.enabled: true` and paste both values.

Also do the zero-code version right now: search "swift univox" on eBay,
tap **Save this search**, and enable email alerts — eBay then notifies you of
new matches itself.

## Facebook Marketplace

Can't be scripted (login-walled), but it's where most stolen bikes surface:

- In the FB app: Marketplace → search "univox" / "swift road bike" → **Save
  search** with notifications on. Do the same with a broad "road bike" search
  filtered to ~1 mile of Shoreditch, sorted by newest.
- Claude with Chrome access can sweep Marketplace in your logged-in browser —
  ask it to "sweep Facebook Marketplace for my bike".

## Channels that can't be scripted — set these up manually

| Channel | What to do |
|---|---|
| Facebook Marketplace | Saved searches + notifications (see above) — the #1 venue |
| eBay | "Save this search" email alerts for `swift univox`, `univox` |
| Gumtree | Built-in email alerts on the same searches (backup for rate-limiting) |
| Preloved | Saved-search email alerts (JS app, not scrapeable) |
| Buycycle / Cycle Exchange | Used-road-bike marketplaces; search "Swift" weekly, set Buycycle brand alert if available |
| Google Alerts | google.com/alerts for `"swift univox"` and `"univox comp"` — catches forums, new sites |
| OLX.pl / Allegro (Poland) | Blocked to scripts; known destination for UK-stolen bikes — manual weekly search for "univox" |
| Cash Converters | JS app; check the Shoreditch/Hackney branches in person — sellers must show ID, so police can trace |
| Brick Lane market | In person, Sunday early morning — the classic London stolen-bike outlet, walking distance from the theft |
| Stolen-bike communities | Post to Stolen Bikes UK (Facebook), stolen-bikes.co.uk, StolenRide (London), r/london cycling threads — thousands of eyes |
| BikeRegister / Immobilise | Register the frame number as stolen — police check recovered bikes against these |

## Tuning

- `config.json → gumtree.queries` / `ebay.queries`: add/remove searches.
- `alert_terms`: words that trigger an instant alert.
- `broad_price_min/max`: price band for keeping broad-query results.
- Phone notifications (ntfy, already configured): the **ntfy** app on your
  phone is subscribed to the topic; the same topic is stored in the repo's
  `NTFY_TOPIC` Actions secret (`gh secret set NTFY_TOPIC`). Alerts push
  loudly with a tap-to-open listing link; digest summaries arrive silently.
  The topic name is effectively the password — never commit it (the repo is
  public). For local test runs: `BIKEWATCH_NTFY_TOPIC=<topic> python3
  bikewatch.py --test-notify`.
- Telegram alternative: create a bot with @BotFather, put the token + your
  chat id in `notify`.
- Cadence: every 20 minutes (:17/:37/:57 — off-peak minutes, since GitHub
  delays :00 crons the most). Public repos get unlimited free Actions
  minutes, which is why the repo is public with the ntfy topic in a secret.

## If you find it

**Do not confront the seller or arrange a private meeting.** Screenshot the
listing immediately (sellers delete them), then call 101 (or 999 if a meeting
is imminent) with your crime reference number — police can seize the bike and
attend a staged handover. Proof of ownership (photos, receipt, frame number)
is what gets it returned to you.
