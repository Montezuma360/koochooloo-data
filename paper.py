"""Koochooloo paper-trading engine: virtual money only. No broker, no real orders.

Each strategy starts with $100,000 and follows fixed rules:
- A signal counts from the first run that sees it, and only if it is at most 4 days old (no hindsight).
- Orders fill at the open of the next trading day after the signal; sells happen at the close once the holding period ends.
- Costs per side: 0.15% for stocks priced $10+, 0.40% below $10. Stocks under $1 or trading under $1M a day are skipped.
- 50 equal slots of 2% per strategy, so no single trade can lose more than 2% of the portfolio. A volatility size (0.5x-1.5x) and a stop-loss distance (2.5x average daily range, 6-25%) are recorded
  for every position but NOT applied: in round 3 research the stop-loss halved returns in both test periods, and volatility
  sizing cut returns by more than it cut risk. Switch on with USE_STOP / USE_VOL_SIZE if later research says otherwise.
- No new entries while a strategy is 15%+ below its own peak value, or while markets are stressed (VIX above 30, or high-yield credit spreads up 1+ point in a month).
- At most 4 positions in the same industry (SEC 2-digit SIC code) per strategy.
- Trading rules: only at regular-session prices (9:30 open, 4:00 close ET); no new trade when RSI(14) is above 70 (overbought) or
  below 30 (oversold) unless there is a strong contrarian reason (insiders buying their own stock after a fall); when the
  data is in doubt (stale feeds or too little price history) the default is NO_TRADE. Every decision is logged with its
  reason in plain language in data/paper/decisions.csv.
- Two strategies were added on 4 Nov 2026 and follow every rule above: "research_setup" (smaller company, 2+ insiders at $25k+, cash flow above
  profit) and "research_setup_trend" (the same, but no new entries while the S&P 500 is below its 200-day average). The older strategies are unchanged.
- Goal: beat the S&P 500 by the largest possible margin after costs, and turn a virtual GBP 100 into GBP 10,000. Strategies
  are ranked by their margin over the S&P 500; summary.json "goal" shows what GBP 100 has become in each and how long
  GBP 10,000 would take at the live pace (after 90 days) and at the paces measured in research.
Writes data/paper/state.json, data/paper/summary.json, data/paper/decisions.csv and appends closed trades to data/paper/trades.csv."""
import collections, csv, datetime as dt, json, os, pathlib, re, time, urllib.request
import pandas as pd
import yfinance as yf

D = pathlib.Path("data")
P = D / "paper"
P.mkdir(parents=True, exist_ok=True)
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()
START_CASH = 100_000.0
SLOTS = 50              # 50 slots of 2% each: even a total loss on one trade costs at most 2% of the portfolio
MAX_RISK = 0.02         # rule: never risk more than 2% of total portfolio value on a single trade
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
    "research_setup": {"label": "Research setup: a smaller company, 2+ insiders each buying $25k+ within 30 days, operating cash flow above profit (added 4 Nov 2026)", "hold": 60, "max_liq": 20e6},
    "research_setup_trend": {"label": "Research setup, but no new entries while the S&P 500 is below its 200-day average (added 4 Nov 2026)", "hold": 60, "max_liq": 20e6, "trend": True},
    "spy_benchmark": {"label": "Buy and hold the S&P 500 (SPY), the bar to beat", "hold": None},
}
TRADE_COLS = ["strategy", "symbol", "signal_date", "entry_date", "exit_date", "entry", "exit", "return_pct", "spy_return_pct", "excess_pct", "exit_reason", "size_x", "stop_pct", "industry", "note"]
DD_PAUSE = 0.15          # pause new entries 15% below the strategy's peak
MAX_PER_INDUSTRY = 4
USE_STOP = False         # round 3: stop-loss cut 60-day insider returns roughly in half (2014-2022 and 2023-2026)
USE_VOL_SIZE = False     # round 3: lower drawdowns but lower returns and no better risk-adjusted return out of sample
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
    try:  # the brain's research setup: smaller company + 2 insiders at $25k+ + cash flow above profit (current figures only)
        for st_ in json.loads((D / "brain.json").read_text()).get("stocks", []):
            if st_.get("research_setup") and TICK.match(str(st_.get("symbol", ""))):
                cl = [e for e in st_.get("evidence", []) if e.get("type") == "insider_cluster"]
                d = day(cl[0].get("date")) if cl else None
                if d:
                    for k in ("research_setup", "research_setup_trend"):
                        out[k].append((st_["symbol"], d, "research setup: " + str(cl[0].get("detail", ""))[:140]))
    except Exception as e:
        print("brain research setups unavailable:", repr(e)[:100])
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


def rsi(closes, n=14):
    """Wilder's 14-day RSI from closing prices (None if there is too little history)."""
    d = closes.diff().dropna()
    if len(d) < 2 * n:
        return None
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    return 100.0 if dn == 0 else float(100 - 100 / (1 + up / dn))


