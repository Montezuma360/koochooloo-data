"""Koochooloo research lab, round 3: market timing, risk controls, quality and survivorship.

Four questions, each with its pass rule written down before the run:
1. MARKET TIMING (S&P 500 or cash). Does stepping out of SPY into Treasury bills when a stress gauge flashes
   beat simply holding SPY? Gauges: credit spread (Moody's Baa minus 10-year Treasury, FRED BAA10Y), VIX above 30,
   inverted yield curve (10y minus 3m), Chicago Fed financial conditions (NFCI) above 0, SPY below its 200-day
   average, hedge funds crowded long S&P futures (CFTC), market-wide short selling (FINRA), and a combined rule.
   Data lags: daily FRED series 1 business day, NFCI 5, CFTC 3. Cash earns the 3-month T-bill rate. 0.05% per switch.
   PASS: higher Sharpe ratio AND a shallower worst drawdown than SPY, in BOTH the design (2007-2018) and holdout
   (2019-2026) periods. The FINRA rule only has data from Aug 2018: design 2018-2022, holdout 2023-2026.
2. RISK LAYER on the two best insider strategies (round 2): does the paper engine's stop-loss (2.5x average
   daily range, 6-25%) and volatility sizing (0.5x-1.5x) help? Same pass rule as round 2, plus drawdown reported.
3. OVERLAYS: skip insider signals while the combined market gauge says risk-off; keep only companies whose latest
   annual report (assumed public 105 days after the year end) showed profit and positive operating cash flow,
   or operating cash flow above profit (earnings backed by cash). Same pass rule as round 2.
4. SURVIVORSHIP: about half of older insider signals have no Yahoo prices (delisted). The SEC fails-to-deliver
   archive records a price for many of them; use it to estimate how those missing trades actually did.
Writes data/research/round3.json and data/research/round3_regime.csv.gz."""
import collections, concurrent.futures as cf, datetime as dt, gzip, io, json, math, os, pathlib, re, sys, time, urllib.error, urllib.parse, zipfile
import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import round2 as R2  # noqa: E402  (reuses SEC loading, cached prices, trades, summary)

OUT, CACHE, log, get = R2.OUT, R2.CACHE, R2.log, R2.get
T_DESIGN, T_HOLD = ("2007-01-01", "2018-12-31"), ("2019-01-01", "2026-12-31")
S_DESIGN, S_HOLD = ("2018-09-01", "2022-12-31"), ("2023-01-01", "2026-12-31")
SWITCH_COST = 0.0005
PASS_TIMING = "Sharpe above SPY's AND worst drawdown shallower than SPY's, in both the design and the holdout period."


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------- data ----------------
def fred(sid):
    f = CACHE / ("fred_%s.pkl" % sid)
    try:
        txt = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + sid).decode()
        df = pd.read_csv(io.StringIO(txt))
        s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values, index=pd.to_datetime(df.iloc[:, 0])).dropna()
        s.to_pickle(f)
    except Exception as e:
        log("FRED", sid, "download failed, using cache:", repr(e)[:80])
        s = pd.read_pickle(f)
    log("FRED", sid, len(s), "points from", s.index[0].date())
    return s


def spy_long():
    f = CACHE / "spy_long.pkl"
    for i in range(6):
        try:
            d = yf.download("SPY", start="1998-01-01", interval="1d", auto_adjust=True, progress=False)
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            d = d[["Open", "Close", "Volume"]].dropna()
            d.index = pd.DatetimeIndex(d.index).tz_localize(None)
            if len(d) > 5000:
                d.to_pickle(f)
                return d
        except Exception as e:
            log("SPY download error", repr(e)[:80])
        time.sleep(60)
    return pd.read_pickle(f)


