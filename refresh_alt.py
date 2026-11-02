"""Koochooloo alternative-data layer: free, public sources. Runs every weekday after the US close.

Sources (all free and public):
- FINRA daily short-sale volume (how much of each stock's trading was short selling)
- SEC fails-to-deliver (shares that were sold but not delivered on time), twice a month
- CFTC Commitments of Traders (how hedge funds and asset managers are positioned in futures), weekly
- FRED macro series (fear gauge, yield curve, credit spreads, jobless claims, dollar, oil...)
- congress-legislators (open source): which committees each member of Congress sits on
- USAspending: the largest new federal contract awards, matched to stock tickers where possible
- ApeWisdom: how often each stock is mentioned on Reddit's investing forums

Writes compact files to data/alt/ and keeps a permanent history under data/history/ (the brain's memory)."""
import csv, datetime as dt, gzip, io, json, pathlib, re, time, urllib.error, urllib.parse, urllib.request, zipfile

D = pathlib.Path("data")
ALT = D / "alt"
HIST = D / "history"
ALT.mkdir(parents=True, exist_ok=True)
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()
STAMP = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
UA = {"User-Agent": "KoochoolooResearch github.com/Montezuma360/koochooloo-data"}
STATUS = {"updated": STAMP, "ok": {}, "failed": {}}


def get(url, data=None, headers=None, tries=3, timeout=90):
    h = dict(UA, **(headers or {}))
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            if i == tries - 1:
                raise
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(3 * (i + 1))


def jget(url, **k):
    return json.loads(get(url, **k))


def save(name, obj):
    (ALT / name).write_text(json.dumps(obj, indent=1, default=str))


def load(path, default=None):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return default


def watchlist():
    """Stocks the brain cares about: signals, quotes, insider/Congress names and paper-trading positions."""
    w = set()
    for name, key in (("signals.json", "symbol"), ("quotes.json", "symbol"), ("insider_trades.json", "symbol"), ("congress_clusters.json", "symbol")):
        doc = load(D / name, {}) or {}
        rows = doc.get("rows", doc if isinstance(doc, list) else [])
        for r in rows[:300]:
            s = str(r.get(key, "")).upper()
            if re.match(r"^[A-Z]{1,5}(\.[A-Z])?$", s):
                w.add(s)
    st = load(D / "paper" / "state.json", {}) or {}
    for s in (st.get("strategies") or {}).values():
        for q in s.get("open", []) + s.get("pending", []):
            w.add(q["sym"])
    return w


def business_days_back(n):
    d, out = TODAY, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out


# ---------- FINRA daily short-sale volume ----------
def short_volume(watch):
    arch = HIST / "short_volume"
    days = business_days_back(25)
    have = {}
    for d in days:
        f = arch / str(d.year) / (d.strftime("%Y%m%d") + ".csv.gz")
        if not f.exists():
            try:
                raw = get("https://cdn.finra.org/equity/regsho/daily/CNMSshvol%s.txt" % d.strftime("%Y%m%d")).decode("latin-1")
            except Exception:
                continue  # holiday, weekend or not yet published
            rows = []
            for line in raw.splitlines()[1:]:
                p = line.split("|")
                if len(p) >= 5 and p[1]:
                    try:
                        rows.append((p[1], int(float(p[2])), int(float(p[4]))))
                    except ValueError:
                        pass
            if not rows:
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            buf = io.StringIO()
            csv.writer(buf).writerows([("symbol", "short", "total")] + rows)
            f.write_bytes(gzip.compress(buf.getvalue().encode()))
        with gzip.open(f, "rt") as fh:
            have[d] = {r["symbol"]: (int(r["short"]), int(r["total"])) for r in csv.DictReader(fh)}
    if not have:
        raise RuntimeError("no FINRA files found for the last 25 business days")
    order = sorted(have)
    last = have[order[-1]]
    recent = order[-20:]

    def avg_ratio(sym):
        s = sum(have[d].get(sym, (0, 0))[0] for d in recent)
        t = sum(have[d].get(sym, (0, 0))[1] for d in recent)
        return round(s / t, 3) if t else None
    tot_s = sum(v[0] for v in last.values())
    tot_t = sum(v[1] for v in last.values())
    liquid = [(s, v) for s, v in last.items() if v[1] >= 2_000_000 and re.match(r"^[A-Z]{1,5}$", s)]
    top = sorted(liquid, key=lambda x: -x[1][0] / x[1][1])[:25]
    out = {"date": str(order[-1]), "days_in_average": len(recent), "market_short_ratio": round(tot_s / tot_t, 3) if tot_t else None,
           "note": "Share of each stock's reported volume that was short selling (FINRA consolidated NMS). Above about 0.5 is high, but market makers short routinely, so compare a stock with its own 20-day average.",
           "highest_ratio_liquid": [{"symbol": s, "ratio": round(v[0] / v[1], 3), "avg20": avg_ratio(s), "volume": v[1]} for s, v in top],
           "watch": {s: {"ratio": round(last[s][0] / last[s][1], 3) if last[s][1] else None, "avg20": avg_ratio(s), "volume": last[s][1]}
                     for s in sorted(watch) if s in last}}
    save("short_volume.json", out)
    return "%s, %d stocks, %d watched" % (out["date"], len(last), len(out["watch"]))


