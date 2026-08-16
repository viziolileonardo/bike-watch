#!/usr/bin/env python3
"""BikeWatch — monitor London marketplaces for a stolen bike.

Stdlib only (no pip installs) so it runs reliably from launchd/cron.

Sources:
  - Gumtree (public search pages, London-filtered)
  - eBay UK (official Browse API — enable in config.json with free
    developer credentials from https://developer.ebay.com)

Each run: fetch every configured query, dedupe against state.json,
classify new listings as ALERT (title/description contains an alert
term, e.g. "univox") or DIGEST (broad-query match worth eyeballing),
send notifications for alerts, and regenerate reports/findings.html.

Usage:
  python3 bikewatch.py            # one sweep (what launchd runs)
  python3 bikewatch.py --test-notify
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
REPORT_PATH = BASE / "reports" / "findings.html"
LOG_PATH = BASE / "bikewatch.log"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 30


def log(msg):
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_get(url, headers=None, data=None, jar=False):
    """Fetch via the system curl — Gumtree's bot manager blocks Python's
    TLS fingerprint but accepts curl. `data` (bytes/str) makes it a POST.

    jar=True attaches the persistent cookie jar and is for Gumtree ONLY:
    its bot manager likes session continuity, but Kleinanzeigen's A/B
    cookies (__ka_srp-v3 / kameleoon) flip its search pages to a
    JS-rendered variant with zero parseable listings — a poisoned shared
    jar silently killed that source once. Everyone else goes cookie-less."""
    cmd = ["curl", "-sS", "--fail-with-body", "--compressed", "--max-time",
           str(TIMEOUT), "-A", UA]
    if jar:
        j = str(BASE / "cookies.txt")
        cmd += ["-b", j, "-c", j]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["--data", data if isinstance(data, str) else data.decode()]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 10)
    if proc.returncode != 0:
        raise OSError(f"curl exit {proc.returncode}: {proc.stderr.decode()[:200]}")
    return proc.stdout.decode("utf-8", errors="replace")


PRETHEFT_DROPPED = {}  # source key -> items dropped by the pre-theft filter this run


def before_theft(iso_str, stolen_iso):
    """True if iso_str parses and is earlier than the theft moment — a
    listing created before the theft cannot be the stolen bike. Sources
    with reliable creation dates (eBay API, Shopify feeds) use this to
    drop pre-theft listings outright; scraper sources have no dates."""
    from datetime import datetime
    try:
        return (datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                < datetime.fromisoformat(stolen_iso.replace("Z", "+00:00")))
    except (ValueError, AttributeError, TypeError):
        return False


def parse_price(text):
    m = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    return float(m.group(0).replace(",", "")) if m else None


def unescape(text):
    import html as htmllib
    return htmllib.unescape(text or "").strip()


# ---------------------------------------------------------------- Gumtree

GT_ARTICLE_RE = re.compile(r'<article data-q="search-result".*?</article>', re.S)
GT_STYLE_RE = re.compile(r"<style.*?</style>", re.S)


def gt_field(block, name):
    m = re.search(r'data-q="%s"[^>]*>([^<]*)' % re.escape(name), block)
    return unescape(m.group(1)) if m else ""


GT_BACKOFF_PATH = BASE / "gumtree_backoff.json"
GT_BACKOFF_MINUTES = 60


def search_gumtree(cfg):
    import time
    if GT_BACKOFF_PATH.exists():
        until = json.loads(GT_BACKOFF_PATH.read_text())["until"]
        if time.time() < until:
            log("gumtree: in bot-challenge backoff, skipping this sweep")
            return []
    fetches = []
    for query in cfg["queries"]:
        fetches.append((query, "https://www.gumtree.com/search?"
                        + urllib.parse.urlencode({
                            "search_category": cfg.get("category", "bicycles"),
                            "q": query,
                            "search_location": cfg.get("location", "London"),
                        })))
    for query in cfg.get("national_queries", []):
        # rare terms searched UK-wide: thieves relist far from the theft
        fetches.append((query + " (UK)", "https://www.gumtree.com/search?"
                        + urllib.parse.urlencode({
                            "search_category": cfg.get("category", "bicycles"),
                            "q": query,
                        })))
    if cfg.get("catch_all"):
        # no-keyword sweep of the newest listings in the whole category, so
        # brand-stripped titles with no matching keywords still get seen
        for pageno in (1, 2):
            fetches.append(("(catch-all newest)",
                            "https://www.gumtree.com/search?"
                            + urllib.parse.urlencode({
                                "search_category": cfg.get("category", "bicycles"),
                                "search_location": cfg.get("location", "London"),
                                "min_price": cfg.get("broad_price_min", 200),
                                "max_price": cfg.get("broad_price_max", 3000),
                                "sort": "date",
                                "page": pageno,
                            })))
    # rotate the fetch order each sweep: bot challenges often cut a run short
    # after a fetch or two, and a fixed order would starve the same tail
    # queries (national rare terms, catch-all) every time
    off = int(time.time() // 1200) % len(fetches)
    fetches = fetches[off:] + fetches[:off]
    results = []
    for i, (query, url) in enumerate(fetches):
        if i:
            time.sleep(8)  # pace requests; Gumtree's bot manager rate-flags bursts
        try:
            page = http_get(url, jar=True)
        except OSError as e:
            log(f"gumtree fetch failed for {query!r}: {e}")
            continue
        if "kramericaindustries" in page:
            GT_BACKOFF_PATH.write_text(json.dumps(
                {"until": time.time() + GT_BACKOFF_MINUTES * 60}))
            log(f"gumtree: bot challenge served, backing off {GT_BACKOFF_MINUTES} min")
            return results
        for raw in GT_ARTICLE_RE.findall(page):
            block = GT_STYLE_RE.sub("", raw)
            href_m = re.search(r'data-q="search-result-anchor" href="([^"]+)"', block)
            if not href_m:
                continue
            href = href_m.group(1)
            img_m = re.search(r'<img[^>]+src="([^"]+)"', block)
            listing_url = urllib.parse.urljoin("https://www.gumtree.com", href)
            id_m = re.search(r"/(\d+)/?$", href)
            results.append({
                "id": "gumtree:" + (id_m.group(1) if id_m else href),
                "source": "Gumtree",
                "query": query,
                "url": listing_url,
                "title": gt_field(block, "tile-title"),
                "description": gt_field(block, "tile-description"),
                "location": gt_field(block, "tile-location"),
                "price_text": gt_field(block, "tile-price"),
                "price": parse_price(gt_field(block, "tile-price")),
                "image": img_m.group(1) if img_m else "",
            })
    return results


# ---------------------------------------------------------------- Kleinanzeigen (DE)

KA_ARTICLE_RE = re.compile(r'<article class="aditem"[^>]*data-adid="(\d+)"[^>]*data-href="([^"]+)".*?</article>', re.S)


def euro_price(text):
    digits = re.sub(r"[^\d]", "", (text or "").split(",")[0])
    return float(digits) if digits else None


def search_kleinanzeigen(cfg):
    results = []
    fetches = []
    for query in cfg["queries"]:
        slug = re.sub(r"\s+", "-", query.strip().lower())
        fetches.append((query, "https://www.kleinanzeigen.de/s-fahrraeder/"
                        f"{urllib.parse.quote(slug)}/k0c217"))
    if cfg.get("catch_all"):
        # no-keyword sweep of the newest bike listings in the price band
        # (newest-first is the site default; the road-bike attribute filter
        # is served an empty page over curl, so this sees every bike type —
        # the roadish digest gate in main() keeps the noise out)
        fetches.append(("(catch-all newest)",
                        "https://www.kleinanzeigen.de/s-fahrraeder/"
                        f"preis:{cfg.get('broad_price_min', 200)}:"
                        f"{cfg.get('broad_price_max', 3200)}/c217"))
    for query, url in fetches:
        try:
            page = http_get(url)
        except OSError as e:
            log(f"kleinanzeigen fetch failed for {query!r}: {e}")
            continue
        for m in KA_ARTICLE_RE.finditer(page):
            adid, href = m.group(1), m.group(2)
            block = m.group(0)
            title_m = re.search(r'class="ellipsis"[^>]*>([^<]+)', block)
            price_m = re.search(r'price-shipping--price"[^>]*>\s*([^<]+)', block)
            loc_m = re.search(r'aditem-main--top--left[^"]*"[^>]*>\s*(?:<[^>]+>\s*)*([^<]+)', block)
            desc_m = re.search(r'aditem-main--middle--description"[^>]*>([^<]+)', block)
            img_m = re.search(r'<img[^>]+src="(https://img\.kleinanzeigen\.de[^"]+)"', block)
            price_text = unescape(price_m.group(1)) if price_m else ""
            results.append({
                "id": "kleinanzeigen:" + adid,
                "source": "Kleinanzeigen (DE)",
                "query": query,
                "url": urllib.parse.urljoin("https://www.kleinanzeigen.de", href),
                "title": unescape(title_m.group(1)) if title_m else "",
                "description": unescape(desc_m.group(1)) if desc_m else "",
                "location": unescape(loc_m.group(1)) if loc_m else "",
                "price_text": price_text or "?",
                "price": euro_price(price_text),
                "image": img_m.group(1) if img_m else "",
            })
    return results


# ---------------------------------------------------------------- Marktplaats (NL)

def search_marktplaats(cfg):
    results = []
    fetches = []
    for query in cfg["queries"]:
        fetches.append((query, "https://www.marktplaats.nl/l/fietsen-en-brommers/q/"
                        + urllib.parse.quote(query.strip().replace(" ", "+")) + "/"))
    if cfg.get("catch_all"):
        # no-keyword sweep of the road-bike category (newest land on page 1;
        # the embedded listings JSON parses identically to query pages)
        fetches.append(("(catch-all newest)",
                        "https://www.marktplaats.nl/l/fietsen-en-brommers/"
                        "fietsen-racefietsen/"))
    for query, url in fetches:
        try:
            page = http_get(url)
        except OSError as e:
            log(f"marktplaats fetch failed for {query!r}: {e}")
            continue
        m = re.search(r'"listings"\s*:\s*\[', page)
        if not m:
            log(f"marktplaats: no listings JSON for {query!r}")
            continue
        try:
            listings, _ = json.JSONDecoder().raw_decode(page[m.end() - 1:])
        except json.JSONDecodeError as e:
            log(f"marktplaats parse failed for {query!r}: {e}")
            continue
        for item in listings:
            price_cents = (item.get("priceInfo") or {}).get("priceCents")
            price = price_cents / 100 if isinstance(price_cents, (int, float)) else None
            images = item.get("imageUrls") or []
            image = images[0] if images else ""
            if image.startswith("//"):
                image = "https:" + image
            results.append({
                "id": "marktplaats:" + str(item.get("itemId")),
                "source": "Marktplaats (NL)",
                "query": query,
                "url": urllib.parse.urljoin("https://www.marktplaats.nl", item.get("vipUrl", "")),
                "title": item.get("title", ""),
                "description": item.get("description", ""),
                "location": (item.get("location") or {}).get("cityName", "") or "NL",
                "price_text": f"€{price:.0f}" if price else "?",
                "price": price,
                "image": image,
            })
    return results


# ---------------------------------------------------------------- Shopify shops
# (Cycle Exchange, MyNextBike, ... — UK second-hand/consignment bike shops)

def search_shopify(cfg):
    """Shopify's public /products.json is a stable structured feed — no
    scraping, no bot managers. Fetches each shop's newest products; these
    shops list bikes under their real brand names, so alert terms do the
    matching (the source is alert_only: no digest flood of whole shop
    inventories)."""
    results = []
    for shop in cfg["shops"]:
        base = shop["base_url"].rstrip("/")
        url = f"{base}/products.json?limit={cfg.get('limit', 250)}"
        try:
            data = json.loads(http_get(url))
        except Exception as e:
            log(f"shopify fetch failed for {shop['name']!r}: {e}")
            continue
        for p in data.get("products", []):
            if cfg.get("stolen_iso") and before_theft(
                    p.get("published_at", ""), cfg["stolen_iso"]):
                PRETHEFT_DROPPED["shopify"] = PRETHEFT_DROPPED.get("shopify", 0) + 1
                continue  # in stock before the theft — cannot be the bike
            variants = p.get("variants") or []
            price = parse_price(str(variants[0].get("price", ""))) if variants else None
            images = p.get("images") or []
            desc = unescape(re.sub(r"<[^>]+>", " ", p.get("body_html") or ""))
            results.append({
                "id": f"shopify:{shop['name']}:{p['id']}",
                "source": shop["name"],
                "query": "(new stock)",
                "url": f"{base}/products/{p.get('handle', '')}",
                "title": p.get("title", ""),
                "description": desc[:500],
                "location": shop.get("location", "UK"),
                "price_text": f"£{price:.0f}" if price else "?",
                "price": price,
                "image": images[0].get("src", "") if images else "",
            })
    return results


# ---------------------------------------------------------------- eBay (official Browse API)

def ebay_token(app_id, cert_id):
    creds = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    })
    resp = http_get(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=body,
    )
    return json.loads(resp)["access_token"]


def search_ebay(cfg):
    results = []
    try:
        token = ebay_token(cfg["app_id"], cfg["cert_id"])
    except Exception as e:
        log(f"ebay auth failed (check app_id/cert_id in config.json): {e}")
        return results
    scoped = [(q, True, False) for q in cfg["queries"]] + \
             [(q, False, False) for q in cfg.get("global_queries", [])] + \
             [(q, True, True) for q in cfg.get("broad_queries", [])]
    for query, restrict_country, price_band in scoped:
        params = {"q": query, "limit": "50", "sort": "newlyListed"}
        filters = []
        if restrict_country:
            filters.append(f"itemLocationCountry:{cfg.get('located_in', 'GB')}")
        if price_band:
            # broad queries ("road bike") sweep everything new in the band
            filters.append(f"price:[{cfg.get('broad_price_min', 200)}.."
                           f"{cfg.get('broad_price_max', 3000)}],priceCurrency:GBP")
        if filters:
            params["filter"] = ",".join(filters)
        params = urllib.parse.urlencode(params)
        try:
            data = json.loads(http_get(
                f"https://api.ebay.com/buy/browse/v1/item_summary/search?{params}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
                },
            ))
        except Exception as e:
            log(f"ebay search failed for {query!r}: {e}")
            continue
        for item in data.get("itemSummaries", []):
            if cfg.get("stolen_iso") and before_theft(
                    item.get("itemCreationDate", ""), cfg["stolen_iso"]):
                PRETHEFT_DROPPED["ebay"] = PRETHEFT_DROPPED.get("ebay", 0) + 1
                continue  # listed before the theft — cannot be the bike
            price = item.get("price", {})
            loc = item.get("itemLocation", {})
            results.append({
                "id": "ebay:" + item["itemId"],
                "source": "eBay",
                "query": query,
                "url": item.get("itemWebUrl", ""),
                "title": item.get("title", ""),
                "description": item.get("shortDescription", ""),
                "location": ", ".join(x for x in (loc.get("city"), loc.get("postalCode")) if x),
                "price_text": f"£{price.get('value', '?')}",
                "price": parse_price(price.get("value", "")),
                "image": (item.get("image") or {}).get("imageUrl", ""),
            })
    return results


# ---------------------------------------------------------------- image colour triage

def blue_score(image_url):
    """Fraction of thumbnail pixels in the muted-blue ('blue steel') hue range,
    or None if Pillow is missing / the fetch fails. Assists eyeballing only —
    never used to drop listings."""
    try:
        from PIL import Image
    except ImportError:
        return None
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".img") as tmp:
            proc = subprocess.run(
                ["curl", "-sS", "--max-time", "15", "-A", UA, "-o", tmp.name,
                 image_url], capture_output=True, timeout=25)
            if proc.returncode != 0:
                return None
            img = Image.open(tmp.name).convert("RGB")
            img.thumbnail((64, 64))
            hsv = img.convert("HSV")
            pixels = list(hsv.getdata())
    except Exception:
        return None
    if not pixels:
        return None
    # PIL hue is 0-255 for 0-360°; steel/denim blues sit ~190-250° → ~135-177
    hits = sum(1 for h, s, v in pixels
               if 130 <= h <= 180 and 40 <= s <= 210 and 50 <= v <= 230)
    return round(hits / len(pixels), 3)


BLUE_CHIP_THRESHOLD = 0.08
MAX_IMAGE_SCORES_PER_SWEEP = 80


# ---------------------------------------------------------------- classify / notify / report

def within_one_edit(a, b):
    """True if strings are within Levenshtein distance 1."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(a) == len(b):
            i += 1
        j += 1
    return True