def cot_sp500():
    """Hedge funds' (leveraged money) net E-mini S&P 500 position as % of open interest, weekly since 2006."""
    rows, off = [], 0
    while True:
        q = urllib.parse.urlencode({"$where": "cftc_contract_market_code='13874A'", "$order": "report_date_as_yyyy_mm_dd ASC",
                                    "$limit": 5000, "$offset": off})
        part = json.loads(get("https://publicreporting.cftc.gov/resource/gpe5-46if.json?" + q))
        rows += part
        if len(part) < 5000:
            break
        off += 5000
    out = {}
    for r in rows:
        try:
            oi = float(r["open_interest_all"])
            out[pd.Timestamp(r["report_date_as_yyyy_mm_dd"][:10])] = 100 * (float(r["lev_money_positions_long"]) - float(r["lev_money_positions_short"])) / oi
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pass
    s = pd.Series(out).sort_index()
    log("CFTC E-mini S&P weeks:", len(s), "from", s.index[0].date() if len(s) else None)
    return s


def finra_market_short():
    """Market-wide share of volume that was short selling, per day, from FINRA's consolidated NMS files."""
    f = CACHE / "finra_market.pkl"
    try:
        have = pd.read_pickle(f)
    except Exception:
        have = {}
    days = [d for d in pd.bdate_range("2018-08-01", dt.date.today() - dt.timedelta(days=1)) if d not in have]

    def one(d):
        try:
            raw = get("https://cdn.finra.org/equity/regsho/daily/CNMSshvol%s.txt" % d.strftime("%Y%m%d"), tries=2).decode("latin-1")
        except urllib.error.HTTPError:
            return d, None  # holiday or no file: FINRA answered, remember it
        except Exception:
            return d, "retry"
        s = t = 0
        for line in raw.splitlines()[1:]:
            p = line.split("|")
            if len(p) >= 5:
                try:
                    s += float(p[2])
                    t += float(p[4])
                except ValueError:
                    pass
        return d, (s / t if t else None)
    with cf.ThreadPoolExecutor(8) as ex:
        for d, v in ex.map(one, days):
            if v != "retry":
                have[d] = v
    pd.to_pickle(have, f)
    s = pd.Series({k: v for k, v in have.items() if v is not None}).sort_index()
    log("FINRA market short ratio days:", len(s), "from", s.index[0].date() if len(s) else None)
    return s


def known(s, lag_bdays, days):
    """Value of a series as it was known at each trading day's close, given its publication lag."""
    s = s.copy()
    s.index = s.index + pd.offsets.BDay(lag_bdays)
    s = s[~s.index.duplicated(keep="last")]
    return s.reindex(s.index.union(days)).ffill().reindex(days)


# ---------------- 1. market timing ----------------
def gauges(spy):
    days = spy.index
    baa = known(fred("BAA10Y"), 1, days)
    vix = known(fred("VIXCLS"), 1, days)
    curve = known(fred("T10Y3M"), 1, days)
    nfci = known(fred("NFCI"), 5, days)
    tbill = known(fred("DGS3MO"), 1, days).fillna(0)
    sma200 = spy.Close.rolling(200).mean()
    cot = cot_sp500()
    cot_pct = cot.rolling(156, min_periods=156).apply(lambda w: (w <= w[-1]).mean() * 100, raw=True)
    cot_k = known(cot_pct, 3, days)
    sv = finra_market_short()
    sv20 = sv.rolling(20, min_periods=15).mean()
    sv_p90 = sv20.rolling(252, min_periods=200).quantile(0.90)
    sv_flag = known((sv20 > sv_p90).astype(float).where(sv_p90.notna()), 1, days)
    G = pd.DataFrame({
        "credit": baa > baa.rolling(200, min_periods=150).mean() * 1.10,
        "vix": vix > 30,
        "curve": curve < 0,
        "nfci": nfci > 0,
        "trend": spy.Close < sma200,
        "cot_crowded": cot_k >= 90,
        "short_ratio": sv_flag == 1,
    }, index=days).fillna(False)
    G["combined"] = G[["credit", "vix", "nfci", "trend"]].sum(axis=1) >= 2
    return G, tbill, {"baa": baa, "vix": vix, "curve": curve, "nfci": nfci, "cot_pct": cot_k, "short20": known(sv20, 1, days)}


