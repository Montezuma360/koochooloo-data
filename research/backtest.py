"""Koochooloo research lab: would following insider buying, Congress buying or sector
momentum have beaten simply holding the S&P 500 (SPY)?

Realistic rules (no hindsight):
- A trade starts only after the filing is public: buy at the next session's open after the filing date.
- Costs per side: 0.15% for stocks priced $10+, 0.40% below $10 (wider spreads), 0.05% for sector ETFs.
- Skip penny stocks (under $1) and illiquid stocks (20-day average traded value under $1M).
- Hold a fixed number of trading days, then sell at that day's close.
- Portfolio test: 20 equal slots, a signal is skipped when all slots are full, idle cash earns nothing.

Known bias: stocks that were delisted or renamed have no Yahoo history, so their trades are left out
(counted as "nodata" in the output).

Writes data/research/backtest.json. Run from GitHub Actions (workflow_dispatch)."""
import io, json, os, re, time, zipfile, collections, datetime as dt, pathlib, urllib.request
import concurrent.futures as cf
import numpy as np
import pandas as pd
import yfinance as yf

OUT = pathlib.Path("data/research")
OUT.mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": os.environ.get("SEC_CONTACT") or "KoochoolooResearch github.com/Montezuma360/koochooloo-data"}
HOLDS = (20, 60, 120)
SLOTS = 20
TICK = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
KADOA = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/"
SECTORS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
LOG = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    LOG.append(s)


def get(url, headers=None, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or UA)
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(4 * (i + 1))