def spy_long():
    """About two years of SPY closes, enough for a 200-day average (None if Yahoo fails)."""
    try:
        df = yf.download("SPY", period="2y", interval="1d", auto_adjust=True, progress=False)
        c = df["Close"]
        if hasattr(c, "columns"):
            c = c.iloc[:, 0]
        c = c.dropna()
        c.index = [pd.Timestamp(x).date() for x in c.index]
        return c
    except Exception as e:
        print("SPY history unavailable:", repr(e)[:100])
        return None


def trend_gate(closes, d0):
    """None when the S&P 500 closed at or above its 200-day average before the fill day; otherwise the reason to wait."""
    if closes is None:
        return "no long S&P 500 history, so the default is no trade"
    h = closes[[d < d0 for d in closes.index]]
    if len(h) < 200:
        return "under 200 sessions of S&P 500 history, so the default is no trade"
    avg, last = float(h.tail(200).mean()), float(h.iloc[-1])
    if last >= avg:
        return None
    return "trend brake: the S&P 500 (%.0f) is below its 200-day average (%.0f), so no new entries" % (last, avg)


DECISIONS = []


def decide(s, k, p, decision, why, r=None):
    s["events"].append({"date": TODAY.isoformat(), "sym": p["sym"], "what": "%s: %s" % (decision, why)})
    DECISIONS.append({"date": TODAY.isoformat(), "strategy": k, "symbol": p["sym"], "decision": decision,
                      "rsi": round(r, 1) if r is not None else "", "reason": why})


def data_doubts():
    """Plain-language reason to doubt the data (stale prices, insider or signal feeds), or None."""
    try:
        h = json.loads((D / "health.json").read_text())
    except Exception:
        return None
    bad = [n for n in ("prices", "sec_insiders", "signals") if (h.get("feeds", {}).get(n) or {}).get("stale")]
    return ("stale feeds: " + ", ".join(bad)) if bad else None


# The owner's goal (2 Nov 2026): beat the S&P 500 by the largest possible margin after costs, and turn a virtual
# GBP 100 into GBP 10,000 (100x). The risk rules above still apply: the margin is widened by backing whichever strategy
# proves best on live data, never by betting more than 2% on one trade.
GOAL_START, GOAL_TARGET = 100.0, 10_000.0
LIVE_MIN_DAYS = 90       # a yearly pace from fewer days is noise, so it is not shown
# Yearly returns measured in research (round 3), used to show how long the target takes at a known pace
REFERENCE_PACE = {
    "S&P 500, long-run average": 0.10,
    "S&P 500, 2023-2026": 0.211,
    "Small-company insider buying, 2014-2022": 0.091,
    "Small-company insider buying, 2023-2026": 0.257,
}


def years_to_target(pace):
    import math
    return round(math.log(GOAL_TARGET / GOAL_START) / math.log(1 + pace), 1) if pace and pace > 0 else None


def goal_block(summ):
    """Leaderboard by margin over the S&P 500, what GBP 100 has become in each strategy, and the honest time to GBP 10,000."""
    spy = summ.get("spy_since_start_pct")
    board = []
    for k, v in summ["strategies"].items():
        days = (TODAY - dt.date.fromisoformat(str(v["started"])[:10])).days
        mult = v["value"] / START_CASH
        pace = mult ** (365 / days) - 1 if days >= LIVE_MIN_DAYS and mult > 0 else None
        v["margin_vs_spy_pct"] = round(v["return_pct"] - spy, 2) if spy is not None and k != "spy_benchmark" else None
        v["gbp100_now"] = round(GOAL_START * mult, 2)
        v["yearly_pace_pct"] = round(100 * pace, 1) if pace is not None else None
        v["years_to_10000_at_pace"] = years_to_target(pace)
        if k != "spy_benchmark":
            board.append({"strategy": k, "margin_vs_spy_pct": v["margin_vs_spy_pct"], "gbp100_now": v["gbp100_now"], "days_live": days})
    board.sort(key=lambda r: -(r["margin_vs_spy_pct"] if r["margin_vs_spy_pct"] is not None else -1e9))
    best = board[0] if board else None
    return {
        "aim": "Beat the S&P 500 by the largest possible margin after costs; turn GBP 100 into GBP 10,000 (100x)",
        "leaderboard": board,
        "leader": best["strategy"] if best else None,
        "leader_is_meaningful": bool(best and best["days_live"] >= LIVE_MIN_DAYS),
        "years_to_10000_at_reference_pace": {n: years_to_target(p) for n, p in REFERENCE_PACE.items()},
        "yearly_return_needed": {"%d years" % y: round(100 * ((GOAL_TARGET / GOAL_START) ** (1 / y) - 1), 1) for y in (3, 5, 10, 20)},
        "note": "Virtual money only. Rankings mean little before %d days of live trading. Not financial advice." % LIVE_MIN_DAYS,
    }


def max_dd(eq):
    peak, worst = START_CASH, 0.0
    for _, v in eq:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return round(100 * worst, 2)


