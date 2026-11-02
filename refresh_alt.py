"""Koochooloo alternative-data layer: free, public sources. Runs every weekday after the US close.

Sources (all free and public):
- FINRA daily short-sale volume (how much of each stock's trading was short selling)
- SEC fails-to-deliver (shares that were sold but not delivered on time), twice a month
- CFTC Commitments of Traders (how hedge funds and asset managers are positioned in futures), weekly
- FRED macro series (fear gauge, yield curve, credit spreads, jobless claims, dollar, oil...)
- congress-legislators (open source): which committees each member of Congress sits on
- USAspending: the largest new federal contract awards, matched to stock tickers where possible
- ApeWisdom: how often each stock is mentioned on Reddit's investing forums
- SEC XBRL frames (weekly): revenue, profit, cash flow and debt for every listed company, with a 0-5 quality score
- SEC Form 13F data sets (quarterly): how many big funds hold each stock and how that changed

Writes compact files to data/alt/ and keeps a permanent history under data/history/ (the brain's memory)."""
import collections, csv, datetime as dt, gzip, io, json, os, pathlib, re, time, urllib.error, urllib.parse, urllib.request, zipfile

D = pathlib.Path("data")
ALT = D / "alt"
HIST = D / "history"
ALT.mkdir(parents=True, exist_ok=True)
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()
STAMP = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
UA = {"User-Agent": "KoochoolooResearch github.com/Montezuma360/koochooloo-data"}
SEC_UA = {"User-Agent": os.environ.get("SEC_CONTACT") or "KoochoolooResearch github.com/Montezuma360/koochooloo-data"}  # the SEC requires a contact email
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
    page = get("https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data", headers=SEC_UA).decode("utf-8", "ignore")
    links = {m.group(2): m.group(1) for m in re.finditer(r'href="([^"]*?cnsfails(\d{6}[ab])\.zip)"', page)}
    period = max(links)
    prev = load(ALT / "fails_to_deliver.json", {}) or {}
    if prev.get("period") == period:
        return "no new file (latest %s)" % period
    url = links[period] if links[period].startswith("http") else "https://www.sec.gov" + links[period]
    z = zipfile.ZipFile(io.BytesIO(get(url, headers=SEC_UA)))
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
        raw = jget("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA)
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


# ---------- SEC company financials (XBRL frames), weekly ----------
def sec_tickers():
    raw = jget("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA)
    t2c, c2t = {}, {}
    for r in raw.values():
        t2c.setdefault(r["ticker"].upper().replace("-", "."), int(r["cik_str"]))
        c2t.setdefault(int(r["cik_str"]), r["ticker"].upper().replace("-", "."))
    return t2c, c2t


def frame(concept, period, unit="USD"):
    try:
        return jget("https://data.sec.gov/api/xbrl/frames/us-gaap/%s/%s/%s.json" % (concept, unit, period), headers=SEC_UA).get("data", [])
    except urllib.error.HTTPError:
        return []


def fundamentals(watch):
    prev = load(ALT / "fundamentals.json", {}) or {}
    if prev.get("updated", "")[:10] >= str(TODAY - dt.timedelta(days=6)):
        return "fresh (weekly)"
    t2c, c2t = sec_tickers()
    years = [TODAY.year - 3, TODAY.year - 2, TODAY.year - 1]
    flows = {"revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
             "net_income": ["NetIncomeLoss"], "op_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"]}
    stocks = {"assets": ["Assets"], "liabilities": ["Liabilities"], "equity": ["StockholdersEquity"]}
    F = {}  # (cik, year) -> {field: value}
    for field, concepts in list(flows.items()) + list(stocks.items()):
        for y in years:
            period = "CY%d" % y if field in flows else "CY%dQ4I" % y
            for c in concepts:  # first concept wins; later ones only fill gaps
                for r in frame(c, period):
                    d = F.setdefault((int(r["cik"]), y), {})
                    d.setdefault(field, float(r["val"]))
                time.sleep(0.12)
    by = {}
    for (cik, y), v in F.items():
        by.setdefault(cik, {})[y] = v
    rows, out = [], {}
    for cik, yrs in by.items():
        ys = sorted(y for y in yrs if "net_income" in yrs[y] or "revenue" in yrs[y])
        if not ys:
            continue
        y = ys[-1]
        cur, old = yrs[y], yrs.get(y - 1, {})
        rev, ni, ocf = cur.get("revenue"), cur.get("net_income"), cur.get("op_cash_flow")
        a, l = cur.get("assets"), cur.get("liabilities")
        rec = {"cik": cik, "ticker": c2t.get(cik), "year": y, "revenue": rev, "net_income": ni, "op_cash_flow": ocf, "assets": a,
               "liabilities": l, "equity": cur.get("equity"),
               "revenue_growth_pct": round(100 * (rev / old["revenue"] - 1), 1) if rev and old.get("revenue") else None,
               "net_margin_pct": round(100 * ni / rev, 1) if rev and ni is not None and rev > 0 else None,
               "cash_backed": (ocf > ni) if ocf is not None and ni is not None else None,
               "liabilities_to_assets": round(l / a, 2) if a and l is not None else None}
        checks = [ni is not None and ni > 0, ocf is not None and ocf > 0, rec["cash_backed"] is True,
                  (rec["revenue_growth_pct"] or -1) > 0, rec["liabilities_to_assets"] is not None and rec["liabilities_to_assets"] < 0.8]
        rec["quality_score"] = sum(bool(x) for x in checks)  # 0-5: profit, cash flow, cash-backed, growing, not over-borrowed
        rows.append(rec)
        if rec["ticker"]:
            out[rec["ticker"]] = rec
    f = HIST / "fundamentals" / (TODAY.strftime("%Y-%m") + ".csv.gz")
    f.parent.mkdir(parents=True, exist_ok=True)
    cols = ["cik", "ticker", "year", "revenue", "net_income", "op_cash_flow", "assets", "liabilities", "equity", "quality_score"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] for c in cols])
    f.write_bytes(gzip.compress(buf.getvalue().encode()))
    save("fundamentals.json", {"updated": STAMP, "source": "SEC XBRL frames (company annual reports)", "companies": len(rows),
                               "note": "Latest full year each company reported. quality_score 0-5 = profitable, positive operating cash flow, cash flow above profit, revenue growing, liabilities under 80% of assets.",
                               "watch": {s: out[s] for s in sorted(watch) if s in out},
                               "top_quality_watch": sorted([s for s in watch if s in out and out[s]["quality_score"] == 5])})
    return "%d companies, %d watched" % (len(rows), sum(1 for s in watch if s in out))