# ---------- SEC fails-to-deliver ----------
def fails_to_deliver(watch):
    page = get("https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data", headers={"User-Agent": "KoochoolooResearch research contact via github.com/Montezuma360"}).decode("utf-8", "ignore")
    links = {m.group(2): m.group(1) for m in re.finditer(r'href="([^"]*?cnsfails(\d{6}[ab])\.zip)"', page)}
    period = max(links)
    prev = load(ALT / "fails_to_deliver.json", {}) or {}
    if prev.get("period") == period:
        return "no new file (latest %s)" % period
    url = links[period] if links[period].startswith("http") else "https://www.sec.gov" + links[period]
    z = zipfile.ZipFile(io.BytesIO(get(url)))
    txt = z.open(z.namelist()[0]).read().decode("latin-1")
    agg = {}
    keep = []
    for line in txt.splitlines()[1:]:
        p = line.split("|")
        if len(p) < 6 or not p[2]:
            continue
        try:
            qty, price = int(p[3]), float(p[5] or 0)
        except ValueError:
            continue
        val = qty * price
        a = agg.setdefault(p[2], {"symbol": p[2], "name": p[4].strip(), "days": 0, "max_qty": 0, "max_value": 0.0})
        a["days"] += 1
        a["max_qty"] = max(a["max_qty"], qty)
        a["max_value"] = max(a["max_value"], val)
        if val >= 10_000:
            keep.append((p[0], p[2], qty, price))
    f = HIST / "fails_to_deliver" / (period + ".csv.gz")
    f.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    csv.writer(buf).writerows([("settlement_date", "symbol", "quantity", "price")] + keep)
    f.write_bytes(gzip.compress(buf.getvalue().encode()))
    top = sorted(agg.values(), key=lambda a: -a["max_value"])[:30]
    out = {"period": period, "note": "Half-month SEC file of shares sold but not delivered on settlement day. Large, persistent fails can signal heavy shorting or settlement stress.",
           "largest": [dict(a, max_value=round(a["max_value"])) for a in top],
           "watch": {s: dict(agg[s], max_value=round(agg[s]["max_value"])) for s in sorted(watch) if s in agg}}
    save("fails_to_deliver.json", out)
    return "%s, %d stocks" % (period, len(agg))


# ---------- CFTC Commitments of Traders ----------
TFF = {"13874A": "S&P 500 (E-mini)", "209742": "Nasdaq-100 (mini)", "239742": "Russell 2000 (E-mini)", "1170E1": "VIX futures",
       "043602": "US 10-year Treasury note", "042601": "US 2-year Treasury note", "099741": "Euro", "096742": "British pound",
       "097741": "Japanese yen", "133741": "Bitcoin (CME)"}
DIS = {"088691": "Gold", "084691": "Silver", "085692": "Copper", "067651": "Crude oil (WTI)", "023651": "Natural gas"}