def classify(item, alert_terms, price_min, price_max, fuzzy_terms=()):
    """Return 'alert', 'digest', or None (drop)."""
    haystack = f"{item['title']} {item['description']}".lower()
    if any(term.lower() in haystack for term in alert_terms):
        return "alert"
    # catch one-letter misspellings of distinctive terms ("unibox", "univax")
    words = set(re.findall(r"[a-z]{5,}", haystack))
    for term in fuzzy_terms:
        if any(within_one_edit(term.lower(), w) for w in words):
            return "alert"
    price = item["price"]
    if price is not None and not (price_min <= price <= price_max):
        return None  # broad match priced far outside plausible resale range
    return "digest"


# ------------------------------------------------------------------- AI triage
# Would-be alerts get a second opinion from Claude before they push: "could
# this plausibly be the stolen bike (or its parts)?" Clear mismatches are
# recorded as FPs — kept in the report, no notification. Degrades gracefully:
# no BIKEWATCH_ANTHROPIC_KEY / API error / cap hit -> None -> notify anyway
# (fail open — a missed FP costs a buzz, a missed match costs the bike).
# Raw HTTP through http_get(): the repo is stdlib-only, no pip installs.

AI_MODEL = "claude-opus-5"
AI_MAX_CALLS_PER_SWEEP = 25
AI_CALLS = {"n": 0, "cap": AI_MAX_CALLS_PER_SWEEP}
AI_SCHEMA = {
    "type": "object",
    "properties": {
        "plausible": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["plausible", "reason"],
    "additionalProperties": False,
}


def ai_verdict(item, bike):
    key = os.environ.get("BIKEWATCH_ANTHROPIC_KEY")
    if not key or AI_CALLS["n"] >= AI_CALLS["cap"]:
        return None
    AI_CALLS["n"] += 1
    prompt = (
        "A bicycle was stolen and a marketplace monitor flagged the listing "
        "below as a possible match. Judge whether it could plausibly be the "
        "stolen bike — or contain identifiable parts from it (wheels, "
        "groupset, frame sold separately or rebuilt onto another frame; "
        "stolen bikes are routinely stripped).\n\n"
        f"STOLEN BIKE: {bike['description']}\n"
        f"Stolen: {bike.get('stolen_when', '')} at {bike.get('stolen_where', '')}\n\n"
        "FLAGGED LISTING:\n"
        f"Title: {item['title']}\n"
        f"Description: {item['description'][:600]}\n"
        f"Price: {item['price_text']}  Location: {item['location']}  "
        f"Source: {item['source']}  Matched via: {item['query']}"
        f"{' (hot: cheap quality road bike near theft area)' if item.get('hot') else ''}\n\n"
        "Rules:\n"
        "- A complete bike listed under a specific DIFFERENT make and model "
        "(e.g. 'Muddyfox Race 400', 'Scott CR1') is that bike — thieves "
        "strip decals or list generically, they do not apply another "
        "brand's model-accurate branding. Mark it implausible unless the "
        "listing mentions the stolen bike's distinctive components or is "
        "described as a recent custom/fresh build.\n"
        "- Sharing ubiquitous components alone (Shimano 105/Tiagra, a "
        "generic carbon frame) is NOT a link; the distinctive identifiable "
        "part here is the DT Swiss E1800 wheelset.\n"
        "- Unbranded, brand-stripped, or vaguely described listings that "
        "fit the stolen bike's type/colour/price stay plausible, "
        "especially near the theft area. Wheelset/parts listings matching "
        "the stolen bike's components stay plausible.\n"
        "- If genuinely uncertain, set plausible=true.\n"
        "One short sentence of reason."
    )
    body = json.dumps({
        "model": AI_MODEL,
        "max_tokens": 2000,
        "output_config": {"effort": "low",
                          "format": {"type": "json_schema", "schema": AI_SCHEMA}},
        "messages": [{"role": "user", "content": prompt}],
    })
    try:
        resp = json.loads(http_get(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            data=body))
        if resp.get("stop_reason") == "refusal":
            return None
        text = next(b["text"] for b in resp["content"] if b["type"] == "text")
        v = json.loads(text)
        return {"plausible": bool(v["plausible"]),
                "reason": str(v.get("reason", ""))[:300]}
    except Exception as e:
        log(f"ai triage failed for {item['id']}: {e}")
        return None


def notify_macos(title, message, url):
    if sys.platform != "darwin":
        return  # cloud runner — ntfy/telegram carry the alert (a Linux ruby
        # gem also shadows the name terminal-notifier, so don't trust which())
    tn = shutil.which("terminal-notifier")
    if tn:
        cmd = [tn, "-title", title, "-message", message, "-sound", "Glass"]
        if url:
            cmd += ["-open", url]  # clicking the notification opens the listing
        subprocess.run(cmd, check=False)
        return
    def q(s):
        return json.dumps(s, ensure_ascii=False)
    script = (
        f'display notification {q(message)} '
        f'with title {q(title)} sound name "Glass"'
    )
    subprocess.run(["osascript", "-e", script], check=False)


def notify_ntfy(topic, title, message, url="", priority="high"):
    """Push to the ntfy app (ntfy.sh). Tapping the notification opens `url`
    where supported (Android); the URL also goes in the body since iOS only
    shows links as tappable text inside the app."""
    headers = {"Title": title, "Priority": priority, "Tags": "rotating_light"
               if priority == "high" else "mag"}
    if url:
        headers["Click"] = url
        message = f"{message}\n{url}"
    try:
        http_get(f"https://ntfy.sh/{topic}", headers=headers, data=message)
    except Exception as e:
        log(f"ntfy notify failed: {e}")


def notify_telegram(token, chat_id, text):
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "disable_web_page_preview": "false",
    })
    try:
        http_get(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    except Exception as e:
        log(f"telegram notify failed: {e}")


def notify_github_issue(alerts, report_url):
    """Redundant channel for the one notification that matters: open a
    GitHub issue on alert (→ GitHub mobile push + email), so a dropped ntfy
    push can't silently cost a match. Cloud-only — needs GITHUB_REPOSITORY
    and BIKEWATCH_GH_TOKEN, both provided by the workflow."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("BIKEWATCH_GH_TOKEN")
    if not (repo and token and alerts):
        return
    owner = repo.split("/")[0]
    lines = [f"- [{a['title']} — {a['price_text']} ({a['location']}, "
             f"{a['source']})]({a['url']})"
             + (f" · [archived copy]({a['wayback']})" if a.get("wayback") else "")
             for a in alerts]
    body = (f"@{owner} possible match(es) — do not contact the seller, "
            "screenshot first, then 101/999:\n\n" + "\n".join(lines)
            + (f"\n\n[Full report]({report_url})" if report_url else ""))
    payload = json.dumps({
        "title": f"🚨 {len(alerts)} possible match(es): "
                 f"{alerts[0]['title'][:60]}",
        "body": body})
    try:
        http_get(f"https://api.github.com/repos/{repo}/issues",
                 headers={"Authorization": f"Bearer {token}",
                          "Accept": "application/vnd.github+json",
                          "Content-Type": "application/json"},
                 data=payload)
    except Exception as e:
        log(f"github issue notify failed: {e}")


def preserve_evidence(item):
    """Suspect listings vanish fast — sellers delete them within hours. At
    detection time: snapshot the listing into the Wayback Machine (off-site,
    timestamped, citable to police) and pull its photo into the repo, so a
    deleted ad stays reviewable. Best-effort; never blocks the alert path."""
    evdir = REPORT_PATH.parent / "evidence"
    evdir.mkdir(parents=True, exist_ok=True)
    sid = re.sub(r"[^\w.-]", "_", item["id"])
    if item.get("image"):
        subprocess.run(["curl", "-sS", "--max-time", "20", "-A", UA, "-o",
                        str(evdir / f"{sid}.jpg"), item["image"]],
                       capture_output=True, timeout=30)
        item["evidence_photo"] = f"evidence/{sid}.jpg"
    try:
        http_get("https://web.archive.org/save/" + item["url"])
        item["wayback"] = "https://web.archive.org/web/2/" + item["url"]
    except Exception as e:
        log(f"wayback save failed for {item['id']}: {e}")


NEAR_THEFT_RE = re.compile(
    r"shoreditch|hackney|hoxton|bethnal|dalston|haggerston|clapton|homerton|"
    r"london fields|whitechapel|stepney|islington|tower hamlets|\bE[1289]\b",
    re.I)
MAX_DIGEST_ROWS = 400


def write_report(findings, bike_desc, crime_ref=""):
    import html as H
    # AI-dismissed alerts stay in state (dedupe + evidence) but are closed:
    # the report only shows what's worth the user's time
    items = sorted((f for f in findings.values() if not f.get("fp")),
                   key=lambda x: x["first_seen"], reverse=True)
    n_dismissed = sum(1 for f in findings.values() if f.get("fp"))
    alerts = [f for f in items if f["level"] == "alert"]
    digest = [f for f in items if f["level"] != "alert"][:MAX_DIGEST_ROWS]
    rows = []
    for f in alerts + digest:
        near = bool(NEAR_THEFT_RE.search(f.get("location", "")))
        blue = (f.get("blue_score") or 0) >= BLUE_CHIP_THRESHOLD
        badge = ("<span class='b alert'>ALERT</span>" if f["level"] == "alert"
                 else "<span class='b digest'>digest</span>")
        if f.get("hot"):
            badge += " <span class='b hot'>HOT</span>"
        if near:
            badge += " <span class='b near'>NEAR THEFT</span>"
        if blue:
            badge += " <span class='b blue'>BLUE</span>"
        img = (f"<img src='{H.escape(f['image'], quote=True)}' loading='lazy'>"
               if f.get("image") else "<div class='noimg'>no photo</div>")
        rows.append(f"""
        <tr class="{f['level']}{' near' if near else ''}{' blue' if blue else ''}" data-id="{H.escape(f['id'], quote=True)}">
          <td class="rv"><input type="checkbox" title="mark reviewed"></td>
          <td>{img}</td>
          <td>
            {badge} <b>{H.escape(f['title'])}</b><br>
            <span class="desc">{H.escape(f['description'][:220])}</span><br>
            <small>{H.escape(f['source'])} · {H.escape(f['location'])}
            · query: <i>{H.escape(f['query'])}</i> · first seen {f['first_seen'][:16]}</small>
          </td>
          <td class="price"><b>{H.escape(f['price_text'])}</b></td>
          <td><a href="{H.escape(f['url'], quote=True)}" target="_blank">open ↗</a>{f"<br><a href='{H.escape(f['wayback'], quote=True)}' target='_blank'><small>archived ↗</small></a>" if f.get('wayback') else ''}</td>
        </tr>""")
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(f"""<!doctype html><meta charset="utf-8">
<title>BikeWatch findings</title>
<style>
 body{{font-family:-apple-system,sans-serif;max-width:1150px;margin:24px auto;padding:0 12px}}
 table{{border-collapse:collapse;width:100%}}
 tr{{border-bottom:1px solid #ddd}} td{{padding:10px;vertical-align:top}}
 td img{{width:150px;border-radius:6px}} .noimg{{width:150px;color:#999;font-size:12px}}
 .b{{color:#fff;padding:2px 8px;border-radius:10px;font-size:12px}}
 .b.alert{{background:#c0392b}} .b.digest{{background:#7f8c8d}} .b.near{{background:#e67e22}}
 .b.hot{{background:#8e44ad}} .b.blue{{background:#2980b9}}
 .only-blue tr:not(.blue){{display:none}}
 tr.alert{{background:#fff5f5}} tr.near td:first-child{{border-left:4px solid #e67e22}}
 .desc{{color:#555}} .price{{white-space:nowrap}}
 tr.reviewed{{opacity:.25}} .hidden-reviewed tr.reviewed{{display:none}}
 header{{display:flex;gap:16px;align-items:center;flex-wrap:wrap}}
 .ref{{background:#eef;border-radius:8px;padding:10px 14px}}
</style>
<body>
<header>
 <h1 style="margin:0">BikeWatch</h1>
 <div class="ref"><b>Looking for:</b> {H.escape(bike_desc)}<br>
 <a href="https://uk.swiftbicycles.com/collections/road/products/univox-comp"
    target="_blank">reference photos ↗</a> —
 compare wheels (DT Swiss E1800), 105 hydraulic levers, frame shape — not just colour
 {f'<br><b>Crime ref:</b> {H.escape(crime_ref)} (Met Police) — quote this if the bike is found; do not approach the seller, call 101/999' if crime_ref else ''}</div>
 <label><input type="checkbox" id="hr" checked> hide reviewed</label>
 <label><input type="checkbox" id="ob"> only blue-ish photos</label>
</header>
<p>{len(alerts)} alerts · {len(digest)} digest shown (cap {MAX_DIGEST_ROWS})
{f' · {n_dismissed} auto-closed by AI triage' if n_dismissed else ''}
 · last sweep {datetime.now().isoformat(timespec='minutes')}
 · tick a row once you've ruled it out</p>
<table id="t">{''.join(rows)}</table>
<script>
 const K='bikewatch-reviewed', seen=new Set(JSON.parse(localStorage.getItem(K)||'[]'));
 const tbl=document.getElementById('t');
 for(const tr of tbl.rows){{
   const id=tr.dataset.id, cb=tr.querySelector('.rv input');
   if(seen.has(id)){{tr.classList.add('reviewed');cb.checked=true;}}
   cb.addEventListener('change',()=>{{
     cb.checked?seen.add(id):seen.delete(id);
     tr.classList.toggle('reviewed',cb.checked);
     localStorage.setItem(K,JSON.stringify([...seen]));}});
 }}
 const hr=document.getElementById('hr'), ob=document.getElementById('ob');
 const sync=()=>{{document.body.classList.toggle('hidden-reviewed',hr.checked);
   document.body.classList.toggle('only-blue',ob.checked);}};
 hr.addEventListener('change',sync); ob.addEventListener('change',sync); sync();
</script>
</body>""", encoding="utf-8")


# ---------------------------------------------------------------- main

def acquire_lock():
    import fcntl
    fh = open(BASE / ".lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another sweep is still running; exiting")
        sys.exit(0)
    return fh


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            STATE_PATH.replace(STATE_PATH.with_suffix(".json.corrupt"))
            log("state.json corrupt — starting fresh, notifications suppressed this run")
    return {"findings": {}}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, STATE_PATH)


HOT_TERMS_RE = re.compile(r"carbon|105|hydraulic|ultegra|di2", re.I)
# catch-all sweeps see every bike type; only listings that could plausibly
# be (or contain parts of) a road bike are worth a digest row (EN/DE/NL terms)
ROADISH_RE = re.compile(
    r"road|racing|race ?-?\s?(bike|fiets)|rennrad|racefiets|carbon|gravel|"
    r"cyclo|aero|fixie|single ?speed|105|tiagra|ultegra|dura.?ace|di2|e ?1800",
    re.I)


def title_tokens(title):
    return frozenset(t for t in re.findall(r"[a-z0-9]+", title.lower()) if len(t) > 2)


def is_relisting(toks, source, seen_tokens):
    """Same ad back under a new id with a tweaked title and/or price — the
    exact (title, price) fingerprint misses those, so also suppress when the
    title's token set heavily overlaps a recent finding from the same source
    ("Muddyfox Race 400 Road Bike £448" -> "Road Bike Muddyfox Carbon Race
    400 £395")."""
    if len(toks) < 4:
        return False
    for prev_source, prev_toks in seen_tokens:
        if prev_source != source or not prev_toks:
            continue
        if len(toks & prev_toks) / len(toks | prev_toks) >= 0.8:
            return True
    return False


def is_hot(item):
    """Quality road bike, suspiciously cheap, near the theft location."""
    return (item["source"] == "Gumtree"
            and item["price"] is not None and 300 <= item["price"] <= 1500
            and NEAR_THEFT_RE.search(item.get("location", "")) is not None
            and HOT_TERMS_RE.search(item["title"] + " " + item["description"])
            is not None)


FAIL_STREAK_WARN = 6   # ≈2 h of a source contributing zero results
REWARN_EVERY = 72      # ≈daily reminder while a source stays dark


def update_health(state, cfg, results_by_source):
    """Track per-source result counts; a warning when any enabled source
    contributes zero results for ~2 h — whatever the cause (empty, crashed,
    stuck in bot-challenge backoff, or misconfigured since day one): each of
    those is a coverage gap even when every individual skip looks
    'expected'. Re-warns daily while dark. Plus a quiet daily heartbeat so
    silence is never ambiguous."""
    health = state.setdefault("health", {})
    topic = cfg["notify"].get("ntfy_topic")
    gt_backoff = GT_BACKOFF_PATH.exists() and __import__("time").time() < \
        json.loads(GT_BACKOFF_PATH.read_text())["until"]
    for key, res in results_by_source.items():
        hs = health.setdefault(key, {"fail_streak": 0, "warned": False,
                                     "ever_ok": False})
        # a run cut short by a bot challenge returns partial results — that
        # must not look healthy, or a persistent challenge loop that always
        # lets the first fetch through would never trip the streak
        challenged = key == "gumtree" and gt_backoff
        if (res or PRETHEFT_DROPPED.get(key)) and not challenged:
            # producing results — or fetching fine with everything it found
            # legitimately dropped as pre-theft — is a healthy source
            hs.update(fail_streak=0, warned=False, ever_ok=True)
            continue
        hs["fail_streak"] += 1
        remind = hs["fail_streak"] % REWARN_EVERY == 0
        if hs["fail_streak"] >= FAIL_STREAK_WARN and (not hs["warned"] or remind):
            hs["warned"] = True
            cause = ("stuck in a bot-challenge/backoff loop"
                     if key == "gumtree" and gt_backoff
                     else "its sweeps are crashing" if res is None
                     else "it has NEVER returned results — check its queries "
                          "and parser" if not hs["ever_ok"]
                     else "it may be blocked or its page layout changed")
            msg = (f"{key}: {hs['fail_streak']} sweeps without full coverage "
                   f"— {cause}. Coverage gap: check bikewatch.log / "
                   "the Actions run logs.")
            log("HEALTH WARNING: " + msg)
            notify_macos("⚠️ BikeWatch health", msg, "")
            if topic:
                notify_ntfy(topic, "⚠️ BikeWatch health", msg)
    now = datetime.now()
    cutoff = (now - __import__("datetime").timedelta(hours=24)).isoformat()
    sweeps = [t for t in health.get("sweeps", []) if t > cutoff]
    sweeps.append(now.isoformat(timespec="seconds"))
    health["sweeps"] = sweeps
    today = now.strftime("%Y-%m-%d")
    if health.get("last_heartbeat") != today and now.hour >= 9:
        health["last_heartbeat"] = today
        per_src = ", ".join(
            f"{k}: {'ERR' if v is None else len(v)}"
            for k, v in results_by_source.items())
        alerts = sum(1 for f in state["findings"].values()
                     if f["level"] == "alert")
        msg = (f"Alive — {len(sweeps)} sweeps in 24h. Last sweep: {per_src}. "
               f"{alerts} alerts, {len(state['findings'])} listings tracked.")
        log("heartbeat: " + msg)
        if topic:
            notify_ntfy(topic, "BikeWatch daily check-in", msg, priority="low")


def send_status(state, notify_cfg, alert_terms, results_by_source,
                n_alerts, n_digest):
    """Hourly 'still alive' ping at min priority — no buzz, but the ntfy app
    shows what was checked and why nothing louder arrived, so a healthy
    quiet hour is distinguishable from a dead monitor."""
    topic = notify_cfg.get("ntfy_topic")
    if not topic or not notify_cfg.get("hourly_status", True):
        return
    health = state.setdefault("health", {})
    hour = datetime.now().strftime("%Y-%m-%dT%H")
    if health.get("last_status") == hour:
        return
    health["last_status"] = hour
    import time
    gt_backoff = GT_BACKOFF_PATH.exists() and time.time() < \
        json.loads(GT_BACKOFF_PATH.read_text())["until"]
    def fmt(k, v):
        if k == "gumtree" and gt_backoff:
            return "gumtree backoff"
        label = f"{k} {'ERR' if v is None else len(v)}"
        if PRETHEFT_DROPPED.get(k):
            label += f" ({PRETHEFT_DROPPED[k]} pre-theft dropped)"
        return label
    per_src = ", ".join(fmt(k, v) for k, v in results_by_source.items())
    msg = (f"Checked {per_src} listings. New this sweep: {n_alerts} "
           f"alert-term hits ({'/'.join(alert_terms)}), {n_digest} broad "
           f"matches (report only, no push). {len(state['findings'])} tracked "
           "total. No alert-term match = no loud notification.")
    log("status ping: " + msg)
    notify_ntfy(topic, "BikeWatch hourly status", msg, priority="min")


def main():
    lock = acquire_lock()  # noqa: F841 — held for process lifetime
    cfg = json.loads(CONFIG_PATH.read_text())
    # the repo is public, so the ntfy topic (= the notification password)
    # lives outside config.json: Actions secret in CI, env var locally
    env_topic = os.environ.get("BIKEWATCH_NTFY_TOPIC")
    if env_topic:
        cfg["notify"]["ntfy_topic"] = env_topic
    # eBay credentials likewise come from secrets, never config.json;
    # their presence is what enables the source
    ebay_cfg = cfg.setdefault("ebay", {})
    for key, env in (("app_id", "BIKEWATCH_EBAY_APP_ID"),
                     ("cert_id", "BIKEWATCH_EBAY_CERT_ID")):
        if os.environ.get(env):
            ebay_cfg[key] = os.environ[env]
    if ebay_cfg.get("app_id") and ebay_cfg.get("cert_id"):
        ebay_cfg["enabled"] = True
    # dated sources drop listings that predate the theft
    for key in ("ebay", "shopify"):
        cfg.setdefault(key, {})["stolen_iso"] = cfg["bike"].get("stolen_iso", "")
    state = load_state()
    findings = state["findings"]
    baseline_run = not findings

    if "--test-notify" in sys.argv:
        notify_macos("BikeWatch test", "Notifications are working.", "")
        if cfg["notify"].get("ntfy_topic"):
            notify_ntfy(cfg["notify"]["ntfy_topic"], "BikeWatch test",
                        "Phone notifications are working. Tap to see the bike.",
                        "https://uk.swiftbicycles.com/collections/road/products/univox-comp")
        print("test notification sent")
        return

    if "--triage-backlog" in sys.argv:
        # bulk AI review of every finding (alert or digest) without a
        # verdict yet: clear mismatches are closed (fp), the report is
        # regenerated. Idempotent and resumable — items that fail (rate
        # limit, API error) stay unreviewed and are picked up on a re-run.
        # No notifications — nothing here is new to the user.
        from concurrent.futures import ThreadPoolExecutor
        pending = [f for f in findings.values()
                   if not f.get("fp") and "ai_reason" not in f
                   and "univox" not in (f["title"] + f.get("description", "")).lower()]
        AI_CALLS["cap"] = len(pending)
        log(f"backlog triage: {len(pending)} unreviewed finding(s)")

        def review(f):
            verdict = ai_verdict(f, cfg["bike"])
            if verdict is not None:
                f["ai_reason"] = verdict["reason"]
                if not verdict["plausible"]:
                    f["fp"] = True
            return f, verdict

        closed = kept = failed = 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            for f, verdict in ex.map(review, pending):
                if verdict is None:
                    failed += 1
                elif verdict["plausible"]:
                    kept += 1
                    log(f"  KEPT {f['level']} {f['id']}: {verdict['reason']}")
                else:
                    closed += 1
        log(f"backlog triage done: {closed} closed, {kept} kept, "
            f"{failed} no-verdict (re-run to retry)")
        write_report(findings, cfg["bike"]["description"],
                     cfg["bike"].get("crime_reference", ""))
        save_state(state)
        return

    sources = [
        ("gumtree", search_gumtree),
        ("ebay", search_ebay),
        ("kleinanzeigen", search_kleinanzeigen),
        ("marktplaats", search_marktplaats),
        ("shopify", search_shopify),
    ]
    results_by_source = {}
    for key, fn in sources:
        if cfg.get(key, {}).get("enabled"):
            try:
                results_by_source[key] = fn(cfg[key])
            except Exception as e:
                log(f"{key} sweep crashed: {e}")
                results_by_source[key] = None  # None = hard failure
    results = [item for res in results_by_source.values() if res
               for item in res]

    key_by_source = {"Gumtree": "gumtree", "eBay": "ebay",
                     "Kleinanzeigen (DE)": "kleinanzeigen",
                     "Marktplaats (NL)": "marktplaats"}
    for shop in cfg.get("shopify", {}).get("shops", []):
        key_by_source[shop["name"]] = "shopify"
    fingerprints = {(f["title"].lower().strip(), f["price_text"])
                    for f in findings.values()}
    relist_cutoff = (datetime.now(timezone.utc)
                     - timedelta(days=14)).isoformat(timespec="seconds")
    seen_tokens = [(f["source"], title_tokens(f["title"]))
                   for f in findings.values() if f["first_seen"] >= relist_cutoff]
    new_alerts, fp_alerts, new_digest, fp_digest = [], [], [], []
    for item in results:
        if item["id"] in findings:
            continue
        fp = (item["title"].lower().strip(), item["price_text"])
        if fp in fingerprints:
            continue  # same ad relisted under a new ID
        fingerprints.add(fp)
        src_cfg = cfg.get(key_by_source.get(item["source"], ""), {})
        level = classify(item, src_cfg.get("alert_terms", cfg["alert_terms"]),
                         src_cfg.get("broad_price_min", 0),
                         src_cfg.get("broad_price_max", 10**9),
                         cfg.get("fuzzy_alert_terms", ()))
        if level is None:
            continue
        if level == "digest" and src_cfg.get("alert_only"):
            continue  # consignment shops: whole-inventory digest is noise
        if (level == "digest" and item["query"].startswith("(catch-all")
                and not ROADISH_RE.search(item["title"] + " " + item["description"])):
            continue  # catch-alls see kids/city/e-bikes too — not the bike
        toks = title_tokens(item["title"])
        if level == "digest" and is_relisting(toks, item["source"], seen_tokens):
            continue  # fuzzy relist — alert-term hits are never suppressed
        seen_tokens.append((item["source"], toks))
        if level == "digest" and is_hot(item):
            level = "alert"
            item["hot"] = True
        if "univox" not in (item["title"] + item["description"]).lower():
            # AI verdict on every new finding (alerts: gates the push;
            # digest: gates report entry); univox hits are exempt
            verdict = ai_verdict(item, cfg["bike"])
            if verdict:
                item["ai_reason"] = verdict["reason"]
                if not verdict["plausible"]:
                    item["fp"] = True
        item["level"] = level
        item["first_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        findings[item["id"]] = item
        if level == "alert":
            (fp_alerts if item.get("fp") else new_alerts).append(item)
        elif item.get("fp"):
            fp_digest.append(item)
        else:
            new_digest.append(item)

    scored = 0
    for item in new_alerts + fp_alerts + new_digest:
        if item.get("image") and scored < MAX_IMAGE_SCORES_PER_SWEEP:
            item["blue_score"] = blue_score(item["image"])
            scored += 1

    notify = {} if baseline_run else cfg["notify"]
    if baseline_run and (new_alerts or new_digest):
        log(f"baseline run: {len(new_alerts) + len(new_digest)} listings "
            "recorded quietly, notifications suppressed")
    MAX_INDIVIDUAL = 3
    for item in new_alerts[:MAX_INDIVIDUAL]:
        msg = f"{item['title']} — {item['price_text']} ({item['location']})"
        if notify.get("macos"):
            notify_macos("🚨 BikeWatch: possible match!", msg, item["url"])
        if notify.get("ntfy_topic"):
            notify_ntfy(notify["ntfy_topic"], "🚨 BikeWatch: possible match!",
                        f"{msg}\non {item['source']}", item["url"])
        if notify.get("telegram_bot_token") and notify.get("telegram_chat_id"):
            notify_telegram(notify["telegram_bot_token"], notify["telegram_chat_id"],
                            f"🚨 Possible match on {item['source']}:\n{msg}\n{item['url']}")
    for item in new_alerts + fp_alerts:
        preserve_evidence(item)  # FPs too — cheap, silent, and the AI can be wrong
    report_url = cfg["notify"].get("report_url", "")
    if new_alerts and notify:
        notify_github_issue(new_alerts, report_url)
    overflow = len(new_alerts) - MAX_INDIVIDUAL
    if overflow > 0:
        if notify.get("macos"):
            notify_macos("🚨 BikeWatch",
                         f"...and {overflow} more possible matches — open the report.",
                         REPORT_PATH.as_uri())
        if notify.get("ntfy_topic"):
            notify_ntfy(notify["ntfy_topic"], "🚨 BikeWatch",
                        f"...and {overflow} more possible matches — open the report.",
                        report_url)
    # digest items are report-only: with broad catch-alls a per-sweep "worth a
    # look" push fired on nearly every sweep — counts ride the hourly status

    update_health(state, cfg, results_by_source)
    send_status(state, notify, cfg["alert_terms"], results_by_source,
                len(new_alerts), len(new_digest))
    write_report(findings, cfg["bike"]["description"],
                 cfg["bike"].get("crime_reference", ""))
    save_state(state)
    log(f"sweep done: {len(results)} fetched, "
        f"{len(new_alerts)} alerts, {len(new_digest)} new digest items, "
        f"{len(fp_alerts) + len(fp_digest)} AI-closed, "
        f"{len(findings)} total tracked")


if __name__ == "__main__":
    main()