# ---------- SEC Form 13F: what big funds hold, quarterly ----------
MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def label_key(lbl):
    m = re.match(r"(\d{2})([a-z]{3})(\d{4})-(\d{2})([a-z]{3})(\d{4})$", lbl)
    if m:
        return "%s-%02d-%s" % (m.group(6), MON.get(m.group(5), 0), m.group(4))
    m = re.match(r"(\d{4})q(\d)$", lbl)
    return "%s-%02d-99" % (m.group(1), 3 * int(m.group(2))) if m else lbl


def cusip_map():
    """CUSIP -> ticker from the last three months of SEC fails-to-deliver files (they list both)."""
    prev = load(ALT / "cusip_map.json", {}) or {}
    if prev.get("updated", "")[:10] >= str(TODAY - dt.timedelta(days=25)):
        return prev["map"]
    page = get("https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data", headers=SEC_UA).decode("utf-8", "ignore")
    links = {m.group(2): m.group(1) for m in re.finditer(r'href="([^"]*?cnsfails(\d{6}[ab])\.zip)"', page)}
    mp = dict(prev.get("map", {}))
    for p in sorted(links)[-6:]:
        url = links[p] if links[p].startswith("http") else "https://www.sec.gov" + links[p]
        z = zipfile.ZipFile(io.BytesIO(get(url, headers=SEC_UA)))
        for line in z.open(z.namelist()[0]).read().decode("latin-1").splitlines()[1:]:
            q = line.split("|")
            if len(q) >= 3 and len(q[1]) == 9 and q[2]:
                mp[q[1]] = q[2]
        time.sleep(0.3)
    save("cusip_map.json", {"updated": STAMP, "source": "SEC fails-to-deliver files", "map": mp})
    return mp