def cot():
    since = (TODAY - dt.timedelta(days=3 * 366)).isoformat()

    def pull(ds, codes):
        q = urllib.parse.urlencode({"$where": "cftc_contract_market_code in(%s) AND report_date_as_yyyy_mm_dd >= '%s'" % (",".join("'%s'" % c for c in codes), since),
                                    "$order": "report_date_as_yyyy_mm_dd ASC", "$limit": 5000})
        return jget("https://publicreporting.cftc.gov/resource/%s.json?%s" % (ds, q))
    out, latest = [], None
    for ds, codes, lk, sk, who in (("gpe5-46if", TFF, "lev_money_positions_long", "lev_money_positions_short", "hedge funds (leveraged money)"),
                                   ("72hh-3qpy", DIS, "m_money_positions_long_all", "m_money_positions_short_all", "hedge funds (managed money)")):
        rows = pull(ds, codes)
        by = {}
        for r in rows:
            try:
                net = float(r[lk]) - float(r[sk])
                oi = float(r.get("open_interest_all") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            by.setdefault(r["cftc_contract_market_code"], []).append((r["report_date_as_yyyy_mm_dd"][:10], net, oi))
        for code, ser in by.items():
            ser.sort()
            nets = [n for _, n, _ in ser]
            d0, n0, oi0 = ser[-1]
            latest = max(latest or d0, d0)
            pct = round(100 * sum(1 for n in nets if n <= n0) / len(nets))
            out.append({"code": code, "market": codes[code], "who": who, "report_date": d0, "net_contracts": int(n0),
                        "net_pct_of_open_interest": round(100 * n0 / oi0, 1) if oi0 else None,
                        "change_1w": int(n0 - nets[-2]) if len(nets) > 1 else None,
                        "percentile_3y": pct, "history": [[d, int(n)] for d, n, _ in ser[-52:]]})
    save("cot.json", {"report_date": latest, "note": "Weekly CFTC positioning. percentile_3y near 100 means hedge funds are the most bullish they have been in 3 years; near 0 the most bearish. Extremes often come before reversals.",
                      "markets": sorted(out, key=lambda x: x["market"])})
    return "%s, %d markets" % (latest, len(out))


# ---------- FRED macro ----------
FRED = {"VIXCLS": "VIX fear gauge", "T10Y2Y": "10-year minus 2-year Treasury yield (yield curve)", "T10Y3M": "10-year minus 3-month Treasury yield",
        "BAMLH0A0HYM2": "High-yield credit spread", "ICSA": "Initial jobless claims (weekly)", "UNRATE": "Unemployment rate",
        "CPIAUCSL": "Consumer price index", "FEDFUNDS": "Fed funds rate", "DTWEXBGS": "US dollar index (broad)", "DCOILWTICO": "WTI crude oil price",
        "NFCI": "Chicago Fed financial conditions (above 0 = tighter than average)", "SAHMREALTIME": "Sahm recession indicator (0.5+ has signalled recessions)"}


def macro():
    out = {}
    for sid, label in FRED.items():
        try:
            txt = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + sid).decode()
            pts = []
            for line in txt.splitlines()[1:]:
                d, _, v = line.partition(",")
                try:
                    pts.append((d, float(v)))
                except ValueError:
                    pass
            pts = [p for p in pts if p[0] >= str(TODAY - dt.timedelta(days=3 * 366))]
            if not pts:
                continue
            d, v = pts[-1]

            def back(days):
                tgt = str(dt.date.fromisoformat(d) - dt.timedelta(days=days))
                older = [p for p in pts if p[0] <= tgt]
                return older[-1][1] if older else None
            m1, y1 = back(30), back(365)
            item = {"label": label, "date": d, "value": v, "change_1m": round(v - m1, 3) if m1 is not None else None,
                    "change_1y": round(v - y1, 3) if y1 is not None else None, "history": pts[-260:]}
            if sid == "CPIAUCSL" and y1:
                item["inflation_yoy_pct"] = round(100 * (v / y1 - 1), 2)
            out[sid] = item
        except Exception as e:
            STATUS["failed"]["macro_" + sid] = repr(e)[:120]
    save("macro.json", {"updated": STAMP, "series": out})
    return "%d series" % len(out)


# ---------- Congress committees (open source) ----------
def committees():
    prev = load(ALT / "committees.json", {}) or {}
    if prev.get("updated", "")[:10] >= str(TODAY - dt.timedelta(days=6)):
        return "fresh (weekly)"
    base = "https://unitedstates.github.io/congress-legislators/"
    leg = jget(base + "legislators-current.json")
    com = jget(base + "committees-current.json")
    mem = jget(base + "committee-membership-current.json")
    names = {}
    for c in com:
        names[c["thomas_id"]] = c["name"]
        for s in c.get("subcommittees", []):
            names[c["thomas_id"] + s["thomas_id"]] = c["name"] + ": " + s["name"]
    people = {}
    for p in leg:
        t = p["terms"][-1]
        nm = p["name"].get("official_full") or (p["name"]["first"] + " " + p["name"]["last"])
        people[p["id"]["bioguide"]] = {"name": nm, "last": p["name"]["last"], "party": t.get("party"), "chamber": t.get("type"), "state": t.get("state"), "committees": []}
    for cid, members in mem.items():
        for m in members:
            b = m.get("bioguide")
            if b in people:
                people[b]["committees"].append({"committee": names.get(cid, cid), "role": m.get("title") or ("member, " + m.get("party", ""))})
    save("committees.json", {"updated": STAMP, "source": "github.com/unitedstates/congress-legislators (public domain)", "members": people})
    return "%d members" % len(people)