# ---------------- signals ----------------
def insider_purchases():
    """Open-market purchases (Form 4, code P) from the SEC's quarterly insider data sets."""
    page = get("https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets").decode("utf-8", "ignore")
    links = {}
    for path, q in re.findall(r'href="([^"]*?((?:19|20)\d\dq\d)_form345\.zip)"', page):
        links[q] = path if path.startswith("http") else "https://www.sec.gov" + path
    frames = []
    for q in sorted(links):
        if q < "2014q1":
            continue
        try:
            z = zipfile.ZipFile(io.BytesIO(get(links[q])))
            nd = pd.read_csv(z.open("NONDERIV_TRANS.tsv"), sep="\t", dtype=str, on_bad_lines="skip",
                             usecols=["ACCESSION_NUMBER", "TRANS_CODE", "TRANS_SHARES", "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"])
            nd = nd[(nd.TRANS_CODE == "P") & (nd.TRANS_ACQUIRED_DISP_CD == "A")].copy()
            acc = set(nd.ACCESSION_NUMBER)
            sub = pd.read_csv(z.open("SUBMISSION.tsv"), sep="\t", dtype=str, on_bad_lines="skip",
                              usecols=["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERTRADINGSYMBOL"])
            sub = sub[sub.ACCESSION_NUMBER.isin(acc) & (sub.DOCUMENT_TYPE == "4")]
            own = pd.read_csv(z.open("REPORTINGOWNER.tsv"), sep="\t", dtype=str, on_bad_lines="skip",
                              usecols=["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNER_RELATIONSHIP"])
            own = own[own.ACCESSION_NUMBER.isin(acc)].drop_duplicates("ACCESSION_NUMBER")
            nd["value"] = pd.to_numeric(nd.TRANS_SHARES, errors="coerce") * pd.to_numeric(nd.TRANS_PRICEPERSHARE, errors="coerce")
            v = nd.groupby("ACCESSION_NUMBER", as_index=False)["value"].sum()
            m = sub.merge(v, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER", how="left")
            frames.append(m)
            log("insiders", q, len(m))
            del z, nd, sub, own
        except Exception as e:
            log("insiders", q, "FAILED", repr(e)[:150])
    df = pd.concat(frames, ignore_index=True)
    df["filed"] = pd.to_datetime(df.FILING_DATE, format="%d-%b-%Y", errors="coerce")
    miss = df.filed.isna()
    if miss.any():
        df.loc[miss, "filed"] = pd.to_datetime(df.loc[miss, "FILING_DATE"], errors="coerce")
    df["t"] = df.ISSUERTRADINGSYMBOL.fillna("").str.upper().str.strip().str.replace("-", ".", regex=False)
    df = df[df.t.str.match(TICK) & df.filed.notna() & (df.value > 0)].copy()
    df["exec"] = df.RPTOWNER_RELATIONSHIP.fillna("").str.contains("Director|Officer", case=False)
    return df.sort_values("filed")


def congress_purchases():
    """Every disclosed stock purchase by members of Congress (kadoa-org data, from 2014)."""
    filers = [f for f in json.loads(get(KADOA + "filers.json")) if f.get("branch") == "congress"]

    def one(f):
        try:
            return json.loads(get(KADOA + "filer/" + f["id"] + ".json")).get("trades", [])
        except Exception:
            return []
    rows = []
    with cf.ThreadPoolExecutor(16) as ex:
        for f, trades in zip(filers, ex.map(one, filers)):
            for t in trades:
                tk = str(t.get("ticker") or "").upper()
                if (str(t.get("transaction_type", "")).lower().startswith("purchase") and TICK.match(tk)
                        and t.get("asset_type") in ("ST", "Stock", None) and t.get("filing_date")):
                    rows.append((tk, pd.Timestamp(t["filing_date"][:10]), f["id"], t.get("amount_range_low") or 0))
    rows.sort(key=lambda r: r[1])
    log("congress purchases", len(rows), "from", len(filers), "members")
    return rows


def cluster_events(rows, n_needed, window, refire=60, min_value=0):
    """rows: (ticker, date, buyer, value) sorted by date. Fires when n distinct buyers fall inside the window."""
    ev, hist, last = [], collections.defaultdict(list), {}
    for t, d, who, val in rows:
        if val < min_value:
            continue
        h = hist[t]
        h.append((d, who))
        while h and (d - h[0][0]).days > window:
            h.pop(0)
        if len({w for _, w in h}) >= n_needed and (t not in last or (d - last[t]).days > refire):
            last[t] = d
            ev.append((t, d))
    return ev


# ---------------- prices ----------------
def prices(tickers, start="2013-11-01"):
    px, todo = {}, sorted(set(tickers))
    for attempt in range(2):
        failed = []
        for i in range(0, len(todo), 150):
            chunk = todo[i:i + 150]
            ys = [c.replace(".", "-") for c in chunk]
            try:
                df = yf.download(ys, start=start, interval="1d", auto_adjust=True, group_by="ticker",
                                 threads=True, progress=False)
            except Exception as e:
                log("price batch failed", repr(e)[:100])
                failed += chunk
                continue
            for c, y in zip(chunk, ys):
                try:
                    s = (df[y] if len(ys) > 1 else df)[["Open", "Close", "Volume"]].dropna()
                    s.index = pd.DatetimeIndex(s.index).tz_localize(None)
                    if len(s):
                        px[c] = s
                    else:
                        failed.append(c)
                except Exception:
                    failed.append(c)
            time.sleep(1.5)
        todo = failed
        if not todo:
            break
        time.sleep(20)
    log("prices", len(px), "of", len(set(tickers)), "tickers")
    return px


# ---------------- simulation ----------------
def trades(events, H, px, spy):
    out = []
    for t, d in events:
        s = px.get(t)
        if s is None:
            out.append({"st": "nodata", "t": t, "d": d})
            continue
        i = s.index.searchsorted(pd.Timestamp(d) + pd.Timedelta(days=1))
        if i >= len(s) or i < 1 or (s.index[i] - pd.Timestamp(d)).days > 7:
            out.append({"st": "noentry", "t": t, "d": d})
            continue
        pin = s.Open.iloc[i]
        liq = (s.Close.iloc[max(0, i - 20):i] * s.Volume.iloc[max(0, i - 20):i]).mean()
        if not (pin >= 1 and liq >= 1e6):
            out.append({"st": "illiquid", "t": t, "d": d})
            continue
        j = i + H - 1
        if j >= len(s):
            out.append({"st": "open", "t": t, "d": d})
            continue
        pout = s.Close.iloc[j]
        if not pout > 0:
            out.append({"st": "bad", "t": t, "d": d})
            continue
        c = 0.0015 if pin >= 10 else 0.004
        r = pout / pin * (1 - c) / (1 + c) - 1
        a = spy.index.searchsorted(s.index[i])
        b = min(spy.index.searchsorted(s.index[j]), len(spy) - 1)
        m = spy.Close.iloc[b] / spy.Open.iloc[a] - 1
        out.append({"st": "done", "t": t, "d": d, "in": s.index[i], "out": s.index[j], "pin": pin, "pout": pout,
                    "c": c, "r": r, "spy": m, "x": r - m})
    return out


def summary(T, lo=None, hi=None):
    D = [t for t in T if t["st"] == "done" and (lo is None or t["d"].year >= lo) and (hi is None or t["d"].year <= hi)]
    if len(D) < 5:
        return {"n": len(D)}
    r = np.array([t["r"] for t in D])
    x = np.array([t["x"] for t in D])
    return {"n": len(D), "win_pct": round(100 * (r > 0).mean(), 1), "beat_spy_pct": round(100 * (x > 0).mean(), 1),
            "avg_pct": round(100 * r.mean(), 2), "median_pct": round(100 * np.median(r), 2),
            "spy_avg_pct": round(100 * np.mean([t["spy"] for t in D]), 2),
            "avg_excess_pct": round(100 * x.mean(), 2), "median_excess_pct": round(100 * np.median(x), 2),
            "t_stat": round(float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))), 2) if x.std() > 0 else None}