def holdings_13f(url):
    """Sum every fund's 13F stock holdings (not options) by CUSIP for one SEC data-set file.
    Filer mistakes are common (e.g. billions of shares reported with a $0 value), so a line only counts when its
    implied price (value / shares) is within 3x of that security's median across all funds."""
    z = zipfile.ZipFile(io.BytesIO(get(url, headers=SEC_UA, timeout=600)))
    names = {n.split("/")[-1].upper(): n for n in z.namelist()}
    sub = {}
    periods = collections.Counter()
    with z.open(names["SUBMISSION.TSV"]) as fh:
        for r in csv.DictReader(io.TextIOWrapper(fh, "latin-1"), delimiter="\t"):
            if r.get("SUBMISSIONTYPE") == "13F-HR":
                sub[r["ACCESSION_NUMBER"]] = (r.get("CIK"), r.get("PERIODOFREPORT"))
                periods[r.get("PERIODOFREPORT")] += 1
    period = periods.most_common(1)[0][0]
    latest = {}  # one filing per fund for the period
    for acc, (cik, per) in sorted(sub.items()):
        if per == period:
            latest[cik] = acc
    keep = set(latest.values())

    def lines():
        with z.open(names["INFOTABLE.TSV"]) as fh:
            for r in csv.DictReader(io.TextIOWrapper(fh, "latin-1"), delimiter="\t"):
                acc = r.get("ACCESSION_NUMBER")
                if acc not in keep or (r.get("PUTCALL") or "").strip() or (r.get("SSHPRNAMTTYPE") or "SH").strip() != "SH":
                    continue
                try:
                    v, sh = float(r.get("VALUE") or 0), float(r.get("SSHPRNAMT") or 0)
                except ValueError:
                    continue
                if v > 0 and sh > 0:
                    yield r["CUSIP"].upper(), sub[acc][0], sh, v
    prices = collections.defaultdict(list)
    for c, _, sh, v in lines():
        prices[c].append(v / sh)
    med = {c: sorted(p)[len(p) // 2] for c, p in prices.items()}
    del prices
    agg, dropped = {}, 0
    for c, cik, sh, v in lines():
        m = med[c]
        if not (m / 3 <= v / sh <= m * 3):
            dropped += 1
            continue
        a = agg.setdefault(c, [set(), 0.0, 0.0])
        a[0].add(cik)
        a[1] += sh
        a[2] += v
    print("13F", period, "funds", len(keep), "lines dropped as filer errors:", dropped)
    return period, len(keep), {c: (len(a[0]), a[1], a[2]) for c, a in agg.items()}


def read_13f(f):
    with gzip.open(f, "rt") as fh:
        return {r["cusip"]: (int(r["holders"]), float(r["shares"]), float(r["value"])) for r in csv.DictReader(fh)}


def write_13f(f, agg):
    f.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["cusip", "holders", "shares", "value"])
    for c, (h, s, v) in sorted(agg.items()):
        w.writerow([c, h, int(s), int(v)])
    f.write_bytes(gzip.compress(buf.getvalue().encode()))


def fund_holdings(watch):
    page = get("https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets", headers=SEC_UA).decode("utf-8", "ignore")
    links = {}
    for m in re.finditer(r'href="([^"]*?/([0-9a-z\-]+)_form13f\.zip)"', page, re.I):
        links[m.group(2).lower()] = m.group(1) if m.group(1).startswith("http") else "https://www.sec.gov" + m.group(1)
    if not links:
        raise RuntimeError("no 13F data-set links found")
    order = sorted(links, key=label_key)
    arch = HIST / "13f" / "v2"  # v2: filer-error lines removed
    cur_lbl, prev_lbl = order[-1], order[-2] if len(order) > 1 else None
    if (arch / (cur_lbl + ".csv.gz")).exists() and (ALT / "fund_holdings_13f.json").exists():
        return "no new file (latest %s)" % cur_lbl
    meta = load(arch / "periods.json", {}) or {}
    for lbl in [x for x in (prev_lbl, cur_lbl) if x]:
        f = arch / (lbl + ".csv.gz")
        if not f.exists():
            period, funds, agg = holdings_13f(links[lbl])
            write_13f(f, agg)
            meta[lbl] = {"period": period, "funds": funds, "securities": len(agg)}
            arch.mkdir(parents=True, exist_ok=True)
            (arch / "periods.json").write_text(json.dumps(meta, indent=1))
    cur, old = read_13f(arch / (cur_lbl + ".csv.gz")), read_13f(arch / (prev_lbl + ".csv.gz")) if prev_lbl else {}
    mp = cusip_map()
    rows = []
    for c, (h, s, v) in cur.items():
        t = mp.get(c)
        if not t:
            continue
        h0, s0, v0 = old.get(c, (0, 0.0, 0.0))
        new = bool(old) and c not in old  # new CUSIP: IPO, spin-off or a company that changed its share code
        rows.append({"symbol": t, "cusip": c, "funds": h, "funds_change": h - h0 if old and not new else None, "new_cusip": new,
                     "shares": int(s), "shares_change_pct": round(100 * (s / s0 - 1), 1) if s0 else None, "value_usd": int(v)})
    by = {}
    for r in rows:  # a ticker can have several CUSIPs (share classes); keep the most widely held
        if r["symbol"] not in by or r["funds"] > by[r["symbol"]]["funds"]:
            by[r["symbol"]] = r
    big = [r for r in by.values() if r["funds"] >= 50 and r["funds_change"] is not None]
    save("fund_holdings_13f.json", {"updated": STAMP, "file": cur_lbl, "compared_with": prev_lbl, "period": meta.get(cur_lbl, {}).get("period"),
                                    "funds_reporting": meta.get(cur_lbl, {}).get("funds"),
                                    "note": "Form 13F: every US fund manager with $100M+ must report its stock holdings 45 days after each quarter. Shows what big money owned, not what it is doing today.",
                                    "most_funds_added": sorted(big, key=lambda r: -r["funds_change"])[:25],
                                    "most_funds_cut": sorted(big, key=lambda r: r["funds_change"])[:25],
                                    "new_listings_most_held": sorted([r for r in by.values() if r["new_cusip"]], key=lambda r: -r["funds"])[:25],
                                    "watch": {s: by[s] for s in sorted(watch) if s in by}})
    return "%s: %d securities, %d mapped to tickers" % (cur_lbl, len(cur), len(by))


def main():
    watch = watchlist()
    for name, fn in (("short_volume", lambda: short_volume(watch)), ("fails_to_deliver", lambda: fails_to_deliver(watch)), ("cot", cot),
                     ("macro", macro), ("committees", committees), ("gov_contracts", contracts), ("reddit", reddit),
                     ("fundamentals", lambda: fundamentals(watch)), ("fund_holdings_13f", lambda: fund_holdings(watch))):
        try:
            STATUS["ok"][name] = fn()
        except Exception as e:
            STATUS["failed"][name] = repr(e)[:200]
    save("status.json", STATUS)
    print(json.dumps(STATUS, indent=1))


if __name__ == "__main__":
    main()