def stats(r, cash):
    if len(r) < 60:
        return None
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    ex = r - cash
    return {"cagr_pct": round(100 * (eq.iloc[-1] ** (1 / yrs) - 1), 2), "vol_pct": round(100 * r.std() * math.sqrt(252), 2),
            "sharpe": round(float(ex.mean() / ex.std() * math.sqrt(252)), 3) if ex.std() > 0 else None,
            "max_drawdown_pct": round(100 * float((eq / eq.cummax() - 1).min()), 1)}


def timing(spy, G, tbill):
    ret = spy.Close.pct_change().fillna(0)
    cash = (tbill / 100 / 252).shift(1).fillna(0)
    res = {}
    for g in G.columns:
        off = G[g].shift(1).fillna(False).astype(bool)  # decided at yesterday's close, held today
        switch = off.ne(off.shift(1)).astype(float)
        switch.iloc[0] = 0
        r = np.where(off, cash, ret) - switch * SWITCH_COST
        r = pd.Series(r, index=ret.index)
        per = (S_DESIGN, S_HOLD) if g == "short_ratio" else (T_DESIGN, T_HOLD)
        out = {}
        for name, (a, b) in zip(("design", "holdout"), per):
            m = (r.index >= a) & (r.index <= b)
            st, base = stats(r[m], cash[m]), stats(ret[m], cash[m])
            if st is None:
                continue
            st["time_out_of_market_pct"] = round(100 * float(off[m].mean()), 1)
            st["switches_per_year"] = round(float(switch[m].sum() / (m.sum() / 252)), 1)
            out[name] = {"strategy": st, "spy": base}
        ok = all(k in out and (out[k]["strategy"]["sharpe"] or -9) > (out[k]["spy"]["sharpe"] or -9)
                 and out[k]["strategy"]["max_drawdown_pct"] > out[k]["spy"]["max_drawdown_pct"] for k in ("design", "holdout"))
        d_ok = "design" in out and (out["design"]["strategy"]["sharpe"] or -9) > (out["design"]["spy"]["sharpe"] or -9) \
            and out["design"]["strategy"]["max_drawdown_pct"] > out["design"]["spy"]["max_drawdown_pct"]
        out["verdict"] = "PASS" if ok else ("design only" if d_ok else "fail")
        res[g] = out
        log("timing", g, out["verdict"], json.dumps({k: v for k, v in out.items() if k != "verdict"}))
    return res