def by_year(T):
    yrs = collections.defaultdict(list)
    for t in T:
        if t["st"] == "done":
            yrs[t["d"].year].append(t["x"])
    return {str(y): {"n": len(v), "avg_excess_pct": round(100 * float(np.mean(v)), 2)} for y, v in sorted(yrs.items())}


def curve_stats(c, spy_close):
    c = c.dropna()
    yrs = (c.index[-1] - c.index[0]).days / 365.25
    s = spy_close[(spy_close.index >= c.index[0]) & (spy_close.index <= c.index[-1])]
    s = s / s.iloc[0]
    dd = lambda z: round(float(100 * (z / z.cummax() - 1).min()), 1)
    return {"start": str(c.index[0].date()), "end": str(c.index[-1].date()),
            "growth_x": round(float(c.iloc[-1] / c.iloc[0]), 2), "cagr_pct": round(100 * float((c.iloc[-1] / c.iloc[0]) ** (1 / yrs) - 1), 1),
            "max_drawdown_pct": dd(c), "spy_growth_x": round(float(s.iloc[-1]), 2),
            "spy_cagr_pct": round(100 * float(s.iloc[-1] ** (1 / yrs) - 1), 1), "spy_max_drawdown_pct": dd(s)}


def portfolio(T, px, spy, start):
    D = sorted([t for t in T if t["st"] == "done"], key=lambda t: t["in"])
    days = spy.index[spy.index >= pd.Timestamp(start)]
    cash, pos, k, eq, exposure = 1.0, [], 0, [], []
    for day in days:
        keep = []
        for p in pos:
            if p["out"] <= day:
                cash += p["u"] * p["pout"] * (1 - p["c"])
            else:
                keep.append(p)
        pos = keep
        while k < len(D) and D[k]["in"] <= day:
            t = D[k]
            k += 1
            if t["in"] != day or len(pos) >= SLOTS:
                continue
            val = cash + sum(p["u"] * px[p["t"]].Close.asof(day) for p in pos)
            alloc = min(cash, val / SLOTS)
            if alloc <= 0:
                continue
            cash -= alloc
            pos.append({**t, "u": alloc * (1 - t["c"]) / t["pin"]})
        inv = sum(p["u"] * px[p["t"]].Close.asof(day) for p in pos)
        eq.append(cash + inv)
        exposure.append(inv / (cash + inv) if cash + inv > 0 else 0)
    st = curve_stats(pd.Series(eq, index=days), spy.Close)
    st["avg_invested_pct"] = round(100 * float(np.mean(exposure)), 1)
    return st


