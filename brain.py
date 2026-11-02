"""Koochooloo brain: one consolidated file (data/brain.json) built from every feed, for the terminal and Setareh.

It answers, in one read:
- regime: is the market risk-on, cautious or risk-off, and why (200-day trend, stress switch, fear & greed, VIX, credit)
- stocks: every stock with any evidence today (insiders, Congress, SEC filings, options, news, social, funds), with
  price, size, RSI, trend and fundamentals, each piece of evidence labelled with what our research found about it,
  and the one research-backed setup flagged
- markets, macro, calendar, crypto, news, SEC red flags, options, sentiment, paper trading, data health

Evidence labels come from three rounds of research (claude/research-findings.md): "edge" (held up in tests),
"no edge" (tested and failed), "untested" (attention only), "warning" (a red flag in the company's own filing).
Nothing here is a prediction or advice. Run it after the other feeds; it reads files only, plus Yahoo prices and SEC
company facts for the stocks it lists (cached)."""
import datetime as dt, json, math, os, pathlib, re, statistics, time, urllib.request

D = pathlib.Path("data")
NOW = dt.datetime.now(dt.timezone.utc)
STAMP = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
TODAY = NOW.date()
SEC_UA = {"User-Agent": os.environ.get("SEC_CONTACT") or "KoochoolooResearch github.com/Montezuma360/koochooloo-data"}
TICK = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
EXEC = re.compile(r"director|officer|chief|president|ceo|cfo|coo|chair|vp|vice|counsel|treasurer|secretary", re.I)  # as paper.py
PRICE_MAX_AGE_MIN = 10       # stock-level prices for the listed stocks are refetched at most this often
FUND_MAX_AGE_DAYS = 7        # SEC company facts (cash flow vs profit) per stock

NOTES = {
    "research_setup": "Smaller company ($1M-$20M traded a day) + 2 or more insiders each buying $25k+ within 30 days + operating "
                      "cash flow above reported profit. In our tests: about +2.4% (2014-2022) and +2.7% (2023-2026) per trade above "
                      "the S&P 500 over 60 days, on average. The most consistent result found, not a guarantee; many trades still lose.",
    "insider_cluster": "2 or more insiders bought within 30 days. Tested: an edge in smaller companies ($1M-$20M traded a day) in "
                       "both periods; in larger companies the edge faded after 2022; below $1M a day it was not tested.",
    "insider_big_buy": "An executive or director bought $250k or more. Tested: no edge over the S&P 500 after 2022.",
    "congress": "Members of Congress disclosed buying. Tested: copying them after disclosure lost to the S&P 500.",
    "signal_score": "The terminal's own tally of buying activity. Untested: shows attention, not an edge.",
    "unusual_options": "Large new options positions. Untested: shows attention, not an edge.",
    "news": "In the headlines today. Untested: attention only.",
    "social": "Trending on StockTwits or Reddit. Untested: attention only.",
    "activist": "An investor filed a Schedule 13D (5%+ stake with intent to influence). Untested here.",
    "funds_13f": "Change in how many big funds hold it last quarter (13F). Untested here; data is 45+ days old by law.",
    "sec_red_flag": "Warning sign in the company's own SEC filing.",
    "results": "Announced results in the last 3 days (8-K item 2.02).",
}
TESTED = {"research_setup": "edge", "insider_cluster": "edge (smaller companies)", "insider_big_buy": "no edge", "congress": "no edge",
          "signal_score": "untested", "unusual_options": "untested", "news": "untested", "social": "untested", "activist": "untested",
          "funds_13f": "untested", "sec_red_flag": "warning", "results": "event"}


def load(p, default=None):
    try:
        return json.loads((D / p).read_text())
    except Exception:
        return default if default is not None else {}


def rows(p):
    return (load(p) or {}).get("rows", [])


def num(x, nd=2):
    try:
        v = float(x)
        return None if math.isnan(v) or math.isinf(v) else round(v, nd)
    except (TypeError, ValueError):
        return None


def age_min(stamp):
    try:
        t = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return round((NOW - t).total_seconds() / 60, 1)
    except Exception:
        return None


def days_ago(n):
    return str(TODAY - dt.timedelta(days=n))


