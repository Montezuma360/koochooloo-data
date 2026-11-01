"""Koochooloo paper-trading engine: virtual money only. No broker, no real orders.

Each strategy starts with $100,000 and follows fixed rules:
- A signal counts from the first run that sees it, and only if it is at most 4 days old (no hindsight).
- Orders fill at the open of the next trading day after the signal; sells happen at the close once the holding period ends.
- Costs per side: 0.15% for stocks priced $10+, 0.40% below $10. Stocks under $1 or trading under $1M a day are skipped.
- 20 equal slots per strategy. When all slots are full, new signals are skipped.
Writes data/paper/state.json, data/paper/summary.json and appends closed trades to data/paper/trades.csv."""
import collections, csv, datetime as dt, json, pathlib, re
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
    "congress_cluster3": {"label": "3+ members of Congress disclose buying the same stock within 60 days", "hold": 60},
    "congress_big_100k": {"label": "A member of Congress discloses a $100k+ purchase", "hold": 60},
    "terminal_top_signals": {"label": "The terminal's own signal score reaches 8+", "hold": 20},
    "spy_benchmark": {"label": "Buy and hold the S&P 500 (SPY), the bar to beat", "hold": None},
}
TRADE_COLS = ["strategy", "symbol", "signal_date", "entry_date", "exit_date", "entry", "exit", "return_pct", "spy_return_pct", "excess_pct", "note"]


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
            b = (df[y] if len(ys) > 1 else df)[["Open", "Close", "Volume"]].dropna()
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

    for k, s in S.items():
        hold = STRATS[k]["hold"]
        # 2. exits after the holding period
        keep = []
        for p in s["open"]:
            b = bars.get(p["sym"])
            if b is not None and p["entry_date"] in b.index:
                i = list(b.index).index(p["entry_date"])
                if hold and len(b) - i >= hold:
                    j = i + hold - 1
                    pin, pout = b.Open.iloc[i], b.Close.iloc[j]
                    proceeds = p["alloc"] * (1 - p["c"]) * pout / pin * (1 - p["c"])
                    s["cash"] += proceeds
                    r = proceeds / p["alloc"] - 1
                    a = spy.Open.get(b.index[i])
                    z = spy.Close[[d <= b.index[j] for d in spy.index]]
                    sr = (z.iloc[-1] / a - 1) if a else None
                    t = {"strategy": k, "symbol": p["sym"], "signal_date": p["signal"], "entry_date": str(b.index[i]), "exit_date": str(b.index[j]),
                         "entry": round(float(pin), 4), "exit": round(float(pout), 4), "return_pct": round(100 * r, 2),
                         "spy_return_pct": round(100 * sr, 2) if sr is not None else None,
                         "excess_pct": round(100 * (r - sr), 2) if sr is not None else None, "note": p.get("note", "")}
                    s["closed"].append(t)
                    new_trades.append(t)
                    continue
                p["last_value"] = float(value(p, b))
            keep.append(p)
        s["open"] = keep

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
            if len(s["open"]) >= SLOTS:
                s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "skipped: all 20 slots full"})
                continue
            equity = s["cash"] + sum(q.get("last_value", q["alloc"]) for q in s["open"])
            alloc = s["cash"] if k == "spy_benchmark" else min(s["cash"], equity / SLOTS)
            if alloc < (equity / SLOTS) * 0.5 and k != "spy_benchmark":
                s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "skipped: not enough cash"})
                continue
            s["cash"] -= alloc
            q = {"sym": p["sym"], "signal": p["signal"], "note": p.get("note", ""), "entry_date": d0, "alloc": alloc, "c": cost(pin)}
            q["last_value"] = float(value(q, b))
            s["open"].append(q)
            s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "bought at %.2f on %s" % (pin, d0)})
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
    summ = {"updated": st["updated"], "priced_to": str(mark_day), "rules": __doc__.split("\n\n", 1)[1].strip(), "strategies": {}}
    for k, s in S.items():
        eq = s["equity"][-1][1] if s["equity"] else START_CASH
        C = s["closed"]
        x = [t["excess_pct"] for t in C if t.get("excess_pct") is not None]
        summ["strategies"][k] = {
            "label": s["label"], "hold_days": s["hold"], "started": s["started"], "value": round(eq, 2),
            "return_pct": round(100 * (eq / START_CASH - 1), 2), "cash": round(s["cash"], 2),
            "open": [{"symbol": q["sym"], "since": q["entry_date"], "value": round(q["last_value"], 2),
                      "return_pct": round(100 * (q["last_value"] / q["alloc"] - 1), 2), "why": q.get("note", "")} for q in s["open"]],
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
    new_file = not tf.exists()
    with tf.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS)
        if new_file:
            w.writeheader()
        for t in new_trades:
            w.writerow(t)
    print(json.dumps({k: (v["value"], len(v["open"]), v["pending"], v["closed_trades"]) for k, v in summ["strategies"].items()}, indent=1))


if __name__ == "__main__":
    main()
