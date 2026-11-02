#!/usr/bin/env python3
"""
Builds data/sealed-prices.json for the Binder app.

Downloads Cardmarket's Product List and Price Guide (JSON), keeps only
non-single products (booster boxes, ETBs, bundles, ...) that have a price,
and writes one small file Binder can read from the browser.

Configuration (environment variables, set as GitHub Actions "Variables"):
  CM_PRICE_GUIDE_URL   direct link to Cardmarket's Pokémon Price Guide file
  CM_PRODUCT_LIST_URL  direct link to Cardmarket's Pokémon Product List (non-singles) file

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
import urllib.request
import zipfile

OUT_PATH = os.path.join("data", "sealed-prices.json")


def fail(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def fetch_json(url, label):
    if not url:
        fail(f"{label} is not set. Add it under Settings > Secrets and variables > Actions > Variables.")
    print(f"Downloading {label} ...")
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


def main():
    products_raw = extract_array(fetch_json(os.environ.get("CM_PRODUCT_LIST_URL"), "Product List"), "Product List")
    guide_raw = extract_array(fetch_json(os.environ.get("CM_PRICE_GUIDE_URL"), "Price Guide"), "Price Guide")
    print(f"Product List: {len(products_raw)} records, Price Guide: {len(guide_raw)} records")

    if products_raw and "idProduct" not in products_raw[0]:
        fail(f"Product List records have no 'idProduct'. Example record keys: {list(products_raw[0])}")
    if guide_raw and "idProduct" not in guide_raw[0]:
        fail(f"Price Guide records have no 'idProduct'. Example record keys: {list(guide_raw[0])}")

    guide = {}
    for rec in guide_raw:
        guide[str(rec.get("idProduct"))] = rec

    out = []
    skipped_singles = 0
    for rec in products_raw:
        category = str(rec.get("categoryName", "")).lower()
        if "single" in category:
            skipped_singles += 1
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
        out.append({
            "idProduct": int(pid) if pid.isdigit() else pid,
            "name": str(name),
            "trend": trend, "avg": avg, "low": low, "avg7": avg7, "avg30": avg30,
        })

    print(f"Kept {len(out)} priced non-single products (skipped {skipped_singles} singles)")
    if not out:
        fail("No priced non-single products found. Check that CM_PRODUCT_LIST_URL is the NON-singles "
             "Product List and that the Price Guide covers sealed products.")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    payload = {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": "Cardmarket Product List + Price Guide",
        "currency": "EUR",
        "products": out,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUT_PATH} ({os.path.getsize(OUT_PATH) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