def sec_get(url):
    req = urllib.request.Request(url, headers=SEC_UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        time.sleep(0.15)
        return json.loads(r.read())


# ---------- market regime ----------
def regime(quotes, macro, fg, paper):
    why, level = [], "risk-on"
    spy = [r for r in rows("prices_daily.json") if r.get("symbol") == "SPY"]
    closes = [r["close"] for r in sorted(spy, key=lambda r: r["t"])]
    live = quotes.get("SPY", {}).get("price")
    if live and closes and spy and sorted(spy, key=lambda r: r["t"])[-1]["t"][:10] != str(TODAY):
        closes = closes + [live]
    trend = None
    if len(closes) >= 200:
        ma = statistics.mean(closes[-200:])
        trend = round(100 * (closes[-1] / ma - 1), 2)
        if trend < 0:
            level = "risk-off"
            why.append("S&P 500 is %.1f%% below its 200-day average (our best-tested risk signal: it cut the worst fall from -34%% to -19%% in 2019-2026)" % -trend)
        else:
            why.append("S&P 500 is %.1f%% above its 200-day average" % trend)
    vix = (macro.get("VIXCLS") or {}).get("value")
    vlive = quotes.get("^VIX", {}).get("price") or vix
    hy = macro.get("BAMLH0A0HYM2") or {}
    if paper.get("market_stress"):
        level = "risk-off"
        why.append("market stress switch is on: %s" % paper.get("market_note"))
    if level != "risk-off":
        caution = []
        if fg.get("value") is not None and fg["value"] < 25:
            caution.append("Fear & Greed %s (%s)" % (fg["value"], fg.get("label")))
        if vlive and vlive > 25:
            caution.append("VIX %.1f above 25" % vlive)
        if (hy.get("change_1m") or 0) > 0.5:
            caution.append("high-yield credit spread up %.2f points in a month" % hy["change_1m"])
        if caution:
            level = "caution"
            why += caution
    return {"level": level, "why": why, "spy_vs_200d_pct": trend, "vix": num(vlive), "fear_greed": fg.get("value"),
            "fear_greed_label": fg.get("label"), "high_yield_spread": hy.get("value"), "high_yield_change_1m": hy.get("change_1m"),
            "stress_switch": bool(paper.get("market_stress")),
            "rules": "risk-off: S&P below its 200-day average or stress switch on (VIX > 30 or high-yield spread +1 point in a month); "
                     "caution: Fear & Greed below 25, VIX above 25 or high-yield spread +0.5 in a month; otherwise risk-on."}


# ---------- per-stock evidence ----------
def gather_evidence():
    ev = {}

    def add(sym, kind, detail, date=None, **extra):
        sym = str(sym or "").upper().replace("-", ".")
        if not TICK.match(sym):
            return
        ev.setdefault(sym, {"evidence": [], "names": set()})
        ev[sym]["evidence"].append(dict({"type": kind, "tested": TESTED[kind], "detail": detail, "date": date}, **extra))

    ins = rows("insider_trades.json")
    # the research rule counts officers and directors (the paper engine's filter) who each bought $25k+ within 30 days
    n25 = {}
    for r in ins:
        if (r.get("side") == "buy" and (r.get("value") or 0) >= 25_000 and (r.get("date") or "") >= days_ago(30)
                and EXEC.search(str(r.get("role") or ""))):
            n25.setdefault(str(r.get("symbol")), set()).add(r.get("insider"))
    for c in rows("insider_clusters.json"):
        k = len(n25.get(c["symbol"], ()))
        add(c["symbol"], "insider_cluster", "%d insiders bought $%s in total within 30 days (%s); %d officers or directors each bought $25k+" % (
            c["n"], format(int(c["value"]), ","), ", ".join(c["insiders"][:4]), k), c.get("last"), insiders=c["n"], value=c["value"],
            execs_25k=k)
    for r in ins:
        if r.get("side") == "buy" and (r.get("value") or 0) >= 250_000 and (r.get("date") or "") >= days_ago(14):
            add(r["symbol"], "insider_big_buy", "%s (%s) bought $%s" % (r.get("insider"), r.get("role") or "insider", format(int(r["value"]), ",")),
                r.get("date"), value=r["value"])
    for c in rows("congress_clusters.json"):
        if c.get("n", 0) >= 2:
            add(c["symbol"], "congress", "%d members disclosed buying (~$%s), last filed %s" % (c["n"], format(int(c.get("value") or 0), ","),
                c.get("last_filed")), c.get("last_filed"), members=c["n"])
    for s in rows("signals.json")[:40]:
        if (s.get("score") or 0) >= 6:
            add(s["symbol"], "signal_score", "score %s: %s" % (s["score"], "; ".join(s.get("reasons", [])[:2])), str(TODAY), score=s["score"])
    sf = load("intel/sec_filings.json")
    for f in sf.get("red_flags_3d", []):
        if f.get("ticker"):
            add(f["ticker"], "sec_red_flag", "%s: %s" % (f["form"], "; ".join(i["meaning"] for i in f["items"] if i["item"] in f["red_flag"])),
                f["time"][:10], link=f["link"])
    for f in sf.get("activist_stakes_3d", []):
        if f.get("ticker"):
            add(f["ticker"], "activist", "%s filed %s" % (f["company"], f["form"]), f["time"][:10], link=f["link"])
    for f in sf.get("results_announced_3d", []):
        if f.get("ticker"):
            add(f["ticker"], "results", "results filed (8-K 2.02)", f["time"][:10], link=f["link"])
    op = load("intel/options.json")
    seen_u = set()
    for u in op.get("unusual", []):
        if u["symbol"] in seen_u:
            continue
        seen_u.add(u["symbol"])
        add(u["symbol"], "unusual_options", "%s strike %s expiring %s: %s contracts traded vs %s open, about $%s" % (
            u["type"], u["strike"], u["expiry"], format(u["volume"], ","), format(u["open_interest"], ","),
            format(u["notional_usd"], ",")), str(TODAY))
    news = load("intel/news.json")
    for sym, n in news.get("most_mentioned_24h", []):
        if n >= 2 and sym not in ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "LINK", "AVAX"):
            add(sym, "news", "%d headlines in 24 hours" % n, str(TODAY), headlines=n)
    for t in load("intel/stocktwits.json").get("trending", [])[:15]:
        if t.get("class") == "Stock":
            add(t["symbol"], "social", "trending on StockTwits: %s" % (t.get("why") or "")[:160], str(TODAY))
    wsb = (load("intel/reddit_forums.json").get("forums") or {}).get("r/wallstreetbets", [])
    for x in wsb[:10]:
        add(x["symbol"], "social", "%s mentions on r/wallstreetbets today (%s a day earlier)" % (x["mentions"], x.get("mentions_24h_ago")), str(TODAY))
    f13 = load("alt/fund_holdings_13f.json")
    for x in (f13.get("most_funds_added") or [])[:10]:
        if x.get("symbol") in ev:
            add(x["symbol"], "funds_13f", "%+d funds held it in %s (now %d)" % (x["funds_change"], f13.get("period"), x["funds"]), f13.get("period"))
    for sym in list(ev):
        w = (f13.get("watch") or {}).get(sym)
        if w and w.get("funds_change") and not any(e["type"] == "funds_13f" for e in ev[sym]["evidence"]):
            add(sym, "funds_13f", "%+d funds held it in %s (now %d)" % (w["funds_change"], f13.get("period"), w["funds"]), f13.get("period"))
    for strat, s in (load("paper/summary.json").get("strategies") or {}).items():
        for o in s.get("open", []):
            if o.get("symbol") in ev or TICK.match(str(o.get("symbol", ""))):
                ev.setdefault(o["symbol"], {"evidence": [], "names": set()}).setdefault("paper", []).append(
                    {"strategy": strat, "since": o.get("since"), "return_pct": o.get("return_pct")})
    return ev


