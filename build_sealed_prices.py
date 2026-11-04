#!/usr/bin/env python3
"""
Builds price files for the Binder app:
  data/sealed-prices.json   (booster boxes, ETBs, ...)  needs CM_PRODUCT_LIST_URL
  data/singles-prices.json  (single cards)              needs CM_SINGLES_LIST_URL
Either one is optional; each is built only when its link is set.

It also builds data/cards-catalog.json (every English card: name, set, number, rarity)
from the public PokemonTCG data on GitHub. Binder uses it for the type-ahead card
preview with images. It also builds data/card-prices.json (Cardmarket EUR price per
exact card id) so adding and refreshing cards never depends on a live, slow API.
Problems with the catalog or card prices are only warnings, never a red run.

Downloads Cardmarket's Product List and Price Guide (JSON), keeps only
non-single products (booster boxes, ETBs, bundles, ...) that have a price,
and writes one small file Binder can read from the browser.

Configuration (environment variables, set as GitHub Actions "Variables"):
  CM_PRICE_GUIDE_URL   direct link to Cardmarket's Pokémon Price Guide file
  CM_PRODUCT_LIST_URL  direct link to Cardmarket's Pokémon Product List (non-singles) file
  CM_SINGLES_LIST_URL  direct link to Cardmarket's Pokémon Product List (singles) file

Standard library only. Fails loudly (non-zero exit) if nothing usable is found,
so a broken run shows up as a red cross in the Actions tab instead of silently
publishing an empty file.
"""
import datetime
import gzip
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import zipfile

SEALED_PATH = os.path.join("data", "sealed-prices.json")
SINGLES_PATH = os.path.join("data", "singles-prices.json")
CATALOG_PATH = os.path.join("data", "cards-catalog.json")
PRICES_PATH = os.path.join("data", "card-prices.json")
PRICE_API = os.environ.get("PRICE_API_URL", "https://api.pokemontcg.io/v2/cards")
PRICE_PAGE = int(os.environ.get("PRICE_PAGE_SIZE", "100"))
PRICE_TIME_BUDGET = int(os.environ.get("PRICE_TIME_BUDGET_MIN", "40")) * 60
CATALOG_BASE = os.environ.get("CATALOG_BASE_URL", "https://raw.githubusercontent.com/PokemonTCG/pokemon-tcg-data/master").rstrip("/")


class JobError(Exception):
    pass


def fail(msg):
    raise JobError(msg)


def report(msg):
    print("ERROR: " + msg, file=sys.stderr)
    # "::error::" makes GitHub show the reason in the red box on the run page.
    safe = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print("::error title=Price update failed::" + safe)


def fetch_json(url, label):
    if not url:
        fail(f"{label} is not set. Add it under Settings > Secrets and variables > Actions > Variables.")
    host = urllib.parse.urlparse(url).netloc or "(no host - is the link complete?)"
    print(f"Downloading {label} from {host} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "binder-sealed-prices/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001
        fail(f"Could not download {label}: {exc}")
    # Be tolerant about packaging: plain JSON, gzip, or a zip containing one JSON file.
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif raw[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".json")]
            if not names:
                fail(f"{label}: zip file contains no .json file ({zf.namelist()[:5]})")
            raw = zf.read(names[0])
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        fail(f"{label} is not valid JSON: {exc}. First bytes: {raw[:80]!r}")