# ---------- USAspending federal contracts ----------
def norm(s):
    s = re.sub(r"[^A-Z0-9 ]", " ", str(s).upper())
    s = re.sub(r"\b(THE|INCORPORATED|INC|CORPORATION|CORP|CO|COMPANY|LLC|LTD|PLC|LP|HOLDINGS|HOLDING|GROUP|NV|SA|AG)\b", " ", s)
    return " ".join(s.split())


def contracts():
    tick = load(ALT / "company_tickers.json")
    if not tick or tick.get("updated", "")[:10] < str(TODAY - dt.timedelta(days=7)):
        raw = jget("https://www.sec.gov/files/company_tickers.json")
        tick = {"updated": STAMP, "by_name": {}}
        for r in raw.values():
            tick["by_name"].setdefault(norm(r["title"]), r["ticker"])
        save("company_tickers.json", tick)
    body = {"filters": {"award_type_codes": ["A", "B", "C", "D"],
                        "time_period": [{"start_date": str(TODAY - dt.timedelta(days=7)), "end_date": str(TODAY)}]},
            "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Start Date", "Description"],
            "limit": 100, "sort": "Award Amount", "order": "desc"}
    res = jget("https://api.usaspending.gov/api/v2/search/spending_by_award/", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    awards = []
    for r in res.get("results", []):
        awards.append({"award_id": r.get("Award ID"), "recipient": r.get("Recipient Name"), "ticker": tick["by_name"].get(norm(r.get("Recipient Name"))),
                       "amount": r.get("Award Amount"), "agency": r.get("Awarding Agency"), "start": r.get("Start Date"),
                       "description": (r.get("Description") or "")[:160]})
    save("gov_contracts.json", {"updated": STAMP, "window_days": 7, "note": "Largest federal contract awards with activity in the last 7 days (USAspending.gov). Amount is the award's total value, not this week's payment.",
                                "awards": awards, "public_company_awards": [a for a in awards if a["ticker"]]})
    return "%d awards, %d matched to tickers" % (len(awards), sum(1 for a in awards if a["ticker"]))


# ---------- Reddit mentions (ApeWisdom) ----------
def reddit():
    rows = []
    for page in (1, 2):
        rows += jget("https://apewisdom.io/api/v1.0/filter/all-stocks/page/%d" % page).get("results", [])
    clean = [{"rank": r.get("rank"), "symbol": r.get("ticker"), "name": re.sub(r"&amp;", "&", r.get("name", "")), "mentions": r.get("mentions"),
              "mentions_24h_ago": r.get("mentions_24h_ago"), "rank_24h_ago": r.get("rank_24h_ago"), "upvotes": r.get("upvotes")} for r in rows]
    f = HIST / "reddit" / str(TODAY.year) / (str(TODAY) + ".json")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(clean))
    rising = sorted([r for r in clean if (r["mentions"] or 0) >= 10], key=lambda r: -((r["mentions"] or 0) / max(1, r["mentions_24h_ago"] or 0)))[:15]
    save("reddit_mentions.json", {"updated": STAMP, "source": "apewisdom.io (Reddit investing communities)", "top": clean[:50], "rising": rising})
    return "%d stocks" % len(clean)


def main():
    watch = watchlist()
    for name, fn in (("short_volume", lambda: short_volume(watch)), ("fails_to_deliver", lambda: fails_to_deliver(watch)), ("cot", cot),
                     ("macro", macro), ("committees", committees), ("gov_contracts", contracts), ("reddit", reddit)):
        try:
            STATUS["ok"][name] = fn()
        except Exception as e:
            STATUS["failed"][name] = repr(e)[:200]
    save("status.json", STATUS)
    print(json.dumps(STATUS, indent=1))


if __name__ == "__main__":
    main()