def rsi(closes, n=14):
    d = [b - a for a, b in zip(closes, closes[1:])]
    if len(d) < 2 * n:
        return None
    up = dn = 0.0
    a = 1 / n
    for x in d:  # Wilder smoothing, same as the paper engine
        up = up + a * (max(x, 0) - up)
        dn = dn + a * (max(-x, 0) - dn)
    return 100.0 if dn == 0 else round(100 - 100 / (1 + up / dn), 1)


def price_facts(symbols, cache):
    """Price, 1-day and 1-month change, RSI, 200-day trend, 52-week high gap and size (average $ traded a day)."""
    have = cache.get("prices", {})
    fresh = age_min(cache.get("prices_at")) is not None and age_min(cache.get("prices_at")) < PRICE_MAX_AGE_MIN
    need = [s for s in symbols if not fresh or s not in have]
    if not need:
        return have
    try:
        import yfinance as yf
    except ImportError:
        return have
    ys = [s.replace(".", "-") for s in need]
    try:
        df = yf.download(ys, period="1y", interval="1d", auto_adjust=True, group_by="ticker", threads=True, progress=False)
    except Exception:
        return have
    for s, y in zip(need, ys):
        try:
            b = (df[y] if len(ys) > 1 else df)[["Close", "Volume"]].dropna()
            c = [float(x) for x in b["Close"]]
            v = [float(x) for x in b["Volume"]]
            if len(c) < 2:
                continue
            dollar = statistics.mean(ci * vi for ci, vi in zip(c[-20:], v[-20:]))
            size = "micro (under $1M a day)" if dollar < 1e6 else "smaller ($1M-$20M a day)" if dollar <= 20e6 else "larger (over $20M a day)"
            have[s] = {"price": num(c[-1]), "change_1d_pct": num(100 * (c[-1] / c[-2] - 1)),
                       "change_1m_pct": num(100 * (c[-1] / c[-22] - 1)) if len(c) >= 22 else None, "rsi14": rsi(c),
                       "vs_200d_pct": num(100 * (c[-1] / statistics.mean(c[-200:]) - 1)) if len(c) >= 200 else None,
                       "below_52w_high_pct": num(100 * (c[-1] / max(c) - 1)), "avg_dollar_volume_20d": round(dollar), "size": size,
                       "as_of": str(b.index[-1].date())}
        except Exception:
            continue
    cache["prices"], cache["prices_at"] = {s: have[s] for s in symbols if s in have}, STAMP  # keep only listed stocks
    return have