def extract_array(data, label):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("priceGuides", "products", "priceGuide", "productList", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        for value in data.values():
            if isinstance(value, list):
                return value
    fail(f"{label}: could not find a list of records. Top-level keys: {list(data)[:10] if isinstance(data, dict) else type(data)}")


def num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


EXTRA_KEYS = ("idExpansion", "expansionName", "expansion", "setName", "set", "number", "cardNumber", "rarity", "idMetacard")


def build_rows(products_raw, guide, skip_singles, label):
    out, skipped = [], 0
    for rec in products_raw:
        category = str(rec.get("categoryName", "")).lower()
        if skip_singles and "single" in category:
            skipped += 1
            continue
        name = rec.get("name") or rec.get("enName") or rec.get("productName")
        pid = str(rec.get("idProduct"))
        g = guide.get(pid)
        if not name or not g:
            continue
        trend = num(g.get("trend", g.get("trendPrice")))
        avg = num(g.get("avg", g.get("averageSellPrice")))
        low = num(g.get("low", g.get("lowPrice")))
        avg7, avg30 = num(g.get("avg7")), num(g.get("avg30"))
        if not any((trend, avg, low, avg30)):
            continue
        row = {
            "idProduct": int(pid) if pid.isdigit() else pid,
            "name": str(name),
            "trend": trend, "avg": avg, "low": low, "avg7": avg7, "avg30": avg30,
        }
        # Keep any set / number / rarity information Cardmarket provides, so Binder can tell printings apart.
        for key in EXTRA_KEYS:
            if rec.get(key) not in (None, ""):
                row[key] = rec[key]
        out.append(row)
    print(f"{label}: kept {len(out)} priced products" + (f" (skipped {skipped} singles)" if skipped else ""))
    return out


def write_feed(path, rows, source):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "currency": "EUR",
        "products": rows,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {path} ({os.path.getsize(path) / 1024:.0f} KB)")


def load_products(url, label):
    raw = extract_array(fetch_json(url, label), label)
    if raw:
        print(f"{label} sample record: {json.dumps(raw[0], ensure_ascii=False)[:400]}")
    if raw and "idProduct" not in raw[0]:
        fail(f"{label} records have no 'idProduct'. Example record keys: {list(raw[0])}")
    return raw


def http_json(url, label, attempts=3):
    last = None
    for n in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "binder-catalog/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (n + 1))
    raise JobError(f"Could not download {label}: {last}")


def build_catalog():
    """Compact list of every English card: sets + [id, name, number, setIndex, rarity]."""
    sets_raw = http_json(f"{CATALOG_BASE}/sets/en.json", "card sets")
    if not isinstance(sets_raw, list) or not sets_raw:
        raise JobError("card sets file is empty or has an unexpected layout")
    sets_raw.sort(key=lambda s: str(s.get("releaseDate", "")))
    sets, cards, failed = [], [], []
    for s in sets_raw:
        sid = s.get("id")
        if not sid:
            continue
        try:
            raw = http_json(f"{CATALOG_BASE}/cards/en/{sid}.json", f"cards of set {sid}", attempts=2)
        except JobError as exc:
            failed.append(sid)
            print(f"  skipped set {sid}: {exc}")
            continue
        idx = len(sets)
        sets.append([sid, s.get("name", sid), s.get("printedTotal") or s.get("total") or 0,
                     str(s.get("releaseDate", "")).replace("/", "-")])
        for c in raw:
            if c.get("id") and c.get("name") and c.get("number") is not None:
                cards.append([c["id"], c["name"], str(c["number"]), idx, c.get("rarity", "")])
    if not cards:
        raise JobError("no cards found in the catalog source")
    os.makedirs(os.path.dirname(CATALOG_PATH), exist_ok=True)
    payload = {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": "PokemonTCG/pokemon-tcg-data",
        "sets": sets,
        "cards": cards,
    }
    with open(CATALOG_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {CATALOG_PATH}: {len(cards)} cards in {len(sets)} sets ({os.path.getsize(CATALOG_PATH) / 1024:.0f} KB)"
          + (f"; {len(failed)} sets skipped" if failed else ""))


def price_page(page):
    url = f"{PRICE_API}?pageSize={PRICE_PAGE}&page={page}&orderBy=-set.releaseDate&select=id,cardmarket"
    headers = {"User-Agent": "binder-card-prices/1.0"}
    key = os.environ.get("POKEMONTCG_API_KEY", "").strip()
    if key:
        headers["X-Api-Key"] = key
    last = None
    for n in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"  page {page} attempt {n + 1} failed: {exc}")
            time.sleep(8 * (n + 1))          # their server returns 5xx errors now and then: wait and retry
    raise JobError(f"card price page {page} failed after 6 tries: {last}")


