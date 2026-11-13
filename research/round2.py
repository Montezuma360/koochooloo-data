"""Koochooloo research lab, round 2: better insider data, tests written down before the run,
and an untouched holdout period.

New versus round 1:
- Richer SEC data: the insider's job title (CEO/CFO), how much the purchase grows their holding,
  and whether it was a pre-planned 10b5-1 trade (planned trades are excluded).
- Price context known at the time of the signal: distance from the 52-week high and daily traded value.
- Portfolios hold 50 slots, so rare big winners are not missed.
- Design period 2014-2022, holdout 2023-2026. A strategy passes only if it meets PASS_RULE in both.
Same trading rules as round 1: buy at the next open after the filing, costs 0.15% per side (0.40% under $10),
skip stocks under $1 or trading under $1M a day, sell at the close after the holding period.
Writes data/research/round2.json."""
import io, json, os, re, time, zipfile, collections, datetime as dt, pathlib, urllib.request
import numpy as np
import pandas as pd
import yfinance as yf

OUT = pathlib.Path("data/research")
OUT.mkdir(parents=True, exist_ok=True)
CACHE = pathlib.Path(".cache")
CACHE.mkdir(exist_ok=True)
UA = {"User-Agent": os.environ.get("SEC_CONTACT") or "KoochoolooResearch github.com/Montezuma360/koochooloo-data"}
TICK = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
SLOTS = 50
DESIGN, HOLDOUT = (2014, 2022), (2023, 2026)
PASS_RULE = ("Design 2014-2022: average excess return per trade above 0 with t-stat of 2 or more, AND the 50-slot portfolio "
             "beats SPY. Holdout 2023-2026: average excess above 0 AND the portfolio beats SPY.")
ACC = "ACCESSION_NUMBER"
LOG = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    LOG.append(s)


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(4 * (i + 1))


def read(z, name, want):
    return pd.read_csv(z.open(name), sep="\t", dtype=str, on_bad_lines="skip", usecols=lambda c: c in want)