def main():
    DECISIONS.clear()
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
    data_doubt = data_doubts()
    spy_hist = spy_long() if any(STRATS[k].get("trend") for k in S) else None
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
                if USE_STOP and k != "spy_benchmark":
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

        # 3. decide on each pending order: TRADE (fill at the regular-session open, 9:30 ET) or NO_TRADE, with the reason
        still = []
        for p in s["pending"]:
            b = bars.get(p["sym"])
            sd = day(p["signal"])
            nxt = [d for d in (b.index if b is not None else []) if d > sd]
            if not nxt:
                if (TODAY - sd).days > 7:
                    decide(s, k, p, "NO_TRADE", "no price data for a week, so the order was dropped")
                else:
                    still.append(p)
                continue
            d0 = nxt[0]
            i = list(b.index).index(d0)
            pin = float(b.Open.iloc[i])
            liq = float((b.Close.iloc[max(0, i - 20):i] * b.Volume.iloc[max(0, i - 20):i]).mean()) if i > 0 else 0
            r = rsi(b.Close.iloc[:i])  # momentum as known at the close before the fill
            tg = trend_gate(spy_hist, d0) if STRATS[k].get("trend") else None
            why = None
            if k != "spy_benchmark" and not (pin >= 1 and liq >= 1e6):
                why = "penny stock or too thinly traded (under $1 or under $1M a day)"
            elif STRATS[k].get("max_liq") and liq > STRATS[k]["max_liq"]:
                why = "not a smaller company (trades $%.0fM a day)" % (liq / 1e6)
            elif gate:
                why = gate
            elif k != "spy_benchmark" and data_doubt:
                why = "uncertain data (%s), so the default is no trade" % data_doubt
            elif tg:
                why = tg
            elif k != "spy_benchmark" and r is None:
                why = "not enough price history to judge momentum, so the default is no trade"
            elif k != "spy_benchmark" and r > 70:
                why = "RSI %.0f is above 70: overbought, buying now risks chasing a spike" % r
            elif k != "spy_benchmark" and r < 30 and not k.startswith(("insider_", "research_setup")):  # insider buying is the one accepted contrarian reason
                why = "RSI %.0f is below 30: oversold with no strong contrarian reason" % r
            elif len(s["open"]) >= SLOTS:
                why = "all %d slots are full" % SLOTS
            if why:
                decide(s, k, p, "NO_TRADE", why, r)
                continue
            ind = (sic.get(p["sym"]) or {}).get("sic")
            if k != "spy_benchmark" and ind and sum(q.get("industry") == ind for q in s["open"]) >= MAX_PER_INDUSTRY:
                decide(s, k, p, "NO_TRADE", "already %d positions in the same industry (%s)" % (MAX_PER_INDUSTRY, sic[p["sym"]].get("name", "")), r)
                continue
            size_x, stop_pct = risk_numbers(b, i) if k != "spy_benchmark" else (1.0, 1.0)
            vol_note = size_x
            if not USE_VOL_SIZE:
                size_x = 1.0
            equity = s["cash"] + sum(q.get("last_value", q["alloc"]) for q in s["open"])
            alloc = s["cash"] if k == "spy_benchmark" else min(s["cash"], equity / SLOTS * size_x, equity * MAX_RISK)
            if alloc < (equity / SLOTS) * 0.4 and k != "spy_benchmark":
                decide(s, k, p, "NO_TRADE", "not enough cash left", r)
                continue
            s["cash"] -= alloc
            q = {"sym": p["sym"], "signal": p["signal"], "note": p.get("note", ""), "entry_date": d0, "alloc": alloc, "c": cost(pin),
                 "size_x": size_x, "vol_size_x": vol_note, "stop_pct": stop_pct, "industry": ind, "rsi": round(r, 1) if r is not None else None}
            q["last_value"] = float(value(q, b))
            s["open"].append(q)
            if k == "spy_benchmark":
                decide(s, k, p, "TRADE", "benchmark: buy and hold the S&P 500 at %.2f on %s" % (pin, d0), r)
            else:
                contra = "; RSI %.0f is oversold, but insiders buying with their own money is the contrarian reason" % r if r < 30 else "; RSI %.0f is neutral" % r
                decide(s, k, p, "TRADE", "%s. Bought $%s (%.1f%% of the portfolio, the 2%% cap) at the %s open, %.2f%s" % (
                    p.get("note", "signal"), format(int(alloc), ","), 100 * alloc / equity, d0, pin, contra), r)
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
    summ["goal"] = goal_block(summ)
    (P / "state.json").write_text(json.dumps(st, indent=1, default=str))
    (P / "summary.json").write_text(json.dumps(summ, indent=1, default=str))
    dfile = P / "decisions.csv"
    newd = not dfile.exists()
    with dfile.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "strategy", "symbol", "decision", "rsi", "reason"])
        if newd:
            w.writeheader()
        w.writerows(DECISIONS)
    with dfile.open(newline="") as f:  # the last 60 from the whole log, so days without new decisions still show the recent ones
        summ["latest_decisions"] = list(csv.DictReader(f))[-60:]
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