def build_card_prices():
    """Cardmarket EUR prices per exact card id: [trend, avg, avg30, avg7, low]. Newer sets first;
    entries from earlier runs are kept, so an interrupted run never loses data."""
    prices = {}
    try:
        with open(PRICES_PATH, encoding="utf-8") as fh:
            prices = dict(json.load(fh).get("prices", {}))
    except Exception:  # noqa: BLE001
        pass
    start, page, fetched, partial = time.time(), 1, 0, False
    while True:
        if time.time() - start > PRICE_TIME_BUDGET:
            partial = True
            break
        try:
            data = price_page(page)
        except JobError as exc:
            if not fetched and not prices:
                raise
            print(f"  stopped early: {exc}")
            partial = True
            break
        cards = data.get("data") or []
        if not cards:
            break
        for c in cards:
            p = ((c.get("cardmarket") or {}).get("prices")) or {}
            vals = [num(p.get(k)) for k in ("trendPrice", "averageSellPrice", "avg30", "avg7", "lowPrice")]
            if c.get("id") and any(vals):
                prices[c["id"]] = [round(v, 2) if v else None for v in vals]
        fetched += len(cards)
        total = data.get("totalCount") or 0
        if len(cards) < PRICE_PAGE or (total and fetched >= total):
            break
        page += 1
        time.sleep(0.5)
    if not prices:
        raise JobError("no card prices received")
    os.makedirs(os.path.dirname(PRICES_PATH), exist_ok=True)
    payload = {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": "pokemontcg.io (Cardmarket EUR)", "currency": "EUR", "partial": partial, "prices": prices,
    }
    with open(PRICES_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print(f"Wrote {PRICES_PATH}: {len(prices)} cards ({fetched} refreshed this run{', partial' if partial else ''}, "
          f"{os.path.getsize(PRICES_PATH) / 1024:.0f} KB)")


def main():
    sealed_url = os.environ.get("CM_PRODUCT_LIST_URL", "").strip()
    singles_url = os.environ.get("CM_SINGLES_LIST_URL", "").strip()
    if os.environ.get("SKIP_CATALOG", "").strip() != "1":
        try:
            build_catalog()
        except JobError as exc:
            # Only a warning: the price feeds below still run and the run stays green.
            print(f"::warning title=Card catalog not updated::{exc}")
            print(f"WARNING: card catalog not updated: {exc}")
        try:
            build_card_prices()
        except JobError as exc:
            print(f"::warning title=Card prices not updated::{exc}")
            print(f"WARNING: card prices not updated: {exc}")
    if not sealed_url and not singles_url:
        report("Nothing to do: set CM_PRODUCT_LIST_URL (sealed) and/or CM_SINGLES_LIST_URL (singles) "
               "under Settings > Secrets and variables > Actions > Variables.")
        sys.exit(1)

    guide_url = os.environ.get("CM_PRICE_GUIDE_URL", "").strip()
    try:
        guide_raw = extract_array(fetch_json(guide_url, "Price Guide"), "Price Guide")
        if guide_raw and "idProduct" not in guide_raw[0]:
            fail(f"Price Guide records have no 'idProduct'. Example record keys: {list(guide_raw[0])}")
    except JobError as exc:
        report(str(exc))
        sys.exit(1)
    guide = {str(rec.get("idProduct")): rec for rec in guide_raw}
    print(f"Price Guide: {len(guide_raw)} records")

    errors = []
    jobs = []
    if sealed_url:
        jobs.append(("Sealed", sealed_url, SEALED_PATH, True, "Cardmarket Product List + Price Guide",
                     "Check that CM_PRODUCT_LIST_URL is the NON-singles Product List and that the Price Guide covers sealed products."))
    if singles_url:
        jobs.append(("Singles", singles_url, SINGLES_PATH, False, "Cardmarket Singles Product List + Price Guide",
                     "Check that CM_SINGLES_LIST_URL is the SINGLES Product List."))
    for label, url, path, skip_singles, source, hint in jobs:
        try:
            rows = build_rows(load_products(url, f"{label} Product List"), guide, skip_singles, label)
            if not rows:
                fail(f"{label}: no priced products found. {hint}")
            write_feed(path, rows, source)
        except JobError as exc:
            errors.append(f"{label}: {exc}")

    for e in errors:
        report(e)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