# ---------------- 2+3. insider strategies with risk layer and overlays ----------------
def quarter3(q, url, fresh):
    """Round 2's SEC loader plus the issuer's CIK (needed to join company financials)."""
    f = CACHE / ("sec3_%s.pkl" % q)
    if f.exists() and not fresh:
        return pd.read_pickle(f)
    z = zipfile.ZipFile(io.BytesIO(get(url)))
    nd = R2.read(z, "NONDERIV_TRANS.tsv", {R2.ACC, "TRANS_CODE", "TRANS_SHARES", "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD", "SHRS_OWND_FOLWNG_TRANS"})
    nd = nd[(nd.TRANS_CODE == "P") & (nd.TRANS_ACQUIRED_DISP_CD == "A")].copy()
    for c in ("TRANS_SHARES", "TRANS_PRICEPERSHARE", "SHRS_OWND_FOLWNG_TRANS"):
        nd[c] = pd.to_numeric(nd[c], errors="coerce") if c in nd.columns else np.nan
    nd["value"] = nd.TRANS_SHARES * nd.TRANS_PRICEPERSHARE
    g = nd.groupby(R2.ACC).agg(value=("value", "sum"), shares=("TRANS_SHARES", "sum"), owned_after=("SHRS_OWND_FOLWNG_TRANS", "max")).reset_index()
    keep = set(g[R2.ACC])
    sub = R2.read(z, "SUBMISSION.tsv", {R2.ACC, "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERTRADINGSYMBOL", "AFF10B5ONE"})
    sub = sub[sub[R2.ACC].isin(keep) & (sub.DOCUMENT_TYPE == "4")]
    own = R2.read(z, "REPORTINGOWNER.tsv", {R2.ACC, "RPTOWNERCIK", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"})
    own = own[own[R2.ACC].isin(keep)].drop_duplicates(R2.ACC)
    m = sub.merge(g, on=R2.ACC).merge(own, on=R2.ACC, how="left")
    m.to_pickle(f)
    return m


def fundamentals():
    """Annual net income and operating cash flow for every SEC filer, from the XBRL frames API (one file per year)."""
    f = CACHE / "fundamentals.pkl"
    try:
        F = pd.read_pickle(f)
        if F.get("saved") == str(dt.date.today()):
            return F["data"]
    except Exception:
        pass
    concepts = {"ni": ["NetIncomeLoss", "ProfitLoss"],
                "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]}
    data = collections.defaultdict(dict)  # (cik, end) -> {ni, ocf}
    jobs = [(k, c, y) for k, cs in concepts.items() for c in cs for y in range(2010, dt.date.today().year)]

    def one(job):
        k, c, y = job
        try:
            j = json.loads(get("https://data.sec.gov/api/xbrl/frames/us-gaap/%s/USD/CY%d.json" % (c, y)))
            return job, j.get("data", [])
        except Exception as e:
            return job, repr(e)[:80]
    with cf.ThreadPoolExecutor(4) as ex:
        for (k, c, y), rows in ex.map(one, jobs):
            if isinstance(rows, str):
                log("frames", c, y, "failed", rows)
                continue
            primary = c == concepts[k][0]
            for r in rows:
                key = (int(r["cik"]), r["end"])
                if primary or k not in data[key]:
                    data[key][k] = float(r["val"])
            time.sleep(0.2)
    by = collections.defaultdict(list)
    for (cik, end), v in data.items():
        if "ni" in v and "ocf" in v:
            by[cik].append((pd.Timestamp(end), v["ni"], v["ocf"]))
    for v in by.values():
        v.sort()
    pd.to_pickle({"saved": str(dt.date.today()), "data": dict(by)}, f)
    log("fundamentals: companies with profit and cash-flow history:", len(by))
    return dict(by)


def quality_at(F, cik, d):
    """Latest annual figures public by date d (year end + 105 days) and no older than 18 months."""
    best = None
    for end, ni, ocf in F.get(cik, []):
        if end + pd.Timedelta(days=105) <= d and end >= d - pd.Timedelta(days=550):
            best = (ni, ocf)
    return best


def risk_layer(T, px, spy, stop=True, vol=True):
    """Apply the paper engine's stop-loss and volatility sizing to finished trades.
    Prices here are open/close only, so the average daily range is estimated as 1.6x the average close-to-close move."""
    out = []
    for t in T:
        if t["st"] != "done":
            out.append(t)
            continue
        s = px[t["t"]]
        i = int(s.index.searchsorted(t["in"]))
        h = s.Close.iloc[max(0, i - 21):i]
        rets = h.pct_change().dropna()
        if len(rets) >= 10 and rets.std() > 0:
            w = clamp(0.02 / rets.std(), 0.5, 1.5)
            sp = clamp(2.5 * 1.6 * h.diff().abs().tail(14).mean() / h.iloc[-1], 0.06, 0.25)
        else:
            w, sp = 1.0, 0.15
        t = dict(t, w=w if vol else 1.0)
        if stop:
            j_end = int(s.index.searchsorted(t["out"]))
            seg = s.Close.iloc[i:j_end + 1].values
            hit = np.nonzero(seg <= t["pin"] * (1 - sp))[0]
            if len(hit):
                j = i + int(hit[0])
                pout = s.Close.iloc[j]
                r = pout / t["pin"] * (1 - t["c"]) / (1 + t["c"]) - 1
                a = spy.index.searchsorted(s.index[i])
                b = min(spy.index.searchsorted(s.index[j]), len(spy) - 1)
                m = spy.Close.iloc[b] / spy.Open.iloc[a] - 1
                t.update(out=s.index[j], pout=pout, r=r, spy=m, x=r - m, stopped=True)
        out.append(t)
    return out


def portfolio3(T, px, spy, start, end):
    """Round 2's 50-slot portfolio, with optional per-trade size multiplier `w`, plus Sharpe ratios."""
    D = sorted([t for t in T if t["st"] == "done" and pd.Timestamp(start) <= t["in"] <= pd.Timestamp(end)], key=lambda t: t["in"])
    days = spy.index[(spy.index >= pd.Timestamp(start)) & (spy.index <= pd.Timestamp(end))]
    cash, pos, k, eq = 1.0, [], 0, []
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
            if t["in"] != day or len(pos) >= R2.SLOTS:
                continue
            val = cash + sum(p["u"] * px[p["t"]].Close.asof(day) for p in pos)
            alloc = min(cash, val / R2.SLOTS * t.get("w", 1.0))
            if alloc > 0:
                cash -= alloc
                pos.append({**t, "u": alloc * (1 - t["c"]) / t["pin"]})
        eq.append(cash + sum(p["u"] * px[p["t"]].Close.asof(day) for p in pos))
    c = pd.Series(eq, index=days)
    s = spy.Close.reindex(days)
    yrs = max((days[-1] - days[0]).days / 365.25, 0.1)
    dd = lambda z: round(float(100 * (z / z.cummax() - 1).min()), 1)
    sh = lambda z: round(float(z.pct_change().mean() / z.pct_change().std() * math.sqrt(252)), 3) if z.pct_change().std() > 0 else None
    return {"cagr_pct": round(100 * float(c.iloc[-1] ** (1 / yrs) - 1), 1), "spy_cagr_pct": round(100 * float((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1), 1),
            "max_drawdown_pct": dd(c), "spy_max_drawdown_pct": dd(s / s.iloc[0]), "sharpe": sh(c), "spy_sharpe": sh(s)}


def test(name, desc, ev, px, spy, cond=None, risk=None):
    T = R2.trades(ev, 60, px, spy, cond)
    if risk:
        T = risk_layer(T, px, spy, **risk)
    des, hol = R2.summary(T, *R2.DESIGN), R2.summary(T, *R2.HOLDOUT)
    pd_, ph = portfolio3(T, px, spy, "2014-03-01", "2022-12-31"), portfolio3(T, px, spy, "2023-01-01", "2026-12-31")
    v = R2.verdict(des, hol, pd_, ph)
    stopped = sum(1 for t in T if t.get("stopped"))
    log(name, v, json.dumps(des), json.dumps(hol), json.dumps(pd_), json.dumps(ph))
    return {"description": desc, "signals": len(ev), "counts": dict(collections.Counter(t["st"] for t in T)), "stopped": stopped,
            "design": des, "holdout": hol, "portfolio_design": pd_, "portfolio_holdout": ph, "verdict_60d": v}, T


# ---------------- 4. survivorship from fails-to-deliver prices ----------------
def ftd_prices(symbols):
    """Prices the SEC fails-to-deliver files recorded for the given symbols (2014 onwards)."""
    f = CACHE / "ftd_prices.pkl"
    try:
        have = pd.read_pickle(f)
    except Exception:
        have = {"periods": {}, "rows": []}
    page = get("https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data").decode("utf-8", "ignore")
    links = {m.group(2): m.group(1) for m in re.finditer(r'href="([^"]*?cnsfails(\d{6}[ab])\.zip)"', page)}
    todo = [p for p in sorted(links) if p >= "201401a" and p not in have["periods"]]
    log("FTD files: cached", len(have["periods"]), "| to fetch", len(todo))
    want = set(symbols)

    def one(p):
        url = links[p] if links[p].startswith("http") else "https://www.sec.gov" + links[p]
        try:
            z = zipfile.ZipFile(io.BytesIO(get(url)))
            txt = z.open(z.namelist()[0]).read().decode("latin-1")
        except Exception as e:
            return p, None, repr(e)[:80]
        rows = []
        for line in txt.splitlines()[1:]:
            q = line.split("|")
            if len(q) >= 6 and q[2] in want:
                try:
                    rows.append((q[2], pd.Timestamp(q[0]), float(q[5])))
                except Exception:
                    pass
        return p, rows, None
    with cf.ThreadPoolExecutor(4) as ex:
        for p, rows, err in ex.map(one, todo):
            if rows is None:
                log("FTD", p, "failed", err)
                continue
            have["periods"][p] = len(rows)
            have["rows"] += rows
    pd.to_pickle(have, f)
    by = collections.defaultdict(list)
    for s, d, pr in have["rows"]:
        if pr > 0 and s in want:
            by[s].append((d, pr))
    return {s: pd.Series(dict(v)).sort_index() for s, v in by.items()}, len(have["periods"])


def estimate_missing(name, ev, px, spy, F_prices):
    """For signals with no Yahoo prices, estimate the 60-day trade from fails-to-deliver prices."""
    res = collections.Counter()
    xs = []
    for t, d in ev:
        if t in px:
            continue
        s = F_prices.get(t)
        if s is None or not len(s):
            res["no_ftd_record"] += 1
            continue
        a = s[(s.index > d) & (s.index <= d + pd.Timedelta(days=10))]
        if not len(a):
            res["no_entry_price"] += 1
            continue
        tgt = a.index[0] + pd.Timedelta(days=87)  # about 60 trading days
        b = s[(s.index >= tgt - pd.Timedelta(days=10)) & (s.index <= tgt + pd.Timedelta(days=10))]
        if not len(b):
            res["vanished_or_no_exit_price" if s.index[-1] < tgt else "no_exit_price"] += 1
            continue
        pin, pout = float(a.iloc[0]), float(b.iloc[(abs(b.index - tgt)).argmin()])
        if pin < 1:
            res["penny"] += 1
            continue
        c = 0.0015 if pin >= 10 else 0.004
        r = pout / pin * (1 - c) / (1 + c) - 1
        m = spy.Close.asof(b.index[(abs(b.index - tgt)).argmin()]) / spy.Close.asof(a.index[0]) - 1
        xs.append((d.year, r - m))
        res["estimated"] += 1
    des = [x for y, x in xs if R2.DESIGN[0] <= y <= R2.DESIGN[1]]
    hol = [x for y, x in xs if R2.HOLDOUT[0] <= y <= R2.HOLDOUT[1]]
    f = lambda v: {"n": len(v), "avg_excess_pct": round(100 * float(np.mean(v)), 2) if v else None,
                   "median_excess_pct": round(100 * float(np.median(v)), 2) if v else None,
                   "beat_spy_pct": round(100 * float(np.mean([x > 0 for x in v])), 1) if v else None}
    out = {"missing_signals": sum(res.values()), "outcome": dict(res), "design": f(des), "holdout": f(hol)}
    log("survivorship", name, json.dumps(out))
    return out


# ---------------- main ----------------
def save(res, t0):
    res["runtime_min"] = round((time.time() - t0) / 60, 1)
    res["log"] = R2.LOG[-100:]
    (OUT / "round3.json").write_text(json.dumps(res, indent=1, default=str))


def main():
    t0 = time.time()
    res = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "rules": __doc__.strip(),
           "pass_rule_timing": PASS_TIMING, "pass_rule_insider": R2.PASS_RULE, "errors": {}}
    G = None

    # 1. market timing
    try:
        spyL = spy_long()
        log("SPY long history from", spyL.index[0].date(), "rows", len(spyL))
        G, tbill, raw = gauges(spyL)
        res["timing"] = timing(spyL, G, tbill)
        reg = G.copy()
        for k, v in raw.items():
            reg[k] = v.round(3)
        reg["spy_close"] = spyL.Close.round(2)
        with gzip.open(OUT / "round3_regime.csv.gz", "wt") as fh:
            reg[reg.index >= "2005-01-01"].to_csv(fh, index_label="date")
        res["today"] = {k: bool(G[k].iloc[-1]) for k in G.columns}
        res["today"]["date"] = str(G.index[-1].date())
    except Exception as e:
        res["errors"]["timing"] = repr(e)[:300]
        log("TIMING FAILED", repr(e)[:300])
    save(res, t0)

    # 2+3. insider strategies with the risk layer and overlays
    R2.quarter = quarter3
    df = R2.insider_purchases()
    df["cik"] = pd.to_numeric(df["ISSUERCIK"], errors="coerce") if "ISSUERCIK" in df.columns else np.nan
    cik_of = {(t, d): c for t, d, c in zip(df.t, df.filed, df.cik)}
    big25 = df[df.value >= 25_000]
    ev = R2.cluster_events(big25, 2)
    px = R2.prices({t for t, _ in ev})
    spy = px["SPY"]
    try:
        F = fundamentals()
    except Exception as e:
        F = {}
        res["errors"]["fundamentals"] = repr(e)[:300]
        log("FUNDAMENTALS FAILED", repr(e)[:300])
    combo = G["combined"] if G is not None else None

    def risk_off(d):
        if combo is None:
            return False
        v = combo.asof(pd.Timestamp(d))
        return bool(v) if v == v else False

    def fin(t, d):
        c = cik_of.get((t, d))
        return quality_at(F, int(c), pd.Timestamp(d)) if c is not None and c == c else None

    def q_ok(t, d, kind):
        q = fin(t, d)
        if q is None:
            return False
        ni, ocf = q
        return (ni > 0 and ocf > 0) if kind == "profitable" else ocf > ni

    res["fundamentals_coverage_pct"] = round(100 * sum(1 for t, d in ev if fin(t, d)) / max(len(ev), 1), 1)
    log("fundamentals coverage of insider signals:", res["fundamentals_coverage_pct"], "%")
    small = lambda c: c["dvol"] <= 20e6
    base = {"cluster2_25k": ("2+ directors or officers each buy $25k+ within 30 days", None),
            "cluster2_smallcap": ("same, in a stock trading $1M-$20M a day", small)}
    S = {}
    for k, (desc, cond) in base.items():
        S[k] = (desc, ev, cond, None)
        S[k + "+stop"] = (desc + "; with the stop-loss", ev, cond, {"stop": True, "vol": False})
        S[k + "+volsize"] = (desc + "; with volatility sizing", ev, cond, {"stop": False, "vol": True})
        S[k + "+stop+volsize"] = (desc + "; with stop-loss and volatility sizing (the paper engine's rules)", ev, cond, {"stop": True, "vol": True})
        S[k + "+riskoff_skip"] = (desc + "; skipped while the combined market gauge is risk-off", [(t, d) for t, d in ev if not risk_off(d)], cond, None)
        S[k + "+profitable"] = (desc + "; only profitable companies with positive operating cash flow", [(t, d) for t, d in ev if q_ok(t, d, "profitable")], cond, None)
        S[k + "+cash_backed"] = (desc + "; only companies whose operating cash flow exceeds profit", [(t, d) for t, d in ev if q_ok(t, d, "cash")], cond, None)
        S[k + "+riskoff_skip+profitable"] = (desc + "; risk-off skip and profitable only",
                                             [(t, d) for t, d in ev if not risk_off(d) and q_ok(t, d, "profitable")], cond, None)
    res["insider"] = {}
    for k, (desc, e, cond, risk) in S.items():
        try:
            res["insider"][k], _ = test(k, desc, e, px, spy, cond, risk)
        except Exception as ex:
            res["errors"]["insider_" + k] = repr(ex)[:300]
            log("TEST FAILED", k, repr(ex)[:300])
    res["tests_run"] = len(S) + len(res.get("timing", {}))
    res["multiple_testing_note"] = ("%d variants were tested. With that many, one or two can pass by luck; a real edge should pass "
                                    "in the holdout AND make sense economically." % res["tests_run"])
    save(res, t0)

    # 4. survivorship
    try:
        miss_syms = {t for t, _ in ev if t not in px}
        Fp, nfiles = ftd_prices(miss_syms)
        res["survivorship"] = {"ftd_files_used": nfiles, "symbols_without_yahoo_prices": len(miss_syms), "symbols_found_in_ftd": len(Fp),
                               "cluster2_25k": estimate_missing("cluster2_25k", ev, px, spy, Fp)}
    except Exception as e:
        res["errors"]["survivorship"] = repr(e)[:300]
        log("SURVIVORSHIP FAILED", repr(e)[:300])
    save(res, t0)
    log("done in", res["runtime_min"], "min")


if __name__ == "__main__":
    main()
