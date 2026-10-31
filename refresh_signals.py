"""Koochooloo signals + dashboard layer (v2). Runs every 5 minutes.
1. Pulls the latest Congress trades (kadoa-org/congress-trading-monitor, refreshed daily by its owners).
2. Merges every layer in data/ into one ranked watchlist (signals.json).
3. Builds data/dashboard.json (and data/dash/ parts): everything Koochooloo Constellation shows live.
The signal score is a transparent tally of who is buying, NOT a tested prediction. Backtest before trusting it."""
import json, re, pathlib, datetime, urllib.request

SRC = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"
OUT = pathlib.Path("data")
OUT.mkdir(exist_ok=True)
NOW = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
now = NOW.replace(microsecond=0).isoformat() + "Z"
status = {"updated": now, "ok": {}, "failed": {}}
TICK = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "KoochoolooResearch"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def load_doc(name):
    p = OUT / f"{name}.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def load(name):
    return load_doc(name).get("rows", [])


def save_rows(name, rows, **extra):
    """Write only when the content changed, so quiet periods add no commits."""
    p = OUT / f"{name}.json"
    old = load_doc(name)
    if old.get("rows") == rows and all(old.get(k) == v for k, v in extra.items()):
        return False
    p.write_text(json.dumps({"updated": now, **extra, "rows": rows}))
    return True


def days_ago(n):
    return (NOW - datetime.timedelta(days=n)).strftime("%Y-%m-%d")


def clean(n):
    n = re.sub(r"\s*\[[^\]]*\]", "", n or "")
    n = re.sub(r"\s*\([^)]*\)", "", n).strip()
    return " ".join(w if (w.isupper() and len(w) <= 4) else w[:1].upper() + w[1:].lower() for w in n.split())[:40]


def mid(t):
    lo, hi = t.get("amount_range_low") or 0, t.get("amount_range_high") or 0
    return round((lo + hi) / 2) if hi else lo


def side_of(t):
    tt = (t.get("transaction_type") or "").lower()
    return "buy" if "purchase" in tt else "sell" if "sale" in tt else None


# ---------- 1. Congress ----------
raw = []
try:
    raw = fetch(SRC)
    status["ok"]["congress_source"] = len(raw)
except Exception as e:
    status["failed"]["congress_source"] = f"{type(e).__name__}: {str(e)[:200]}"

congress = []
for t in raw:
    tk = (t.get("ticker") or "").upper()
    s = side_of(t)
    if not TICK.match(tk) or not s or (t.get("filing_date") or "") < days_ago(120):
        continue
    congress.append({"symbol": tk, "asset": clean(t.get("asset_name")), "filer": t.get("filer_name"), "fid": t.get("filer_id"),
                     "party": t.get("party"), "chamber": t.get("chamber") or t.get("branch"), "side": s,
                     "traded": t.get("transaction_date"), "filed": t.get("filing_date"),
                     "days_to_file": t.get("days_to_file"), "value_mid": mid(t), "doc": t.get("doc_url")})
if raw:
    congress.sort(key=lambda r: (r["filed"] or "", r["traded"] or ""), reverse=True)
    save_rows("congress_trades", congress)
    status["ok"]["congress_trades"] = len(congress)
else:
    congress = load("congress_trades")

agg = {}
for r in congress:
    if r["side"] == "buy" and (r["filed"] or "") >= days_ago(60):
        a = agg.setdefault(r["symbol"], {"symbol": r["symbol"], "asset": r["asset"], "filers": set(), "value": 0, "last_filed": ""})
        a["filers"].add(r["filer"]); a["value"] += r["value_mid"]; a["last_filed"] = max(a["last_filed"], r["filed"] or "")
cclusters = sorted(({**a, "filers": sorted(f for f in a["filers"] if f), "n": len(a["filers"])} for a in agg.values() if len(a["filers"]) >= 2),
                   key=lambda a: (-a["n"], -a["value"]))
save_rows("congress_clusters", cclusters)
status["ok"]["congress_clusters"] = len(cclusters)

# Hot buys for the Constellation (same method as its first snapshot): by trade month, 3/6/12-month windows
hot, hot_end = {}, None
dated = [t for t in raw if t.get("transaction_date")]
if dated:
    hot_end = max(t["transaction_date"] for t in dated)
    endm = int(hot_end[:4]) * 12 + int(hot_end[5:7]) - 1
    for win in (3, 6, 12):
        hagg = {}
        for t in dated:
            k, s = (t.get("ticker") or ""), side_of(t)
            if not s or not re.fullmatch(r"[A-Z]{1,5}", k):
                continue
            m = int(t["transaction_date"][:4]) * 12 + int(t["transaction_date"][5:7]) - 1
            if m <= endm - win:
                continue
            a = hagg.setdefault(k, {"k": k, "name": clean(t.get("asset_name") or k), "bb": set(), "sb": set(), "bv": 0, "sv": 0})
            if s == "buy":
                a["bb"].add(t.get("filer_id")); a["bv"] += mid(t)
            else:
                a["sb"].add(t.get("filer_id")); a["sv"] += mid(t)
        rows = [{"k": a["k"], "name": a["name"], "buyers": len(a["bb"]), "sellers": len(a["sb"]), "buy": round(a["bv"]),
                 "sell": round(a["sv"]), "bids": sorted(x for x in a["bb"] if x)} for a in hagg.values() if len(a["bb"]) >= 2]
        rows.sort(key=lambda r: (-(r["buyers"] - r["sellers"]), -(r["buy"] - r["sell"])))
        hot[str(win)] = rows[:14]

