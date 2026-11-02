"""Koochooloo paper-trading engine: virtual money only. No broker, no real orders.

Each strategy starts with $100,000 and follows fixed rules:
- A signal counts from the first run that sees it, and only if it is at most 4 days old (no hindsight).
- Orders fill at the open of the next trading day after the signal; sells happen at the close once the holding period ends.
- Costs per side: 0.15% for stocks priced $10+, 0.40% below $10. Stocks under $1 or trading under $1M a day are skipped.
- 20 slots per strategy. Position size is scaled by volatility: calmer stocks get up to 1.5x a normal slot, wilder ones down to 0.5x.
- Every position has a stop-loss set at 2.5x its average daily range (between 6% and 25% below the entry price); it is sold at the close that breaks it.
- No new entries while a strategy is 15%+ below its own peak value, or while markets are stressed (VIX above 30, or high-yield credit spreads up 1+ point in a month).
- At most 4 positions in the same industry (SEC 2-digit SIC code) per strategy.
Writes data/paper/state.json, data/paper/summary.json and appends closed trades to data/paper/trades.csv."""
import collections, csv, datetime as dt, json, os, pathlib, re, time, urllib.request
import pandas as pd
import yfinance as yf

D = pathlib.Path("data")
P = D / "paper"
P.mkdir(parents=True, exist_ok=True)
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()
START_CASH = 100_000.0
SLOTS = 20
FRESH_DAYS = 4
TICK = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
EXEC = re.compile(r"director|officer|chief|president|ceo|cfo|coo|chair|vp|vice|counsel|treasurer|secretary", re.I)
STRATS = {
    "insider_cluster3": {"label": "3+ company insiders buy the same stock within 30 days", "hold": 60},
    "insider_big_250k": {"label": "A director or officer buys $250k+ on the open market", "hold": 60},
    "insider_cluster2_smallcap": {"label": "2+ insiders each buy $25k+ within 30 days in a smaller company ($1M-$20M traded a day)", "hold": 60, "max_liq": 20e6},
    "congress_cluster3": {"label": "3+ members of Congress disclose buying the same stock within 60 days", "hold": 60},
    "congress_big_100k": {"label": "A member of Congress discloses a $100k+ purchase", "hold": 60},
    "terminal_top_signals": {"label": "The terminal's own signal score reaches 8+", "hold": 20},
    "spy_benchmark": {"label": "Buy and hold the S&P 500 (SPY), the bar to beat", "hold": None},
}
TRADE_COLS = ["strategy", "symbol", "signal_date", "entry_date", "exit_date", "entry", "exit", "return_pct", "spy_return_pct", "excess_pct", "exit_reason", "size_x", "stop_pct", "industry", "note"]
DD_PAUSE = 0.15          # pause new entries 15% below the strategy's peak
MAX_PER_INDUSTRY = 4
SEC_UA = {"User-Agent": os.environ.get("SEC_CONTACT") or "KoochoolooResearch github.com/Montezuma360/koochooloo-data"}


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def risk_numbers(b, i):
    """Volatility size multiplier and stop distance from the 20 sessions before bar i (no look-ahead)."""
    h = b.iloc[max(0, i - 21):i]
    if len(h) < 10:
        return 1.0, 0.15
    vol = float(h.Close.pct_change().dropna().std())
    prev = h.Close.shift(1)
    tr = pd.concat([h.High - h.Low, (h.High - prev).abs(), (h.Low - prev).abs()], axis=1).max(axis=1).dropna().tail(14)
    atr = float(tr.mean()) if len(tr) else 0
    px = float(h.Close.iloc[-1])
    size = clamp(0.02 / vol, 0.5, 1.5) if vol > 0 else 1.0
    stop = clamp(2.5 * atr / px, 0.06, 0.25) if px > 0 and atr > 0 else 0.15
    return round(size, 3), round(stop, 4)


def market_stress():
    """True when the fear gauge or credit markets say stay out. Reads data/alt/macro.json (FRED)."""
    try:
        m = json.loads((D / "alt" / "macro.json").read_text())["series"]
    except Exception:
        return False, "no macro data"
    why = []
    v = m.get("VIXCLS", {})
    if (v.get("value") or 0) > 30:
        why.append("VIX %.1f above 30" % v["value"])
    h = m.get("BAMLH0A0HYM2", {})
    if (h.get("change_1m") or 0) > 1.0:
        why.append("high-yield spread up %.2f points in a month" % h["change_1m"])
    return bool(why), "; ".join(why) or "calm (VIX %s, high-yield spread %s)" % (v.get("value"), h.get("value"))