def cash_vs_profit(symbols, cache):
    """Latest annual operating cash flow and profit from SEC company facts (cached a week)."""
    fc = cache.setdefault("fund", {})
    fund = load("alt/fundamentals.json").get("watch") or {}
    cikmap = {v: int(k) for k, v in (load("intel/cik_tickers.json").get("map") or {}).items()}
    out, calls = {}, 0
    for s in symbols:
        if s in fund and fund[s].get("op_cash_flow") is not None and fund[s].get("net_income") is not None:
            f = fund[s]
            out[s] = {"year": f.get("year"), "op_cash_flow": f["op_cash_flow"], "net_income": f["net_income"],
                      "cash_backed": f["op_cash_flow"] > f["net_income"], "net_margin_pct": f.get("net_margin_pct"),
                      "revenue_growth_pct": f.get("revenue_growth_pct"), "source": "SEC XBRL (weekly)"}
            continue
        c = fc.get(s)
        if c and age_min(c.get("at")) is not None and age_min(c["at"]) < FUND_MAX_AGE_DAYS * 1440:
            if c.get("data"):
                out[s] = c["data"]
            continue
        cik = cikmap.get(s)
        if not cik or calls >= 30:
            continue
        data = None
        try:
            vals = {}
            for concept in ("NetCashProvidedByUsedInOperatingActivities", "NetIncomeLoss"):
                calls += 1
                j = sec_get("https://data.sec.gov/api/xbrl/companyconcept/CIK%010d/us-gaap/%s.json" % (cik, concept))
                fy = [u for u in j.get("units", {}).get("USD", []) if u.get("form") in ("10-K", "20-F", "40-F") and u.get("fp") == "FY"
                      and str(u.get("frame", "")).startswith("CY") and len(str(u.get("frame", ""))) == 6]
                if fy:
                    last = max(fy, key=lambda u: u["frame"])
                    vals[concept] = (last["frame"], last["val"])
            ocf, ni = vals.get("NetCashProvidedByUsedInOperatingActivities"), vals.get("NetIncomeLoss")
            if ocf and ni and ocf[0] == ni[0]:
                data = {"year": ocf[0][2:], "op_cash_flow": ocf[1], "net_income": ni[1], "cash_backed": ocf[1] > ni[1],
                        "source": "SEC company facts"}
        except Exception:
            data = None
        fc[s] = {"at": STAMP, "data": data}
        if data:
            out[s] = data
    return out