def quarter(q, url, fresh):
    f = CACHE / ("sec_%s.pkl" % q)
    if f.exists() and not fresh:
        return pd.read_pickle(f)
    z = zipfile.ZipFile(io.BytesIO(get(url)))
    nd = read(z, "NONDERIV_TRANS.tsv", {ACC, "TRANS_CODE", "TRANS_SHARES", "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD", "SHRS_OWND_FOLWNG_TRANS"})
    nd = nd[(nd.TRANS_CODE == "P") & (nd.TRANS_ACQUIRED_DISP_CD == "A")].copy()
    for c in ("TRANS_SHARES", "TRANS_PRICEPERSHARE", "SHRS_OWND_FOLWNG_TRANS"):
        nd[c] = pd.to_numeric(nd[c], errors="coerce") if c in nd.columns else np.nan
    nd["value"] = nd.TRANS_SHARES * nd.TRANS_PRICEPERSHARE
    g = nd.groupby(ACC).agg(value=("value", "sum"), shares=("TRANS_SHARES", "sum"), owned_after=("SHRS_OWND_FOLWNG_TRANS", "max")).reset_index()
    keep = set(g[ACC])
    sub = read(z, "SUBMISSION.tsv", {ACC, "FILING_DATE", "DOCUMENT_TYPE", "ISSUERTRADINGSYMBOL", "AFF10B5ONE"})
    sub = sub[sub[ACC].isin(keep) & (sub.DOCUMENT_TYPE == "4")]
    own = read(z, "REPORTINGOWNER.tsv", {ACC, "RPTOWNERCIK", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"})
    own = own[own[ACC].isin(keep)].drop_duplicates(ACC)
    m = sub.merge(g, on=ACC).merge(own, on=ACC, how="left")
    m.to_pickle(f)
    return m


def insider_purchases():
    page = get("https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets").decode("utf-8", "ignore")
    links = {}
    for path, q in re.findall(r'href="([^"]*?((?:19|20)\d\dq\d)_form345\.zip)"', page):
        links[q] = path if path.startswith("http") else "https://www.sec.gov" + path
    qs = [q for q in sorted(links) if q >= "2014q1"]
    frames = []
    for n, q in enumerate(qs):
        try:
            frames.append(quarter(q, links[q], fresh=n >= len(qs) - 2))
        except Exception as e:
            log("insiders", q, "FAILED", repr(e)[:150])
    df = pd.concat(frames, ignore_index=True)
    for c in ("AFF10B5ONE", "RPTOWNER_TITLE", "RPTOWNER_RELATIONSHIP", "RPTOWNERCIK"):
        if c not in df.columns:
            df[c] = None
    df["filed"] = pd.to_datetime(df.FILING_DATE, format="%d-%b-%Y", errors="coerce")
    miss = df.filed.isna()
    if miss.any():
        df.loc[miss, "filed"] = pd.to_datetime(df.loc[miss, "FILING_DATE"], errors="coerce")
    df["t"] = df.ISSUERTRADINGSYMBOL.fillna("").str.upper().str.strip().str.replace("-", ".", regex=False)
    df = df[df.t.str.match(TICK) & df.filed.notna() & (df.value > 0)].copy()
    rel, title = df.RPTOWNER_RELATIONSHIP.fillna(""), df.RPTOWNER_TITLE.fillna("")
    df["exec"] = rel.str.contains("Director|Officer", case=False)
    df["ceo_cfo"] = title.str.contains(r"chief executive|chief financial|\bceo\b|\bcfo\b|president", case=False)
    df["plan"] = df.AFF10B5ONE.fillna("").astype(str).str.strip().str.lower().isin(["1", "true", "y", "yes"])
    before = df.owned_after - df.shares
    df["pct_inc"] = np.where(df.owned_after.isna(), np.nan, np.where(before > 0, df.shares / before.clip(lower=1), 10.0))
    df = df[df.exec & ~df.plan].sort_values("filed")
    log("insider purchases by directors/officers (not pre-planned):", len(df), "| with holding data:", int(df.pct_inc.notna().sum()),
        "| CEO/CFO:", int(df.ceo_cfo.sum()))
    return df


# ---------------- prices (cached between runs) ----------------
def _batch(chunk, start):
    ys = [c.replace(".", "-") for c in chunk]
    req = ys if "SPY" in ys else ys + ["SPY"]
    df = yf.download(req, start=start, interval="1d", auto_adjust=True, group_by="ticker", threads=True, progress=False)
    if not isinstance(df.columns, pd.MultiIndex):
        df.columns = pd.MultiIndex.from_product([[req[0]], df.columns])

    def frame(y):
        s = df[y][["Open", "Close", "Volume"]].dropna()
        s.index = pd.DatetimeIndex(s.index).tz_localize(None)
        return s
    try:
        ok = len(frame("SPY")) > 0
    except Exception:
        ok = False
    got, miss = {}, []
    for c, y in zip(chunk, ys):
        try:
            s = frame(y)
            (got.__setitem__(c, s) if len(s) else miss.append(c))
        except Exception:
            miss.append(c)
    return got, miss, ok


def prices(tickers, start="2013-01-01"):
    f = CACHE / "prices.pkl"
    try:
        cache = pd.read_pickle(f)
    except Exception:
        cache = {"px": {}, "missing": [], "saved": None}
    px, known_missing = cache["px"], set(cache["missing"])
    todo = [t for t in ["SPY"] + sorted(set(tickers) - {"SPY"}) if t not in px and t not in known_missing]
    log("prices: cached", len(px), "| known missing", len(known_missing), "| to fetch", len(todo))
    i, waits = 0, 0
    while i < len(todo):
        chunk = todo[i:i + 60]
        try:
            got, miss, ok = _batch(chunk, start)
        except Exception as e:
            log("price batch error", repr(e)[:100])
            got, miss, ok = {}, chunk, False
        if not ok and waits < 30:
            waits += 1
            log("rate limited at", i, "of", len(todo), "- pausing 3 minutes")
            time.sleep(180)
            continue
        px.update(got)
        known_missing.update(miss)
        i += len(chunk)
        time.sleep(8)
    pd.to_pickle({"px": px, "missing": sorted(known_missing), "saved": str(dt.date.today())}, f)
    if "SPY" not in px:
        raise SystemExit("No SPY prices: Yahoo blocked the download. Run again later.")
    return px


# ---------------- events ----------------
def single_events(df, refire=60):
    ev, last = [], {}
    for t, d in zip(df.t, df.filed):
        if t in last and (d - last[t]).days <= refire:
            continue
        last[t] = d
        ev.append((t, d))
    return ev


def cluster_events(df, n_needed, window=30, refire=60):
    ev, hist, last = [], collections.defaultdict(list), {}
    for t, d, who in zip(df.t, df.filed, df.RPTOWNERCIK.fillna("")):
        h = hist[t]
        h.append((d, who))
        while h and (d - h[0][0]).days > window:
            h.pop(0)
        if len({w for _, w in h}) >= n_needed and (t not in last or (d - last[t]).days > refire):
            last[t] = d
            ev.append((t, d))
    return ev


# ---------------- simulation ----------------
def context(s, d):
    """What was known at the close of the filing day: distance from the 52-week high and daily traded value."""
    k = s.index.searchsorted(pd.Timestamp(d), side="right") - 1
    if k < 60:
        return None
    c = s.Close.iloc[max(0, k - 251):k + 1]
    return {"dd52": float(c.iloc[-1] / c.max() - 1), "dvol": float((s.Close.iloc[k - 19:k + 1] * s.Volume.iloc[k - 19:k + 1]).mean())}


def trades(events, H, px, spy, cond=None):
    out = []
    for t, d in events:
        s = px.get(t)
        if s is None:
            out.append({"st": "nodata", "d": d})
            continue
        ctx = context(s, d)
        if cond is not None and (ctx is None or not cond(ctx)):
            out.append({"st": "filtered", "d": d})
            continue
        i = s.index.searchsorted(pd.Timestamp(d) + pd.Timedelta(days=1))
        if i >= len(s) or i < 1 or (s.index[i] - pd.Timestamp(d)).days > 7:
            out.append({"st": "noentry", "d": d})
            continue
        pin = s.Open.iloc[i]
        liq = (s.Close.iloc[max(0, i - 20):i] * s.Volume.iloc[max(0, i - 20):i]).mean()
        if not (pin >= 1 and liq >= 1e6):
            out.append({"st": "illiquid", "d": d})
            continue
        j = i + H - 1
        if j >= len(s):
            out.append({"st": "open", "d": d})
            continue
        pout = s.Close.iloc[j]
        if not pout > 0:
            out.append({"st": "bad", "d": d})
            continue
        c = 0.0015 if pin >= 10 else 0.004
        r = pout / pin * (1 - c) / (1 + c) - 1
        a = spy.index.searchsorted(s.index[i])
        b = min(spy.index.searchsorted(s.index[j]), len(spy) - 1)
        m = spy.Close.iloc[b] / spy.Open.iloc[a] - 1
        out.append({"st": "done", "t": t, "d": d, "in": s.index[i], "out": s.index[j], "pin": pin, "pout": pout, "c": c, "r": r, "spy": m, "x": r - m})
    return out


def summary(T, lo, hi):
    D = [t for t in T if t["st"] == "done" and lo <= t["d"].year <= hi]
    nod = sum(1 for t in T if t["st"] == "nodata" and lo <= t["d"].year <= hi)
    if len(D) < 10:
        return {"n": len(D), "nodata": nod}
    r = np.array([t["r"] for t in D])
    x = np.array([t["x"] for t in D])
    sd = x.std(ddof=1)
    return {"n": len(D), "nodata": nod, "win_pct": round(100 * (r > 0).mean(), 1), "beat_spy_pct": round(100 * (x > 0).mean(), 1),
            "avg_pct": round(100 * r.mean(), 2), "avg_excess_pct": round(100 * x.mean(), 2), "median_excess_pct": round(100 * np.median(x), 2),
            "t_stat": round(float(x.mean() / (sd / np.sqrt(len(x)))), 2) if sd > 0 else None,
            "stress_avg_excess_pct": round(100 * float((x.sum() - 0.10 * nod) / (len(x) + nod)), 2)}


def portfolio(T, px, spy, start, end):
    D = sorted([t for t in T if t["st"] == "done" and pd.Timestamp(start) <= t["in"] <= pd.Timestamp(end)], key=lambda t: t["in"])
    days = spy.index[(spy.index >= pd.Timestamp(start)) & (spy.index <= pd.Timestamp(end))]
    cash, pos, k, eq, expo = 1.0, [], 0, [], []
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
            if alloc > 0:
                cash -= alloc
                pos.append({**t, "u": alloc * (1 - t["c"]) / t["pin"]})
        inv = sum(p["u"] * px[p["t"]].Close.asof(day) for p in pos)
        eq.append(cash + inv)
        expo.append(inv / (cash + inv) if cash + inv > 0 else 0)
    c = pd.Series(eq, index=days)
    s = spy.Close.reindex(days)
    yrs = max((days[-1] - days[0]).days / 365.25, 0.1)
    dd = lambda z: round(float(100 * (z / z.cummax() - 1).min()), 1)
    return {"start": str(days[0].date()), "end": str(days[-1].date()), "cagr_pct": round(100 * float(c.iloc[-1] ** (1 / yrs) - 1), 1),
            "spy_cagr_pct": round(100 * float((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1), 1), "max_drawdown_pct": dd(c),
            "spy_max_drawdown_pct": dd(s / s.iloc[0]), "avg_invested_pct": round(100 * float(np.mean(expo)), 1)}


def verdict(des, hol, pd_, ph):
    d_ok = des.get("avg_excess_pct", -1) > 0 and (des.get("t_stat") or 0) >= 2 and pd_["cagr_pct"] > pd_["spy_cagr_pct"]
    h_ok = hol.get("avg_excess_pct", -1) > 0 and ph["cagr_pct"] > ph["spy_cagr_pct"]
    return "PASS" if d_ok and h_ok else ("design only" if d_ok else "fail")


def main():
    t0 = time.time()
    df = insider_purchases()
    big25 = df[df.value >= 25_000]
    conviction = df[(df.value >= 100_000) & (df.pct_inc >= 0.20)]
    ceo = df[df.ceo_cfo & (df.value >= 100_000)]
    buy50 = df[df.value >= 50_000]
    S = {
        "baseline_any_25k": ("any director or officer buys $25k+ (the reference point)", single_events(big25), None),
        "cluster2_25k_50slots": ("2+ directors or officers each buy $25k+ within 30 days", cluster_events(big25, 2), None),
        "conviction_buy": ("insider buys $100k+ that grows their holding by 20%+", single_events(conviction), None),
        "ceo_cfo_100k": ("the CEO, CFO or president buys $100k+", single_events(ceo), None),
        "contrarian_buy": ("insider buys $50k+ while the stock is 30%+ below its 52-week high", single_events(buy50), lambda c: c["dd52"] <= -0.30),
        "conviction_contrarian": ("conviction buy while the stock is 30%+ below its 52-week high", single_events(conviction), lambda c: c["dd52"] <= -0.30),
        "cluster2_smallcap": ("2+ insiders buy $25k+ in a stock trading $1M-$20M a day", cluster_events(big25, 2), lambda c: c["dvol"] <= 20e6),
    }
    for k, (_, ev, _) in S.items():
        log("signals", k, len(ev))
    px = prices({t for _, (_, ev, _) in S.items() for t, _ in ev})
    spy = px["SPY"]
    res = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "rules": __doc__.strip(), "pass_rule": PASS_RULE,
           "stress_note": "stress_avg_excess_pct assumes every trade with no price data (delisted stocks) lost 10% against SPY.",
           "strategies": {}}
    for k, (desc, ev, cond) in S.items():
        r = {"description": desc, "signals": len(ev), "holds": {}}
        for H in (20, 60, 120):
            T = trades(ev, H, px, spy, cond)
            r["holds"][str(H)] = {"counts": dict(collections.Counter(t["st"] for t in T)),
                                  "design": summary(T, *DESIGN), "holdout": summary(T, *HOLDOUT)}
            if H == 60:
                r["portfolio_design"] = portfolio(T, px, spy, "2014-03-01", "2022-12-31")
                r["portfolio_holdout"] = portfolio(T, px, spy, "2023-01-01", "2026-12-31")
        h = r["holds"]["60"]
        r["verdict_60d"] = verdict(h["design"], h["holdout"], r["portfolio_design"], r["portfolio_holdout"])
        res["strategies"][k] = r
        log(k, r["verdict_60d"], json.dumps(h["design"]), json.dumps(h["holdout"]), json.dumps(r["portfolio_design"]), json.dumps(r["portfolio_holdout"]))
    res["runtime_min"] = round((time.time() - t0) / 60, 1)
    res["log"] = LOG[-60:]
    (OUT / "round2.json").write_text(json.dumps(res, indent=1, default=str))
    log("done in", res["runtime_min"], "min")


if __name__ == "__main__":
    main()