# ---------- 2. Insiders (deduplicated) ----------
ins_doc = load_doc("insider_trades")
insiders = list({r["id"]: r for r in ins_doc.get("rows", []) if r.get("id")}.values())
iagg = {}
for r in insiders:
    if r["side"] == "buy" and (r.get("date") or "") >= days_ago(30) and r.get("symbol"):
        a = iagg.setdefault(r["symbol"], {"symbol": r["symbol"], "issuer": r.get("issuer"), "insiders": set(), "value": 0, "last": ""})
        a["insiders"].add(r.get("insider")); a["value"] += r.get("value") or 0; a["last"] = max(a["last"], r.get("date") or "")
iclusters = sorted(({**a, "insiders": sorted(i for i in a["insiders"] if i), "n": len(a["insiders"])} for a in iagg.values() if len(a["insiders"]) >= 2),
                   key=lambda a: (-a["n"], -a["value"]))

# ---------- 3. Signals ----------
sig = {}
def row(sym, name=None):
    r = sig.setdefault(sym, {"symbol": sym, "name": name, "score": 0, "reasons": []})
    r["name"] = r["name"] or name
    return r

for c in iclusters:
    r = row(c["symbol"], c.get("issuer")); r["score"] += 3 * c["n"]
    r["reasons"].append(f"{c['n']} insiders bought ${c['value']:,} in 30 days (last {c['last']})")
for t in insiders:
    if t["side"] == "buy" and t.get("symbol") and (t.get("date") or "") >= days_ago(14) and (t.get("value") or 0) >= 100000:
        r = row(t["symbol"], t.get("issuer")); r["score"] += 2
        r["reasons"].append(f"{t.get('role') or 'Insider'} {t.get('insider')} bought ${t['value']:,} on {t['date']}")
for c in cclusters:
    r = row(c["symbol"], c.get("asset")); r["score"] += 2 * c["n"]
    r["reasons"].append(f"{c['n']} politicians bought (~${c['value']:,}, last filed {c['last_filed']})")
gainers = {q["symbol"]: q for q in load("gainers") if q.get("symbol")}
active = {q["symbol"]: q for q in load("most_active") if q.get("symbol")}
for sym, r in sig.items():
    if sym in gainers:
        r["score"] += 1
        r["reasons"].append(f"top gainer today: {gainers[sym].get('regularMarketChangePercent') or 0:+.1f}%")
    elif sym in active:
        r["reasons"].append(f"heavy trading today: {active[sym].get('regularMarketChangePercent') or 0:+.1f}%")
signals = sorted((r for r in sig.values() if r["score"] >= 3), key=lambda r: -r["score"])[:100]
save_rows("signals", signals, note="Tally of buying activity, not a tested prediction.")
status["ok"]["signals"] = len(signals)

# ---------- 4. Dashboard: everything the Constellation shows live ----------
def slim(q):
    return {"s": q.get("symbol"), "n": (q.get("shortName") or "")[:32], "p": q.get("regularMarketPrice"),
            "c": round(q.get("regularMarketChangePercent") or 0, 2), "v": q.get("regularMarketVolume"), "mc": q.get("marketCap")}

px_status, sec_status = load_doc("status"), load_doc("sec_status")
treas = load("treasury_rates")
ibuys = sorted((r for r in insiders if r["side"] == "buy"), key=lambda r: (r.get("date") or "", r.get("value") or 0), reverse=True)[:40]
isells = sorted((r for r in insiders if r["side"] == "sell" and (r.get("date") or "") >= days_ago(14)), key=lambda r: -(r.get("value") or 0))[:20]
dash = {
    "generated": now,
    "layers": {
        "prices": px_status.get("updated"), "history": px_status.get("slow_last"), "insiders": sec_status.get("updated"),
        "congress_fetched": now if raw else load_doc("dashboard").get("layers", {}).get("congress_fetched"),
        "congress_latest_filing": max((r["filed"] or "" for r in congress), default=None),
    },
    "quotes": load("quotes"),
    "sectors": load("sector_board"),
    "gainers": [slim(q) for q in load("gainers")[:25]],
    "losers": [slim(q) for q in load("losers")[:25]],
    "active": [slim(q) for q in load("most_active")[:25]],
    "treasury": {"latest": treas[-1] if treas else None, "month_ago": treas[-22] if len(treas) > 22 else None},
    "insider": {"buys": ibuys, "sells": isells, "clusters": iclusters[:30], "total": len(insiders)},
    "congress": {"latest": congress[:40], "clusters": cclusters[:30], "hot": hot, "hot_end": hot_end},
    "signals": signals[:40],
}
prev = load_doc("dashboard")
strip = lambda d: {k: v for k, v in d.items() if k != "generated"} | {"layers": {k: v for k, v in d.get("layers", {}).items() if k != "congress_fetched"}}
if strip(prev) != strip(dash) or not (OUT / "dash" / "index.json").exists():
    text = json.dumps(dash, separators=(",", ":"))
    (OUT / "dashboard.json").write_text(text)
    # The live page reads through a connector that only returns small files inline, so also split it into ~10 KB parts
    (OUT / "dash").mkdir(exist_ok=True)
    parts = [text[i:i + 10000] for i in range(0, len(text), 10000)]
    for i, chunk in enumerate(parts):
        (OUT / "dash" / f"p{i}.json").write_text(json.dumps({"g": now, "i": i, "d": chunk}))
    (OUT / "dash" / "index.json").write_text(json.dumps({"g": now, "n": len(parts)}))
    status["dashboard_changed"] = True
status["ok"]["dashboard_bytes"] = (OUT / "dashboard.json").stat().st_size
status["layers"] = {k: len(load(k)) for k in ("gainers", "most_active", "quotes", "sector_board")}
status["layers"]["insider_trades_unique"] = len(insiders)
if status.get("dashboard_changed") or status["failed"]:
    (OUT / "signals_status.json").write_text(json.dumps(status, indent=2))
print(json.dumps(status, indent=2))