def stocks_block(ev, cache):
    syms = sorted(ev)
    px = price_facts(syms, cache)
    cf = cash_vs_profit([s for s in syms if any(e["type"] in ("insider_cluster", "insider_big_buy") for e in ev[s]["evidence"])], cache)
    out = []
    for s in syms:
        e = ev[s]["evidence"]
        p = px.get(s, {})
        f = cf.get(s)
        size = p.get("size", "")
        for x in e:  # the cluster edge was found in smaller companies only, so label it by this company's actual size
            if x["type"] == "insider_cluster":
                x["tested"] = ("edge (smaller companies)" if size.startswith("smaller") else
                               "no reliable edge (larger companies, after 2022)" if size.startswith("larger") else
                               "untested (below the tested size: under $1M traded a day)")
        cl = [x for x in e if x["type"] == "insider_cluster" and (x.get("execs_25k") or 0) >= 2]
        setup = bool(cl and size.startswith("smaller") and f and f.get("cash_backed"))
        if setup:
            e.insert(0, {"type": "research_setup", "tested": TESTED["research_setup"], "detail": NOTES["research_setup"], "date": str(TODAY)})
        types = sorted({x["type"] for x in e})
        r = p.get("rsi14")
        out.append({"symbol": s, "research_setup": setup, "evidence_types": types,
                    "warnings": [x["detail"] for x in e if x["tested"] == "warning"],
                    "price": p, "fundamentals": f,
                    "rsi_rule": None if r is None else ("ok (30-70)" if 30 <= r <= 70 else "above 70: overbought" if r > 70 else "below 30: oversold"),
                    "evidence": e, "paper_positions": ev[s].get("paper", [])})
    def rank(t):
        return 3 if t.startswith("edge") else 0 if t.startswith(("no", "warning")) else 1
    out.sort(key=lambda x: (-int(x["research_setup"]), -max([rank(ev_["tested"]) for ev_ in x["evidence"]] or [0]),
                            -len(x["evidence_types"]), x["symbol"]))
    return out


# ---------- the rest ----------
def compact_markets(quotes):
    idx = {k: quotes[k] for k in ("SPY", "QQQ", "IWM", "DIA", "^VIX", "^FTSE", "^GDAXI", "^N225") if k in quotes}
    sectors = [{k: r.get(k) for k in ("symbol", "sector", "1d", "1w_vs_spy", "1m_vs_spy", "3m_vs_spy")} for r in rows("sector_board.json")]
    ff = [{k: r.get(k) for k in ("symbol", "name", "last", "change_1d_pct", "change_5d_pct")} for r in load("intel/futures_fx.json").get("rows", [])]
    mv = lambda p: [{"symbol": r.get("symbol"), "name": r.get("shortName"), "change_pct": num(r.get("regularMarketChangePercent")),
                     "price": r.get("regularMarketPrice")} for r in rows(p)[:8]]
    tr = rows("treasury_rates.json")
    return {"indices": idx, "sectors": sectors, "futures_fx": ff, "gainers": mv("gainers.json"), "losers": mv("losers.json"),
            "most_active": mv("most_active.json"), "treasury": tr[-1] if tr else None}


def compact_macro(macro):
    us = {k: {"label": v.get("label"), "value": v.get("value"), "date": v.get("date"), "change_1m": v.get("change_1m"),
              **({"inflation_yoy_pct": v["inflation_yoy_pct"]} if "inflation_yoy_pct" in v else {})} for k, v in macro.items()}
    g = (load("intel/macro_global.json").get("series") or {})
    other = {k: {kk: v.get(kk) for kk in ("latest", "value", "previous", "growth_pct", "inflation_annual_pct", "jobs_added_thousands") if v.get(kk) is not None}
             for k, v in g.items()}
    cal = load("intel/calendar.json")
    cot = [{k: m.get(k) for k in ("market", "who", "net_pct_of_open_interest", "percentile_3y", "change_1w")} for m in load("alt/cot.json").get("markets", [])]
    sv = load("alt/short_volume.json")
    return {"us_fred": us, "global": other, "high_impact_events": cal.get("high_impact_upcoming", [])[:12],
            "futures_positioning_cftc": cot, "market_short_ratio": sv.get("market_short_ratio"), "short_ratio_date": sv.get("date")}


def compact_crypto():
    c, cs, cd = load("intel/crypto.json"), load("intel/crypto_sectors.json"), load("intel/crypto_derivatives.json")
    cf, oc = load("intel/crypto_fear_greed.json"), load("intel/onchain.json")
    return {"global": c.get("global"), "top": (c.get("top100") or [])[:15], "trending": (c.get("trending") or [])[:8],
            "exchange_spread_pct": c.get("exchange_spread_pct"), "sectors_best": (cs.get("best_24h") or [])[:6],
            "sectors_worst": (cs.get("worst_24h") or [])[:6], "perpetuals": cd.get("perpetuals"),
            "fear_greed": cf.get("now"), "bitcoin_network": (oc.get("bitcoin") or {})}


