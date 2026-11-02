"""Koochooloo SEC insider layer v2. Reads every new Form 4 filing from SEC EDGAR (free, no key), keeps open-market
insider buys and sells for 90 days, and flags clusters of insider buying.

v2 (2 Nov 2026):
- Complete: pages back through EDGAR's live list until it reaches filings already processed, so a slow or skipped run
  never loses filings (v1 read only the latest 100 and missed busy afternoons).
- No duplicates: joint filings appear once per filer in EDGAR's list; each filing is now processed once, rows are unique
  by id, and amendments that repeat a transaction are dropped (v1 stored 213 duplicates out of 355 rows).
- Fast: one request per filing (the full submission text) and up to 8 requests a second in parallel, inside the SEC's
  10-a-second fair-access limit.
- Each row records the filing date as well as the trade date.
- Merges data/insider_backfill.json (written by backfill_insiders.py from EDGAR's daily index) so history is complete.

Writes data/insider_trades.json, data/insider_clusters.json, data/sec_seen.json and data/sec_status.json."""
import concurrent.futures, datetime, json, os, pathlib, re, threading, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET

UA = os.environ.get("SEC_CONTACT") or "KoochoolooResearch github.com/Montezuma360/koochooloo-data"
OUT = pathlib.Path("data")
OUT.mkdir(exist_ok=True)
NOW = datetime.datetime.utcnow()
STAMP = NOW.replace(microsecond=0).isoformat() + "Z"
RATE = 8.0            # requests per second, under the SEC's limit of 10
KEEP_DAYS = 90        # trade-date window kept in insider_trades.json
SEEN_DAYS = 5         # how long a processed filing number is remembered
_lock, _next = threading.Lock(), [0.0]


def get(url, tries=3, timeout=20):
    for i in range(tries):
        with _lock:  # global pace across threads
            t = time.monotonic()
            wait = _next[0] - t
            _next[0] = max(t, _next[0]) + 1 / RATE
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404 or i == tries - 1:
                raise
            time.sleep(5 * (i + 1))  # 403/429/5xx: the SEC asks callers to back off
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def txt(node, path):
    el = node.find(path)
    return el.text.strip() if el is not None and el.text else None


def parse_form4(xml, accession, filed=None):
    xml = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml)
    root = ET.fromstring(xml)
    issuer = txt(root, "issuer/issuerName")
    sym = (txt(root, "issuer/issuerTradingSymbol") or "").upper().replace("-", ".") or None
    rows = []
    owners = root.findall("reportingOwner")
    owner = txt(owners[0], "reportingOwnerId/rptOwnerName") if owners else None
    rel = owners[0].find("reportingOwnerRelationship") if owners else None
    role = None
    if rel is not None:
        role = txt(rel, "officerTitle") or ("Director" if txt(rel, "isDirector") in ("1", "true") else
               "10% owner" if txt(rel, "isTenPercentOwner") in ("1", "true") else None)
    for i, t in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction")):
        code = txt(t, "transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        try:
            shares = float(txt(t, "transactionAmounts/transactionShares/value") or 0)
            price = float(txt(t, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        rows.append({"id": f"{accession}-{i}", "symbol": sym, "issuer": issuer, "insider": owner, "role": role,
                     "side": "buy" if code == "P" else "sell", "date": txt(t, "transactionDate/value"), "filed": filed,
                     "shares": round(shares), "price": round(price, 4), "value": round(shares * price)})
    return rows


def fetch_filing(cik, acc, filed):
    """One request: the full submission text, which carries the Form 4 XML inline."""
    t = get("https://www.sec.gov/Archives/edgar/data/%s/%s/%s.txt" % (cik, acc.replace("-", ""), acc))
    m = re.search(r"<XML>\s*(.*?)\s*</XML>", t, re.S)
    return parse_form4(m.group(1), acc, filed) if m else []


def fetch_many(jobs, errors):
    """jobs: {accession: (cik, filed)} -> rows, fetched in parallel at the global pace."""
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_filing, cik, acc, filed): acc for acc, (cik, filed) in jobs.items()}
        for f in concurrent.futures.as_completed(futs):
            try:
                rows += f.result()
            except Exception as e:
                errors.append("%s: %s" % (futs[f], type(e).__name__))
    return rows


def unique(rows):
    """Unique by id, then drop repeats of the same trade (amendments and joint filings filed under new numbers)."""
    by_id, seen_trade, out = {}, set(), []
    for r in rows:
        by_id.setdefault(r["id"], r)
    for r in sorted(by_id.values(), key=lambda r: r["id"]):
        k = (r.get("symbol"), r.get("insider"), r.get("date"), r.get("side"), r.get("shares"), r.get("price"))
        if k in seen_trade:
            continue
        seen_trade.add(k)
        out.append(r)
    return out


def new_filings(seen, max_pages):
    """Page back through EDGAR's live Form 4 list until reaching filings already processed."""
    found, pages = {}, 0
    for page in range(max_pages):
        atom = get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb=&owner=include"
                   "&start=%d&count=100&output=atom" % (page * 100))
        pages += 1
        entries = re.findall(r"<entry>(.*?)</entry>", atom, re.S)
        if not entries:
            break
        overlap = False
        for e in entries:
            m = re.search(r"/Archives/edgar/data/(\d+)/\d+/([\d-]+)-index\.htm", e)
            if not m:
                continue
            acc = m.group(2)
            if acc in seen:
                overlap = True
                continue
            d = re.search(r"<updated>(\d{4}-\d{2}-\d{2})", e)
            found.setdefault(acc, (m.group(1), d.group(1) if d else None))
        if overlap:
            break
    return found, pages