def sec_json(url):
    req = urllib.request.Request(url, headers=SEC_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def industries(st, syms):
    """2-digit SEC SIC industry for each symbol, cached in state forever."""
    cache = st.setdefault("sic", {})
    todo = [s for s in syms if s not in cache and s != "SPY"]
    if not todo:
        return cache
    try:
        tick = {v["ticker"].upper(): v["cik_str"] for v in sec_json("https://www.sec.gov/files/company_tickers.json").values()}
    except Exception as e:
        print("SEC ticker map unavailable:", repr(e)[:100])
        return cache
    for s in todo[:60]:
        cik = tick.get(s.upper()) or tick.get(s.upper().replace(".", "-"))
        if not cik:
            cache[s] = {"sic": None, "name": "unknown"}
            continue
        try:
            j = sec_json("https://data.sec.gov/submissions/CIK%010d.json" % int(cik))
            sic = str(j.get("sic") or "")
            cache[s] = {"sic": sic[:2] or None, "name": j.get("sicDescription") or ""}
        except Exception:
            pass
        time.sleep(0.15)  # SEC fair-access limit is 10 requests a second
    return cache


def load_rows(name):
    try:
        return json.loads((D / name).read_text()).get("rows", [])
    except Exception:
        return []


def day(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def latest_cluster(rows, date_key, who_key, n, window):
    """Latest date on which n different buyers fall inside the window."""
    rows = sorted(rows, key=lambda r: r[date_key])
    last = None
    for i, r in enumerate(rows):
        d = day(r[date_key])
        buyers = {x[who_key] for x in rows[:i + 1] if 0 <= (d - day(x[date_key])).days <= window}
        if len(buyers) >= n:
            last = (d, len(buyers))
    return last


def find_signals():
    out = collections.defaultdict(list)
    ins = [r for r in load_rows("insider_trades.json") if r.get("side") == "buy" and TICK.match(str(r.get("symbol", "")))
           and day(r.get("date")) and EXEC.search(str(r.get("role", "")))]
    by = collections.defaultdict(list)
    for r in ins:
        by[r["symbol"]].append(r)
        if (r.get("value") or 0) >= 250_000:
            out["insider_big_250k"].append((r["symbol"], day(r["date"]), "%s (%s) bought $%s" % (r.get("insider"), r.get("role"), format(int(r["value"]), ","))))
    for sym, L in by.items():
        c = latest_cluster(L, "date", "insider", 3, 30)
        if c:
            out["insider_cluster3"].append((sym, c[0], "%d insiders bought within 30 days" % c[1]))
        c2 = latest_cluster([r for r in L if (r.get("value") or 0) >= 25_000], "date", "insider", 2, 30)
        if c2:
            out["insider_cluster2_smallcap"].append((sym, c2[0], "%d insiders each bought $25k+ within 30 days" % c2[1]))
    con = [r for r in load_rows("congress_trades.json") if r.get("side") == "buy" and TICK.match(str(r.get("symbol", ""))) and day(r.get("filed"))]
    byc = collections.defaultdict(list)
    for r in con:
        byc[r["symbol"]].append(r)
        if (r.get("value_mid") or 0) >= 100_000:
            out["congress_big_100k"].append((r["symbol"], day(r["filed"]), "%s disclosed about $%s" % (r.get("filer"), format(int(r["value_mid"]), ","))))
    for sym, L in byc.items():
        c = latest_cluster(L, "filed", "fid", 3, 60)
        if c:
            out["congress_cluster3"].append((sym, c[0], "%d members bought within 60 days" % c[1]))
    for r in load_rows("signals.json"):
        if (r.get("score") or 0) >= 8 and TICK.match(str(r.get("symbol", ""))):
            out["terminal_top_signals"].append((r["symbol"], TODAY, "score %s: %s" % (r["score"], "; ".join(r.get("reasons", [])[:2]))))
    return out


def fetch(symbols):
    bars = {}
    syms = sorted(symbols)
    if not syms:
        return bars
    ys = [s.replace(".", "-") for s in syms]
    df = yf.download(ys, period="9mo", interval="1d", auto_adjust=True, group_by="ticker", threads=True, progress=False)
    for s, y in zip(syms, ys):
        try:
            b = (df[y] if len(ys) > 1 else df)[["Open", "High", "Low", "Close", "Volume"]].dropna()
            b.index = [pd.Timestamp(x).date() for x in b.index]
            if len(b) and b.index[-1] == TODAY and NOW.hour < 21:
                b = b.iloc[:-1]  # today's session is not finished yet
            if len(b):
                bars[s] = b
        except Exception:
            pass
    return bars


def cost(price):
    return 0.0015 if price >= 10 else 0.004


def value(p, b, at=None):
    """Mark a position to market using the latest close on or before `at`."""
    if b is None or p["entry_date"] not in b.index:
        return p.get("last_value", p["alloc"])
    pin = b.at[p["entry_date"], "Open"]
    closes = b.Close[[d <= (at or b.index[-1]) for d in b.index]]
    return p["alloc"] * (1 - p["c"]) * closes.iloc[-1] / pin


def max_dd(eq):
    peak, worst = START_CASH, 0.0
    for _, v in eq:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return round(100 * worst, 2)


def main():
    try:
        st = json.loads((P / "state.json").read_text())
    except Exception:
        st = {"created": NOW.isoformat(timespec="seconds"), "runs": 0, "strategies": {}}
    for k, cfg in STRATS.items():
        st["strategies"].setdefault(k, {"label": cfg["label"], "hold": cfg["hold"], "cash": START_CASH, "open": [], "pending": [],
                                        "closed": [], "seen": {}, "events": [], "equity": [], "started": TODAY.isoformat()})
    S = st["strategies"]
    for s in S.values():
        for q in s["open"]:
            q["entry_date"] = day(q["entry_date"])

    # 1. new signals become pending orders
    for k, sigs in find_signals().items():
        s = S[k]
        for sym, d, note in sigs:
            last = s["seen"].get(sym)
            if last and (TODAY - day(last)).days < max(60, STRATS[k]["hold"] or 0):
                continue
            s["seen"][sym] = TODAY.isoformat()
            if (TODAY - d).days > FRESH_DAYS:
                continue  # already old news when the engine first saw it
            if any(p["sym"] == sym for p in s["open"] + s["pending"]):
                continue
            s["pending"].append({"sym": sym, "signal": TODAY.isoformat(), "event": d.isoformat(), "note": note})
    spy_s = S["spy_benchmark"]
    if not spy_s["open"] and not spy_s["pending"] and spy_s["cash"] > 0:
        spy_s["pending"].append({"sym": "SPY", "signal": TODAY.isoformat(), "event": TODAY.isoformat(), "note": "benchmark"})

    need = {"SPY"} | {p["sym"] for s in S.values() for p in s["open"] + s["pending"]}
    bars = fetch(need)
    spy = bars.get("SPY")
    if spy is None:
        print("No SPY prices; nothing to do this run.")
        return
    mark_day = spy.index[-1]
    new_trades = []
    stressed, stress_why = market_stress()
    sic = industries(st, {p["sym"] for s in S.values() for p in s["pending"]} | {p["sym"] for s in S.values() for p in s["open"]})

    for k, s in S.items():
        hold = STRATS[k]["hold"]
        # 2. exits: stop-loss first (sold at the close that breaks it), then the end of the holding period
        keep = []
        for p in s["open"]:
            b = bars.get(p["sym"])
            if b is not None and p["entry_date"] in b.index:
                i = list(b.index).index(p["entry_date"])
                if "stop_pct" not in p:  # positions opened before the risk rules existed
                    p["size_x"], p["stop_pct"] = risk_numbers(b, i)
                pin = float(b.Open.iloc[i])
                last_j = min(len(b) - 1, i + hold - 1) if hold else len(b) - 1
                j, why = None, None
                if k != "spy_benchmark":
                    for jj in range(i, last_j + 1):
                        if b.Close.iloc[jj] <= pin * (1 - p["stop_pct"]):
                            j, why = jj, "stop-loss %.1f%%" % (100 * p["stop_pct"])
                            break
                if j is None and hold and len(b) - i >= hold:
                    j, why = i + hold - 1, "held %d days" % hold
                if j is not None:
                    pout = float(b.Close.iloc[j])
                    proceeds = p["alloc"] * (1 - p["c"]) * pout / pin * (1 - p["c"])
                    s["cash"] += proceeds
                    r = proceeds / p["alloc"] - 1
                    a = spy.Open.get(b.index[i])
                    z = spy.Close[[d <= b.index[j] for d in spy.index]]
                    sr = (z.iloc[-1] / a - 1) if a else None
                    t = {"strategy": k, "symbol": p["sym"], "signal_date": p["signal"], "entry_date": str(b.index[i]), "exit_date": str(b.index[j]),
                         "entry": round(pin, 4), "exit": round(pout, 4), "return_pct": round(100 * r, 2),
                         "spy_return_pct": round(100 * sr, 2) if sr is not None else None,
                         "excess_pct": round(100 * (r - sr), 2) if sr is not None else None, "exit_reason": why,
                         "size_x": p.get("size_x"), "stop_pct": round(100 * p["stop_pct"], 1), "industry": p.get("industry"), "note": p.get("note", "")}
                    s["closed"].append(t)
                    new_trades.append(t)
                    s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "sold at %.2f on %s (%s)" % (pout, b.index[j], why)})
                    continue
                p["last_value"] = float(value(p, b))
            keep.append(p)
        s["open"] = keep

        # risk gates for new entries
        eq_now = s["cash"] + sum(q.get("last_value", q["alloc"]) for q in s["open"])
        peak = max([e[1] for e in s["equity"]] + [START_CASH, eq_now])
        s["drawdown_pct"] = round(100 * (eq_now / peak - 1), 2)
        s["paused"] = k != "spy_benchmark" and eq_now < peak * (1 - DD_PAUSE)
        gate = None
        if k != "spy_benchmark":
            if s["paused"]:
                gate = "skipped: strategy is %.1f%% below its peak (pause at 15%%)" % -s["drawdown_pct"]
            elif stressed:
                gate = "skipped: market stress (%s)" % stress_why

        # 3. fill pending orders at the next session's open
        still = []
        for p in s["pending"]:
            b = bars.get(p["sym"])
            sd = day(p["signal"])
            nxt = [d for d in (b.index if b is not None else []) if d > sd]
            if not nxt:
                if (TODAY - sd).days > 7:
                    s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "dropped: no price data"})
                else:
                    still.append(p)
                continue
            d0 = nxt[0]
            i = list(b.index).index(d0)
            pin = float(b.Open.iloc[i])
            liq = float((b.Close.iloc[max(0, i - 20):i] * b.Volume.iloc[max(0, i - 20):i]).mean()) if i > 0 else 0
            if k != "spy_benchmark" and not (pin >= 1 and liq >= 1e6):
                s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "skipped: penny stock or too illiquid"})
                continue
            if STRATS[k].get("max_liq") and liq > STRATS[k]["max_liq"]:
                s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "skipped: not a smaller company"})
                continue
            if gate:
                s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": gate})
                continue
            if len(s["open"]) >= SLOTS:
                s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "skipped: all 20 slots full"})
                continue
            ind = (sic.get(p["sym"]) or {}).get("sic")
            if k != "spy_benchmark" and ind and sum(q.get("industry") == ind for q in s["open"]) >= MAX_PER_INDUSTRY:
                s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "skipped: already %d positions in industry %s (%s)" % (MAX_PER_INDUSTRY, ind, sic[p["sym"]].get("name", ""))})
                continue
            size_x, stop_pct = risk_numbers(b, i) if k != "spy_benchmark" else (1.0, 1.0)
            equity = s["cash"] + sum(q.get("last_value", q["alloc"]) for q in s["open"])
            alloc = s["cash"] if k == "spy_benchmark" else min(s["cash"], equity / SLOTS * size_x)
            if alloc < (equity / SLOTS) * 0.4 and k != "spy_benchmark":
                s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "skipped: not enough cash"})
                continue
            s["cash"] -= alloc
            q = {"sym": p["sym"], "signal": p["signal"], "note": p.get("note", ""), "entry_date": d0, "alloc": alloc, "c": cost(pin),
                 "size_x": size_x, "stop_pct": stop_pct, "industry": ind}
            q["last_value"] = float(value(q, b))
            s["open"].append(q)
            s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "bought at %.2f on %s (size %.2fx, stop %.0f%% below)" % (pin, d0, size_x, 100 * stop_pct) if k != "spy_benchmark" else "bought at %.2f on %s" % (pin, d0)})
        s["pending"] = still

        # 4. mark to market
        eq = s["cash"] + sum(q["last_value"] for q in s["open"])
        s["equity"] = [e for e in s["equity"] if e[0] != str(mark_day)] + [[str(mark_day), round(eq, 2)]]
        s["events"] = s["events"][-200:]

    # 5. outputs
    for s in S.values():
        for q in s["open"]:
            q["entry_date"] = str(q["entry_date"])
    st["runs"] += 1
    st["updated"] = NOW.isoformat(timespec="seconds")
    spy_start = S["spy_benchmark"]["equity"][0][1] if S["spy_benchmark"]["equity"] else START_CASH
    summ = {"updated": st["updated"], "priced_to": str(mark_day), "rules": __doc__.split("\n\n", 1)[1].strip(),
            "market_stress": stressed, "market_note": stress_why, "strategies": {}}
    for k, s in S.items():
        eq = s["equity"][-1][1] if s["equity"] else START_CASH
        C = s["closed"]
        x = [t["excess_pct"] for t in C if t.get("excess_pct") is not None]
        summ["strategies"][k] = {
            "label": s["label"], "hold_days": s["hold"], "started": s["started"], "value": round(eq, 2),
            "paused": s.get("paused", False), "drawdown_pct": s.get("drawdown_pct"),
            "max_drawdown_pct": max_dd(s["equity"]),
            "stops_hit": sum(1 for t in C if str(t.get("exit_reason", "")).startswith("stop")),
            "return_pct": round(100 * (eq / START_CASH - 1), 2), "cash": round(s["cash"], 2),
            "open": [{"symbol": q["sym"], "since": q["entry_date"], "value": round(q["last_value"], 2),
                      "return_pct": round(100 * (q["last_value"] / q["alloc"] - 1), 2), "why": q.get("note", ""),
                      "size_x": q.get("size_x"), "stop_pct": round(100 * q.get("stop_pct", 0), 1), "industry": (sic.get(q["sym"]) or {}).get("name")} for q in s["open"]],
            "pending": [p["sym"] for p in s["pending"]],
            "closed_trades": len(C),
            "win_pct": round(100 * sum(t["return_pct"] > 0 for t in C) / len(C), 1) if C else None,
            "beat_spy_pct": round(100 * sum(v > 0 for v in x) / len(x), 1) if x else None,
            "avg_return_pct": round(sum(t["return_pct"] for t in C) / len(C), 2) if C else None,
            "avg_excess_pct": round(sum(x) / len(x), 2) if x else None,
            "recent_trades": C[-10:], "equity": s["equity"][-400:], "events": s["events"][-15:],
        }
    summ["spy_since_start_pct"] = round(100 * (S["spy_benchmark"]["equity"][-1][1] / START_CASH - 1), 2) if S["spy_benchmark"]["equity"] else None
    (P / "state.json").write_text(json.dumps(st, indent=1, default=str))
    (P / "summary.json").write_text(json.dumps(summ, indent=1, default=str))
    tf = P / "trades.csv"
    if tf.exists():  # upgrade an old file to the current columns
        with tf.open() as f:
            r = csv.DictReader(f)
            if r.fieldnames != TRADE_COLS:
                rows = list(r)
                with tf.open("w", newline="") as g:
                    w = csv.DictWriter(g, fieldnames=TRADE_COLS, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(rows)
    new_file = not tf.exists()
    with tf.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        for t in new_trades:
            w.writerow(t)
    print("market:", stress_why)
    print(json.dumps({k: (v["value"], len(v["open"]), v["pending"], v["closed_trades"], v["stops_hit"], v["paused"]) for k, v in summ["strategies"].items()}, indent=1))


if __name__ == "__main__":
    main()