def compact_paper():
    p = load("paper/summary.json")
    g = p.get("goal") or {}
    strat = {k: {kk: v.get(kk) for kk in ("label", "return_pct", "margin_vs_spy_pct", "gbp100_now", "paused", "drawdown_pct", "closed_trades",
                                          "win_pct", "beat_spy_pct")} | {"open": len(v.get("open", []))} for k, v in (p.get("strategies") or {}).items()}
    return {"updated": p.get("updated"), "spy_since_start_pct": p.get("spy_since_start_pct"), "goal": {k: g.get(k) for k in
            ("aim", "leaderboard", "leader", "leader_is_meaningful", "years_to_10000_at_reference_pace", "note")},
            "strategies": strat, "latest_decisions": (p.get("latest_decisions") or [])[-15:]}


def health_block():
    h = load("health.json")
    feeds = {k: {"age_min": round(60 * v["age_hours"], 1) if v.get("age_hours") is not None else None, "ok": v.get("ok"),
                 "on_target": v.get("on_target")} for k, v in (h.get("feeds") or {}).items()}
    st = load("intel/status.json").get("sources") or {}
    intel = {k: {"age_min": age_min(v.get("last_ok")), "ok": not v.get("error"), "every_min": v.get("every_minutes")} for k, v in st.items()}
    return {"healthy": h.get("healthy"), "problems": h.get("problems"), "warnings": h.get("warnings"), "repo_size_mb": h.get("repo_size_mb"),
            "feeds": feeds, "intelligence_sources": intel, "checked": h.get("updated")}


def main():
    t0 = time.time()
    cache = load("brain_cache.json") or {}
    quotes = {r["symbol"]: r for r in rows("quotes.json")}
    macro = (load("alt/macro.json").get("series") or {})
    fg = load("intel/fear_greed.json")
    paper = load("paper/summary.json")
    reg = regime(quotes, macro, fg, paper)
    ev = gather_evidence()
    stocks = stocks_block(ev, cache)
    news = load("intel/news.json")
    sf = load("intel/sec_filings.json")
    op = load("intel/options.json")
    setups = [s["symbol"] for s in stocks if s["research_setup"]]
    summary = [
        "Market: %s. %s." % (reg["level"], "; ".join(reg["why"])),
        "Fear & Greed %s (%s); VIX %s; high-yield spread %s (%+.2f in a month)." % (
            reg["fear_greed"], reg["fear_greed_label"], reg["vix"], reg["high_yield_spread"], reg["high_yield_change_1m"] or 0),
        "Research-backed setups right now: %s." % (", ".join(setups) if setups else "none"),
        "%d stocks have evidence today; %d carry a warning from their own SEC filing." % (len(stocks), sum(1 for s in stocks if s["warnings"])),
    ]
    brain = {
        "updated": STAMP, "about": __doc__.split("\n\n")[0], "summary": summary, "regime": reg,
        "stocks": stocks[:150], "evidence_notes": NOTES,
        "markets": compact_markets(quotes), "macro": compact_macro(macro), "crypto": compact_crypto(),
        "news": {"latest": [{k: i.get(k) for k in ("time", "source", "title", "link", "tags")} for i in (news.get("items") or [])
                            if i.get("kind") != "crypto"][:20],
                 "crypto_latest": [{k: i.get(k) for k in ("time", "source", "title", "link")} for i in (news.get("items") or [])
                                   if i.get("kind") == "crypto"][:8],
                 "most_mentioned_24h": news.get("most_mentioned_24h")},
        "sec": {"red_flags": sf.get("red_flags_3d", [])[:15], "activist_stakes": sf.get("activist_stakes_3d", [])[:15],
                "results_announced": len(sf.get("results_announced_3d", []))},
        "options": {"aggregate_put_call": op.get("aggregate_put_call_volume"), "symbols": op.get("symbols"), "unusual": (op.get("unusual") or [])[:10]},
        "fear_greed": {k: fg.get(k) for k in ("value", "label", "components_0_to_100", "week_ago", "month_ago")},
        "sentiment": {"stocktwits": [{k: t.get(k) for k in ("symbol", "name", "why")} for t in (load("intel/stocktwits.json").get("trending") or [])[:12]],
                      "reddit_rising": (load("alt/reddit_mentions.json").get("rising") or [])[:10]},
        "paper": compact_paper(), "health": health_block(),
    }
    brain["seconds_to_build"] = round(time.time() - t0, 1)
    (D / "brain.json").write_text(json.dumps(brain, separators=(",", ":"), default=str))
    (D / "brain_cache.json").write_text(json.dumps(cache, separators=(",", ":"), default=str))
    print("\n".join(summary))
    print("stocks %d, research setups %d, %.0f KB, %.1fs" % (len(stocks), len(setups), (D / "brain.json").stat().st_size / 1024, time.time() - t0))


if __name__ == "__main__":
    main()