def sector_momentum(px, spy, trend):
    """Each month-end hold the 3 sector funds with the best 3-month return. Trend version: cash when SPY < 200-day average."""
    closes = pd.DataFrame({s: px[s].Close for s in SECTORS if s in px}).reindex(spy.index)
    opens = pd.DataFrame({s: px[s].Open for s in SECTORS if s in px}).reindex(spy.index)
    sma = spy.Close.rolling(200).mean()
    month_end = set(spy.index.to_series().groupby(spy.index.to_period("M")).max())
    cash, hold, eq, cost = 1.0, {}, [], 0.0005
    pending = None
    for i, day in enumerate(spy.index):
        if pending is not None:
            val = cash + sum(u * opens.at[day, s] for s, u in hold.items())
            cash = val * (1 - cost) if hold else val
            hold = {}
            if pending:
                each = cash / len(pending)
                hold = {s: each * (1 - cost) / opens.at[day, s] for s in pending}
                cash = 0.0
            pending = None
        eq.append(cash + sum(u * closes.at[day, s] for s, u in hold.items()))
        if day in month_end and i >= 200:
            mom = (closes.iloc[i] / closes.iloc[i - 63] - 1).dropna()
            pick = list(mom.sort_values(ascending=False).index[:3])
            if trend and spy.Close.iloc[i] < sma.iloc[i]:
                pick = []
            pending = pick
    c = pd.Series(eq, index=spy.index)
    c = c[c.index >= spy.index[201]]
    return curve_stats(c, spy.Close)


def main():
    t0 = time.time()
    ins = insider_purchases()
    ex = ins[ins["exec"]]
    rows_exec = list(zip(ex.t, ex.filed, ex.RPTOWNERCIK.fillna(""), ex.value))
    con = congress_purchases()
    strategies = {
        "insider_cluster3": ("3+ company directors or officers buy the same stock within 30 days", cluster_events(rows_exec, 3, 30)),
        "insider_cluster2_25k": ("2+ directors or officers each buy $25k+ within 30 days", cluster_events(rows_exec, 2, 30, min_value=25000)),
        "insider_big_250k": ("one director or officer buys $250k+ on the open market", cluster_events(rows_exec, 1, 1, min_value=250000)),
        "congress_cluster3": ("3+ members of Congress disclose buying the same stock within 60 days", cluster_events(con, 3, 60)),
        "congress_cluster2": ("2+ members of Congress disclose buying the same stock within 60 days", cluster_events(con, 2, 60)),
        "congress_big_100k": ("one member of Congress discloses a $100k+ purchase", cluster_events(con, 1, 1, min_value=100001)),
    }
    for k, (_, e) in strategies.items():
        log("events", k, len(e))
    need = {t for _, (_, e) in strategies.items() for t, _ in e} | set(SECTORS) | {"SPY"}
    px = prices(need)
    spy = px["SPY"]
    result = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "rules": __doc__.split("Known bias")[0].strip(), "known_bias": "Delisted or renamed stocks have no Yahoo history and are left out (see counts.nodata).",
              "strategies": {}}
    for k, (desc, ev) in strategies.items():
        res = {"description": desc, "signals": len(ev), "holds": {}}
        for H in HOLDS:
            T = trades(ev, H, px, spy)
            res["holds"][str(H)] = {"counts": dict(collections.Counter(t["st"] for t in T)),
                                    "all": summary(T), "2014_2019": summary(T, 2014, 2019), "2020_2026": summary(T, 2020, 2026),
                                    "by_year": by_year(T)}
            if H == 60:
                res["portfolio_60d"] = portfolio(T, px, spy, "2014-03-01")
        result["strategies"][k] = res
        log(k, json.dumps(res["holds"]["60"]["all"]), json.dumps(res.get("portfolio_60d")))
    result["sector_momentum"] = {"top3_monthly": sector_momentum(px, spy, False), "top3_monthly_trend_filter": sector_momentum(px, spy, True)}
    log("sector momentum", json.dumps(result["sector_momentum"]))
    result["runtime_min"] = round((time.time() - t0) / 60, 1)
    result["log"] = LOG[-80:]
    (OUT / "backtest.json").write_text(json.dumps(result, indent=1, default=str))
    log("done in", result["runtime_min"], "min")


if __name__ == "__main__":
    main()
