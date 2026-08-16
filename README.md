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
   UK-stolen bikes — plus the UK consignment shops **Cycle Exchange** and
   **MyNextBike** (Shopify JSON feeds, newest 250 products each, alert-only:
   consignment shops list real brand names, so alert terms suffice and their
   inventories stay out of the digest) — and optionally **eBay** (official
   Browse API: UK broad queries + worldwide exact-term queries, plus
   price-banded newest-first "road bike" sweeps of everything just listed
   in the UK) for every query in `config.json`.
   Gumtree rate-limits: the script paces its requests and backs off for an
   hour if served a bot challenge (`gumtree_backoff.json`).
2. Dedupes against `state.json` so you only hear about *new* listings.
3. Classifies each new listing:
   - **ALERT** — title/description contains an alert term: instant ntfy
     push (+ a GitHub issue as a redundant channel — GitHub-app push +
     email — so one dropped notification can't cost a match), capped at 3
     individual pushes per sweep with a summary beyond that. UK sources
     alert on `univox`/`swift`/`e1800`; EU sources only on `univox`
     (Swift-brand bikes are common there, "Univox" is also a guitar-amp
     brand, and DT Swiss E1800 wheelsets turned out to be everywhere on
     DE/NL markets — 30 alerts in one sweep — hence per-source
     `alert_terms`). `e1800` still alerts on UK sources to catch the
     wheelset if the bike is parted out; the EU `dt swiss e1800` queries
     feed the digest quietly.
   - **digest** — broad match (e.g. any carbon/105/disc road bike in London
     within the plausible resale price band £300–£2,500): collected quietly
     into the report, no push (counts ride the hourly status ping).
     Thieves usually strip the brand from listings, so eyeball these.
   - **AI triage**: before a non-`univox` alert pushes, Claude judges
     whether the listing could plausibly be the bike or its parts (the
     `ANTHROPIC_API_KEY` Actions secret / `BIKEWATCH_ANTHROPIC_KEY` env
     var). Clear mismatches — other branded bikes that merely share common
     components, e-bikes, motorbikes — are marked **AI: unlikely** in the
     report and don't push. Fail-open: no key or an API error means the
     alert pushes as before; `univox` hits always push, no AI veto.
4. Regenerates `reports/findings.html` — review everything with photos
   from any device: https://viziolileonardo.github.io/bike-watch/reports/findings.html
   (GitHub Pages, updates ~1 min after each sweep; or locally via
   `git pull && open reports/findings.html`)

Detection details (pressure-tested):
- Alert terms match fuzzily: one-letter typos of "univox" (unibox, univax…)
  still alert. "swift" stays exact-match (edit distance 1 reaches "shift").
- **Catch-all sweeps** fetch the newest listings regardless of keywords, so
  brand-stripped titles ("racing bicycle barely used") still get checked:
  the 60 newest London bicycle listings (£200–£3,000) on Gumtree, the
  newest price-banded bikes on Kleinanzeigen, the newest road bikes on
  Marktplaats, and eBay UK's newest price-banded "road bike" results. Only
  road-bike-ish catch-all finds (road/carbon/gravel/105/Di2/… in any of
  EN/DE/NL) enter the digest — kids/city/e-bikes can't be the UniVox;
  alert terms are still checked against *every* listing first.
- Gumtree's fetch order **rotates each sweep**, so when its bot manager
  cuts a run short after a fetch or two, the tail queries (national rare
  terms, catch-all) still get their turn across successive sweeps instead
  of being starved every time.
- Relistings are suppressed: exact (same title + price under a new listing
  ID) and fuzzy (same source, ≥80% title-token overlap with a finding from
  the last 14 days — catches retitled/repriced re-posts). Alert-term hits
  are never fuzzy-suppressed.
- **Evidence preservation**: the moment a listing alerts, its page is
  snapshotted into the Wayback Machine and its photo committed to
  `reports/evidence/` — suspect ads often vanish within hours, and a
  deleted ad stays reviewable and citable to police this way.
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
- **Self-health**: any enabled source contributing zero results for ~2 h —
  whether empty, crashing, stuck in a bot-challenge backoff loop, or
  misconfigured since day one — triggers a ⚠️ push, repeated daily while it
  stays dark; a quiet daily check-in after 09:00 confirms the monitor is
  alive — silence is never ambiguous. An **hourly status ping** (min priority — visible in the
  ntfy app, never buzzes) says what was checked and why nothing louder came:
  e.g. "Checked gumtree 62, kleinanzeigen 3, marktplaats 4 listings. New
  this sweep: 0 alert-term hits (univox/swift), 1 broad match (report only)."
  Turn off with `notify.hourly_status: false`. Concurrent runs are prevented by a lockfile; state
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
3. `gh secret set EBAY_APP_ID` and `gh secret set EBAY_CERT_ID` (paste each
   value when prompted). **Never put them in `config.json` — the repo is
   public.** Their presence as secrets auto-enables the source.

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
| Buycycle | Used-road-bike marketplace; search "Swift" weekly, set brand alert if available (Cycle Exchange + MyNextBike are automated now) |
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
  loudly with a tap-to-open listing link; digest items stay in the report
  (no per-sweep push — the hourly status carries the counts).
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