def clusters_of(rows):
    """2+ different insiders buying the same stock in the last 30 days (by trade date)."""
    c30 = (NOW - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    agg = {}
    for r in rows:
        if r["side"] == "buy" and (r.get("date") or "") >= c30 and r["symbol"]:
            a = agg.setdefault(r["symbol"], {"symbol": r["symbol"], "issuer": r["issuer"], "insiders": set(), "value": 0, "last": "",
                                             "last_filed": ""})
            a["insiders"].add(r["insider"]); a["value"] += r["value"]; a["last"] = max(a["last"], r["date"])
            a["last_filed"] = max(a["last_filed"], r.get("filed") or "")
    return sorted(({**a, "insiders": sorted(i for i in a["insiders"] if i), "n": len(a["insiders"])} for a in agg.values()
                   if len(a["insiders"]) >= 2), key=lambda a: (-a["n"], -a["value"]))


def load(name, default):
    try:
        return json.loads((OUT / name).read_text())
    except Exception:
        return default


def main():
    t0 = time.time()
    status = {"updated": STAMP, "filings_seen": 0, "new_rows": 0, "errors": []}
    old = load("insider_trades.json", {}).get("rows", [])
    seen = {k: v for k, v in load("sec_seen.json", {}).get("seen", {}).items()
            if v >= (NOW - datetime.timedelta(days=SEEN_DAYS)).strftime("%Y-%m-%d")}
    first_run = not seen
    for r in old:  # filings already stored count as processed
        seen.setdefault(r["id"].rsplit("-", 1)[0], (r.get("filed") or r.get("date") or STAMP[:10]))
    new = []
    try:
        jobs, pages = new_filings(seen, 10 if first_run else 6)
        status["filings_seen"], status["pages_read"] = len(jobs), pages
        status["filing_errors"] = []  # single filings that failed (retried next run); kept apart from feed-level errors
        new = fetch_many(jobs, status["filing_errors"])
        for acc in [e.split(":")[0] for e in status["filing_errors"]]:
            jobs.pop(acc, None)  # not marked as processed, so the next run tries again
        for acc, (_, filed) in jobs.items():
            seen[acc] = filed or STAMP[:10]
    except Exception as e:
        status["errors"].append(f"feed: {type(e).__name__}: {str(e)[:150]}")
    back = load("insider_backfill.json", {}).get("rows", [])
    cut = (NOW - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    window = [r for r in old + new + back if r.get("date") and r["date"] >= cut]
    rows = unique(window)
    rows.sort(key=lambda r: (r.get("filed") or r["date"], r["id"]), reverse=True)
    status["new_rows"] = len(new)
    status["total_rows"] = len(rows)
    status["repeats_dropped"] = len(window) - len(rows)  # rows met more than once (stored, backfilled, amended)
    status["history_from"] = min((r["date"] for r in rows), default=None)
    (OUT / "insider_trades.json").write_text(json.dumps({"updated": STAMP, "rows": rows}))
    (OUT / "sec_seen.json").write_text(json.dumps({"seen": seen}, separators=(",", ":")))
    cl = clusters_of(rows)
    (OUT / "insider_clusters.json").write_text(json.dumps({"updated": STAMP, "rows": cl}))
    status["clusters"] = len(cl)
    status["errors"] = status["errors"][:20]
    status["filing_errors"] = status.get("filing_errors", [])[:20]
    status["seconds"] = round(time.time() - t0, 1)
    (OUT / "sec_status.json").write_text(json.dumps(status, indent=2))
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
