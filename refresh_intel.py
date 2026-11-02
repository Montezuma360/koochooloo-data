"""Koochooloo intelligence layer: free public feeds beyond prices, each refreshed as often as its source changes.

Run it as often as you like (every 5-minute live tick and every hourly backup run). Every source has its own minimum
gap, so slow sources are not over-polled, and one failing source never stops the rest. Sources that need a free API key
switch on by themselves when the key is added as a GitHub secret (FINNHUB_KEY, ETHERSCAN_KEY; COINGECKO_KEY is optional).

Sources (all free; news is stored as headline + link only, never article text):
- Crypto: CoinGecko (top 100, global market, trending, sector performance), exchange prices from Kraken, Coinbase and Binance's
  market-data API, Kraken perpetual futures (funding rates, open interest), Alternative.me crypto fear & greed
- Bitcoin on-chain: mempool.space (fees, mempool, hashrate, difficulty), blockchain.com charts; Ethereum via Etherscan (key)
- Futures and FX quotes via Yahoo (oil, gold, silver, copper, grains, index and bond futures, major currency pairs)
- ECB reference exchange rates; macro releases from BLS (US), ONS (UK), Bank of England, ECB
- Economic calendar (Forex Factory weekly export)
- News headlines (RSS): CNBC, MarketWatch, Investing.com, Benzinga, Yahoo Finance, CoinDesk, Cointelegraph, Decrypt,
  The Block; official: Federal Reserve, SEC, ECB, Bank of England, FCA
- SEC EDGAR live filings: 8-K material events (with item meanings and red flags), 10-K, 10-Q, Schedule 13D activist stakes
- Options (Cboe delayed quotes): put/call volume and open interest, near-term implied volatility, unusual contracts
- Koochooloo fear & greed: built the way CNN's index is built (momentum, breadth, volatility, VIX term structure,
  safe-haven demand, junk-bond spread, and put/call once enough history exists), from public data
- Sentiment: StockTwits trending, Reddit mentions by forum (r/wallstreetbets, r/stocks, r/options, r/investing,
  r/CryptoCurrency via ApeWisdom)
- Finnhub (key): earnings and IPO calendars, analyst recommendation trends

Writes data/intel/<source>.json and data/intel/status.json; permanent history under data/history/intel/."""
import datetime as dt, email.utils, html, json, math, os, pathlib, re, statistics, sys, time, traceback
import urllib.error, urllib.request, xml.etree.ElementTree as ET

D = pathlib.Path("data")
OUT = D / "intel"
HIST = D / "history" / "intel"
OUT.mkdir(parents=True, exist_ok=True)
NOW = dt.datetime.now(dt.timezone.utc)
STAMP = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
UA = {"User-Agent": "KoochoolooResearch github.com/Montezuma360/koochooloo-data"}
SEC_UA = {"User-Agent": os.environ.get("SEC_CONTACT") or "KoochoolooResearch github.com/Montezuma360/koochooloo-data"}
FINNHUB_KEY = os.environ.get("FINNHUB_KEY")
ETHERSCAN_KEY = os.environ.get("ETHERSCAN_KEY")
COINGECKO_KEY = os.environ.get("COINGECKO_KEY")  # optional free "Demo" key: higher, steadier rate limit
MEGA = {"AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon", "GOOGL": "Alphabet|Google", "META": "Meta Platforms|Meta",
        "TSLA": "Tesla", "AVGO": "Broadcom", "BRK.B": "Berkshire", "JPM": "JPMorgan", "LLY": "Eli Lilly|Lilly", "V": "Visa",
        "XOM": "Exxon", "UNH": "UnitedHealth", "MA": "Mastercard", "COST": "Costco", "NFLX": "Netflix", "AMD": "AMD",
        "PLTR": "Palantir", "ORCL": "Oracle"}
COINS = {"BTC": "Bitcoin|BTC", "ETH": "Ethereum|Ether|ETH", "SOL": "Solana", "XRP": "XRP|Ripple", "BNB": "BNB|Binance Coin",
         "DOGE": "Dogecoin", "ADA": "Cardano", "TRX": "Tron", "LINK": "Chainlink", "AVAX": "Avalanche"}


# ---------- plumbing ----------
def get(url, headers=None, data=None, tries=2, timeout=40):
    h = dict(UA, **(headers or {}))
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 451) or i == tries - 1:
                raise
            if e.code == 429:  # rate limited (CoinGecko's free tier is shared by everyone on GitHub's servers): wait, then retry once
                time.sleep(15)
                continue
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(2 * (i + 1))


def jget(url, **k):
    return json.loads(get(url, **k))


def load(path, default=None):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return default


def save(name, obj):
    obj = dict({"updated": STAMP}, **obj)
    (OUT / (name + ".json")).write_text(json.dumps(obj, indent=1, default=str))


def history(name, rows):
    """Append rows (dicts) to data/history/intel/<name>/YYYY/YYYY-MM-DD.jsonl: the brain's long-run memory."""
    if not rows:
        return
    f = HIST / name / str(NOW.year) / (NOW.strftime("%Y-%m-%d") + ".jsonl")
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str, separators=(",", ":")) + "\n")


def fnum(x, nd=4):
    try:
        v = float(x)
        return None if math.isnan(v) else round(v, nd)
    except (TypeError, ValueError):
        return None


def market_open():
    from zoneinfo import ZoneInfo
    et = NOW.astimezone(ZoneInfo("America/New_York"))
    return et.weekday() < 5 and dt.time(9, 30) <= et.time() <= dt.time(16, 15)


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


# ---------- crypto ----------
def cg(path):
    """CoinGecko public API; uses the free Demo key when the COINGECKO_KEY secret exists."""
    return jget("https://api.coingecko.com/api/v3" + path, headers={"x-cg-demo-api-key": COINGECKO_KEY} if COINGECKO_KEY else None)


def crypto():
    mk = cg("/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=100&page=1&price_change_percentage=1h,24h,7d")
    top = [{"rank": c.get("market_cap_rank"), "symbol": (c.get("symbol") or "").upper(), "name": c.get("name"), "price": c.get("current_price"),
            "market_cap": c.get("market_cap"), "volume_24h": c.get("total_volume"),
            "change_1h_pct": fnum(c.get("price_change_percentage_1h_in_currency"), 2),
            "change_24h_pct": fnum(c.get("price_change_percentage_24h_in_currency"), 2),
            "change_7d_pct": fnum(c.get("price_change_percentage_7d_in_currency"), 2)} for c in mk]
    g = cg("/global")["data"]
    glob = {"total_market_cap_usd": fnum(g["total_market_cap"]["usd"], 0), "total_volume_usd": fnum(g["total_volume"]["usd"], 0),
            "btc_dominance_pct": fnum(g["market_cap_percentage"].get("btc"), 2), "eth_dominance_pct": fnum(g["market_cap_percentage"].get("eth"), 2),
            "market_cap_change_24h_pct": fnum(g.get("market_cap_change_percentage_24h_usd"), 2)}
    tr = cg("/search/trending").get("coins", [])
    trending = [{"symbol": c["item"].get("symbol"), "name": c["item"].get("name"), "rank": c["item"].get("market_cap_rank")} for c in tr[:15]]
    ex = {}
    for name, fn in (("kraken", _kraken), ("coinbase", _coinbase), ("binance", _binance)):
        try:
            ex[name] = fn()
        except Exception as e:
            ex[name] = {"error": repr(e)[:120]}
    spread = {}
    for coin in ("BTC", "ETH", "SOL"):
        px = [v[coin] for v in ex.values() if isinstance(v, dict) and v.get(coin)]
        if len(px) >= 2:
            spread[coin] = round(100 * (max(px) / min(px) - 1), 3)
    save("crypto", {"source": "CoinGecko, Kraken, Coinbase, Binance (market-data API)", "global": glob, "top100": top,
                    "trending": trending, "exchange_prices": ex, "exchange_spread_pct": spread})
    by = {c["symbol"]: c for c in top}
    history("crypto", [{"t": STAMP, "btc": by.get("BTC", {}).get("price"), "eth": by.get("ETH", {}).get("price"),
                        "sol": by.get("SOL", {}).get("price"), **glob}])
    return "%d coins, %d trending, %d exchanges" % (len(top), len(trending), sum("error" not in v for v in ex.values()))


def _kraken():
    r = jget("https://api.kraken.com/0/public/Ticker?pair=XBTUSD,ETHUSD,SOLUSD")["result"]
    m = {"XXBTZUSD": "BTC", "XETHZUSD": "ETH", "SOLUSD": "SOL"}
    return {m[k]: float(v["c"][0]) for k, v in r.items() if k in m}


def _coinbase():
    out = {}
    for c in ("BTC", "ETH", "SOL"):
        out[c] = float(jget("https://api.exchange.coinbase.com/products/%s-USD/ticker" % c)["price"])
    return out


def _binance():
    r = jget("https://data-api.binance.vision/api/v3/ticker/price?symbols=%5B%22BTCUSDT%22,%22ETHUSDT%22,%22SOLUSDT%22%5D")
    return {x["symbol"][:-4]: float(x["price"]) for x in r}


def crypto_sectors():
    """CoinGecko categories: how each crypto sector or narrative (DeFi, Layer 1, AI, memes, real-world assets...) is doing."""
    c = cg("/coins/categories?order=market_cap_desc")
    rows = [{"id": x.get("id"), "name": x.get("name"), "market_cap": fnum(x.get("market_cap"), 0),
             "change_24h_pct": fnum(x.get("market_cap_change_24h"), 2), "volume_24h": fnum(x.get("volume_24h"), 0),
             "top_coins": x.get("top_3_coins_id") or []} for x in c if x.get("market_cap")]
    big = [r for r in rows if (r["market_cap"] or 0) >= 500e6 and r["change_24h_pct"] is not None]  # ignore tiny, noisy sectors
    by_move = sorted(big, key=lambda r: r["change_24h_pct"])
    save("crypto_sectors", {"source": "CoinGecko categories (sectors and narratives)", "largest": rows[:40],
                            "best_24h": by_move[::-1][:15], "worst_24h": by_move[:15], "sectors_counted": len(rows)})
    history("crypto_sectors", [{"t": STAMP, **{r["id"]: [r["market_cap"], r["change_24h_pct"]] for r in rows[:40]}}])
    return "%d sectors; best %s %+.1f%%, worst %s %+.1f%%" % (len(rows), by_move[-1]["name"], by_move[-1]["change_24h_pct"],
                                                           by_move[0]["name"], by_move[0]["change_24h_pct"])


def crypto_derivatives():
    t = jget("https://futures.kraken.com/derivatives/api/v3/tickers")["tickers"]
    out = {}
    for x in t:
        if x.get("symbol") in ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD"):
            c = {"PF_XBTUSD": "BTC", "PF_ETHUSD": "ETH", "PF_SOLUSD": "SOL"}[x["symbol"]]
            rel = x.get("relativeFundingRate")
            out[c] = {"mark": fnum(x.get("markPrice"), 2), "open_interest": fnum(x.get("openInterest"), 2),
                      "volume_24h": fnum(x.get("vol24h"), 2), "change_24h_pct": fnum(x.get("change24h"), 2),
                      "funding_rate_hourly": fnum(rel, 8),
                      "funding_annualised_pct": fnum(100 * rel * 24 * 365, 2) if rel is not None else None,
                      "premium_to_index_pct": fnum(100 * (x["markPrice"] / x["indexPrice"] - 1), 3) if x.get("indexPrice") else None}
    save("crypto_derivatives", {"source": "Kraken Futures perpetual contracts", "note":
         "Positive funding = traders paying to stay long (crowded optimism); negative = crowded pessimism.", "perpetuals": out})
    history("crypto_derivatives", [{"t": STAMP, **{k: {"f": v["funding_annualised_pct"], "oi": v["open_interest"]} for k, v in out.items()}}])
    return "%d perpetuals" % len(out)


def crypto_fear_greed():
    d = jget("https://api.alternative.me/fng/?limit=31")["data"]
    pts = [{"date": dt.datetime.fromtimestamp(int(x["timestamp"]), dt.timezone.utc).strftime("%Y-%m-%d"), "value": int(x["value"]),
            "label": x["value_classification"]} for x in d]
    save("crypto_fear_greed", {"source": "alternative.me Crypto Fear & Greed Index", "now": pts[0], "yesterday": pts[1] if len(pts) > 1 else None,
                               "week_ago": pts[7] if len(pts) > 7 else None, "month_avg": round(statistics.mean(p["value"] for p in pts[:30]), 1),
                               "history": pts})
    return "%d (%s)" % (pts[0]["value"], pts[0]["label"])


# ---------- on-chain ----------
def onchain():
    base = "https://mempool.space/api"
    fees = jget(base + "/v1/fees/recommended")
    mp = jget(base + "/mempool")
    diff = jget(base + "/v1/difficulty-adjustment")
    hr = jget(base + "/v1/mining/hashrate/1w")
    tip = int(get(base + "/blocks/tip/height"))
    btc = {"block_height": tip, "fees_sat_per_vb": fees, "mempool_tx": mp.get("count"), "mempool_vsize_mb": fnum(mp.get("vsize", 0) / 1e6, 2),
           "mempool_fees_btc": fnum(mp.get("total_fee", 0) / 1e8, 4), "hashrate_eh_s": fnum((hr.get("currentHashrate") or 0) / 1e18, 1),
           "difficulty": hr.get("currentDifficulty"), "next_difficulty_change_pct": fnum(diff.get("difficultyChange"), 2),
           "next_retarget_in_blocks": diff.get("remainingBlocks")}
    eth = None
    if ETHERSCAN_KEY:
        try:
            g = jget("https://api.etherscan.io/v2/api?chainid=1&module=gastracker&action=gasoracle&apikey=" + ETHERSCAN_KEY)["result"]
            s = jget("https://api.etherscan.io/v2/api?chainid=1&module=stats&action=ethsupply2&apikey=" + ETHERSCAN_KEY)["result"]
            eth = {"gas_gwei": {"safe": g.get("SafeGasPrice"), "normal": g.get("ProposeGasPrice"), "fast": g.get("FastGasPrice")},
                   "supply_eth": fnum(int(s["EthSupply"]) / 1e18, 0), "staked_eth": fnum(int(s["Eth2Staking"]) / 1e18, 0),
                   "burnt_fees_eth": fnum(int(s["BurntFees"]) / 1e18, 0)}
        except Exception as e:
            eth = {"error": repr(e)[:120]}
    save("onchain", {"source": "mempool.space" + (", Etherscan" if ETHERSCAN_KEY else ""), "bitcoin": btc, "ethereum": eth or "add ETHERSCAN_KEY secret to switch on"})
    history("onchain", [{"t": STAMP, "h": tip, "fee": fees.get("halfHourFee"), "mp": mp.get("count"), "hr": btc["hashrate_eh_s"]}])
    return "block %d, %s sat/vB" % (tip, fees.get("halfHourFee"))


def onchain_daily():
    out = {}
    for chart in ("n-transactions", "estimated-transaction-volume-usd", "n-unique-addresses", "miners-revenue", "hash-rate"):
        try:
            v = jget("https://api.blockchain.info/charts/%s?timespan=60days&format=json" % chart)["values"]
            out[chart] = [[dt.datetime.fromtimestamp(p["x"], dt.timezone.utc).strftime("%Y-%m-%d"), fnum(p["y"], 2)] for p in v]
        except Exception as e:
            out[chart] = {"error": repr(e)[:100]}
    save("onchain_daily", {"source": "blockchain.com charts (Bitcoin, daily)", "charts": out})
    return "%d charts" % sum(isinstance(v, list) for v in out.values())


# ---------- futures, FX, macro ----------
FUTURES = {"ES=F": "S&P 500 futures", "NQ=F": "Nasdaq 100 futures", "YM=F": "Dow futures", "RTY=F": "Russell 2000 futures",
           "ZN=F": "10-year Treasury note futures", "ZB=F": "30-year Treasury bond futures", "CL=F": "WTI crude oil", "BZ=F": "Brent crude oil",
           "NG=F": "Natural gas", "RB=F": "Gasoline", "GC=F": "Gold", "SI=F": "Silver", "HG=F": "Copper", "PL=F": "Platinum",
           "ZC=F": "Corn", "ZW=F": "Wheat", "ZS=F": "Soybeans", "KC=F": "Coffee", "CC=F": "Cocoa", "LE=F": "Live cattle",
           "GBPUSD=X": "Pound / dollar", "EURUSD=X": "Euro / dollar", "USDJPY=X": "Dollar / yen", "AUDUSD=X": "Australian dollar / dollar",
           "USDCAD=X": "Dollar / Canadian dollar", "USDCHF=X": "Dollar / Swiss franc", "USDCNY=X": "Dollar / yuan", "EURGBP=X": "Euro / pound",
           "DX-Y.NYB": "US dollar index", "^TNX": "US 10-year yield", "^MOVE": "Bond volatility (MOVE)"}


def futures_fx():
    import yfinance as yf
    df = yf.download(list(FUTURES), period="10d", interval="1d", auto_adjust=False, progress=False, threads=True)["Close"]
    rows = []
    for sym, label in FUTURES.items():
        if sym not in df:
            continue
        s = df[sym].dropna()
        if len(s) < 2:
            continue
        rows.append({"symbol": sym, "name": label, "last": fnum(s.iloc[-1]), "change_1d_pct": fnum(100 * (s.iloc[-1] / s.iloc[-2] - 1), 2),
                     "change_5d_pct": fnum(100 * (s.iloc[-1] / s.iloc[-6] - 1), 2) if len(s) >= 6 else None, "as_of": str(s.index[-1].date())})
    save("futures_fx", {"source": "Yahoo Finance (futures delayed by the exchange; personal use)", "rows": rows})
    history("futures_fx", [{"t": STAMP, **{r["symbol"]: r["last"] for r in rows}}])
    return "%d quotes" % len(rows)


def ecb_fx():
    x = get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml").decode()
    day = re.search(r"time=['\"](\d{4}-\d{2}-\d{2})", x).group(1)
    eur = {m[0]: float(m[1]) for m in re.findall(r"currency=['\"](\w{3})['\"]\s+rate=['\"]([\d.]+)", x)}
    eur["EUR"] = 1.0
    gbp = {k: round(v / eur["GBP"], 5) for k, v in eur.items()} if "GBP" in eur else {}
    usd = {k: round(v / eur["USD"], 5) for k, v in eur.items()} if "USD" in eur else {}
    save("ecb_fx", {"source": "European Central Bank reference rates (set about 16:00 CET each working day)", "date": day,
                    "per_euro": eur, "per_pound": gbp, "per_dollar": usd})
    prev = load(HIST / "ecb_fx" / "last.json", {})
    if prev.get("date") != day:
        history("ecb_fx", [{"date": day, **eur}])
        (HIST / "ecb_fx").mkdir(parents=True, exist_ok=True)
        (HIST / "ecb_fx" / "last.json").write_text(json.dumps({"date": day}))
    return "%s, %d currencies" % (day, len(eur))


def _ons(path):
    j = jget("https://www.ons.gov.uk" + path + "/data")
    months = [m for m in j.get("months", []) if m.get("value") not in (None, "")]
    q = [m for m in j.get("quarters", []) if m.get("value") not in (None, "")]
    s = months or q
    return {"title": j.get("description", {}).get("title"), "latest": s[-1]["date"], "value": fnum(s[-1]["value"], 3),
            "previous": fnum(s[-2]["value"], 3) if len(s) > 1 else None, "next_release": j.get("description", {}).get("nextRelease"),
            "history": [[m["date"], fnum(m["value"], 3)] for m in s[-24:]]}


def _ecb(key):
    j = jget("https://data-api.ecb.europa.eu/service/data/%s?format=jsondata&lastNObservations=13" % key)
    ser = list(j["dataSets"][0]["series"].values())[0]["observations"]
    dates = [v["id"] for v in j["structure"]["dimensions"]["observation"][0]["values"]]
    pts = [[dates[int(i)], fnum(v[0], 3)] for i, v in sorted(ser.items(), key=lambda kv: int(kv[0]))]
    return {"latest": pts[-1][0], "value": pts[-1][1], "previous": pts[-2][1] if len(pts) > 1 else None, "history": pts}


def _eurostat_hicp():
    """Euro-area annual inflation. From 2026 Eurostat publishes HICP under ECOICOP version 2 (prc_hicp_minr)."""
    j = jget("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_minr?geo=EA&coicop18=TOTAL&unit=RCH_A&lastTimePeriod=13")
    idx = j["dimension"]["time"]["category"]["index"]
    pts = sorted([t, fnum(j["value"][str(i)], 2)] for t, i in idx.items() if str(i) in j["value"])
    return {"latest": pts[-1][0], "value": pts[-1][1], "previous": pts[-2][1] if len(pts) > 1 else None, "history": pts}


def _ons_growth(path):
    r = _ons(path)
    h = r["history"]
    r["title"] = (r.get("title") or "") + " (index; growth worked out from it)"
    r["growth_pct"] = round(100 * (h[-1][1] / h[-2][1] - 1), 2) if len(h) > 1 and h[-2][1] else None
    r["growth_3m_on_3m_pct"] = round(100 * (sum(v for _, v in h[-3:]) / sum(v for _, v in h[-6:-3]) - 1), 2) if len(h) >= 6 else None
    return r


def macro_global():
    out, errs = {}, {}
    jobs = {
        "uk_cpi_annual_pct": lambda: _ons("/economy/inflationandpriceindices/timeseries/d7g7/mm23"),
        "uk_unemployment_pct": lambda: _ons("/employmentandlabourmarket/peoplenotinwork/unemployment/timeseries/mgsx/lms"),
        "uk_gdp_monthly": lambda: _ons_growth("/economy/grossdomesticproductgdp/timeseries/ecy2/mgdp"),
        "euro_deposit_rate_pct": lambda: _ecb("FM/B.U2.EUR.4F.KR.DFR.LEV"),
        "euro_inflation_annual_pct": _eurostat_hicp,
    }
    for k, fn in jobs.items():
        try:
            out[k] = fn()
        except Exception as e:
            errs[k] = repr(e)[:120]
    try:  # Bank of England: Bank Rate and the 10-year gilt (nominal zero-coupon) yield
        start = (NOW - dt.timedelta(days=400)).strftime("%d/%b/%Y")
        csv = get("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=%s&Dateto=now"
                  "&SeriesCodes=IUDBEDR,IUDMNZC&CSVF=TN&UsingCodes=Y" % start).decode()
        rows = [l.split(",") for l in csv.strip().splitlines()[1:]]
        for col, name in ((1, "uk_bank_rate_pct"), (2, "uk_10y_gilt_yield_pct")):
            pts = [[dt.datetime.strptime(r[0], "%d %b %Y").strftime("%Y-%m-%d"), fnum(r[col], 3)] for r in rows if len(r) > col and r[col]]
            out[name] = {"latest": pts[-1][0], "value": pts[-1][1], "history": pts[-260:]}
    except Exception as e:
        errs["bank_of_england"] = repr(e)[:120]
    try:  # BLS: v1 public API (no key), one request for four headline series
        body = json.dumps({"seriesid": ["CUUR0000SA0", "LNS14000000", "CES0000000001", "CES0500000003"]}).encode()
        j = json.loads(get("https://api.bls.gov/publicAPI/v1/timeseries/data/", data=body, headers={"Content-Type": "application/json"}))
        names = {"CUUR0000SA0": "us_cpi_index", "LNS14000000": "us_unemployment_pct", "CES0000000001": "us_payrolls_thousands",
                 "CES0500000003": "us_avg_hourly_earnings"}
        for s in j["Results"]["series"]:
            pts = [["%s-%s" % (p["year"], p["period"][1:]), fnum(p["value"], 3)] for p in s["data"] if p["period"].startswith("M") and p["period"] != "M13"]
            pts.sort()
            item = {"latest": pts[-1][0], "value": pts[-1][1], "previous": pts[-2][1], "history": pts[-24:]}
            if s["seriesID"] == "CUUR0000SA0" and len(pts) >= 13:
                item["inflation_annual_pct"] = round(100 * (pts[-1][1] / pts[-13][1] - 1), 2)
            if s["seriesID"] == "CES0000000001":
                item["jobs_added_thousands"] = round(pts[-1][1] - pts[-2][1], 0)
            out[names[s["seriesID"]]] = item
    except Exception as e:
        errs["bls"] = repr(e)[:120]
    save("macro_global", {"source": "BLS, ONS, Bank of England, ECB (official statistics)", "series": out, "failed": errs})
    if not out:
        raise RuntimeError("all macro sources failed: %s" % errs)
    return "%d series%s" % (len(out), (", %d failed" % len(errs)) if errs else "")


def calendar():
    ev = jget("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    rows = [{"time": e.get("date"), "country": e.get("country"), "event": e.get("title"), "impact": e.get("impact"),
             "forecast": e.get("forecast") or None, "previous": e.get("previous") or None, "actual": e.get("actual") or None} for e in ev]
    def when(r):
        try:
            return dt.datetime.fromisoformat(r["time"])
        except Exception:
            return None
    upcoming = [r for r in rows if when(r) and when(r) >= NOW]
    save("calendar", {"source": "Forex Factory weekly calendar export", "high_impact_upcoming": [r for r in upcoming if r["impact"] == "High"][:25],
                      "this_week": rows})
    return "%d events, %d high-impact still to come" % (len(rows), sum(r["impact"] == "High" for r in upcoming))


# ---------- news ----------
FEEDS = {
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "CNBC Markets": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "Investing.com": "https://www.investing.com/rss/news.rss",
    "Benzinga": "https://www.benzinga.com/feed",
    "Yahoo Finance": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US" % ",".join(list(MEGA)[:10]).replace(".", "-"),
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
    "SEC": "https://www.sec.gov/news/pressreleases.rss",
    "ECB": "https://www.ecb.europa.eu/rss/press.html",
    "Bank of England": "https://www.bankofengland.co.uk/rss/news",
    "FCA": "https://www.fca.org.uk/news/rss.xml",
}
OFFICIAL = {"Federal Reserve", "SEC", "ECB", "Bank of England", "FCA"}
CRYPTO_NEWS = {"CoinDesk", "Cointelegraph", "Decrypt", "The Block"}
TAGS = [(t, re.compile(r"\b(%s)\b" % pat)) for t, pat in MEGA.items()] + [(c, re.compile(r"\b(%s)\b" % pat)) for c, pat in COINS.items()]
CASHTAG = re.compile(r"(?:\$|\((?:NASDAQ|NYSE|NYSEARCA|AMEX|Nasdaq|NYSE American):\s?)([A-Z]{1,5}(?:\.[A-Z])?)\b")


def _parse_feed(src, raw):
    raw = raw.lstrip(b"\xef\xbb\xbf \r\n\t")
    root = ET.fromstring(raw)
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out = []
    for it in items[:60]:
        def tx(*names):
            for n in names:
                e = it.find(n)
                if e is not None:
                    return e.text or e.get("href") or ""
            return ""
        title = strip_tags(tx("title", "{http://www.w3.org/2005/Atom}title"))
        link = tx("link", "{http://www.w3.org/2005/Atom}link").strip()
        when = tx("pubDate", "{http://purl.org/dc/elements/1.1/}date", "{http://www.w3.org/2005/Atom}updated", "{http://www.w3.org/2005/Atom}published")
        try:
            t = email.utils.parsedate_to_datetime(when).astimezone(dt.timezone.utc)
        except Exception:
            try:
                t = dt.datetime.fromisoformat(when.strip().replace("Z", "+00:00")).astimezone(dt.timezone.utc)
            except Exception:
                try:  # FCA style: "Monday, November 2, 2026 - 12:12" (UK time)
                    from zoneinfo import ZoneInfo
                    t = dt.datetime.strptime(when.strip(), "%A, %B %d, %Y - %H:%M").replace(tzinfo=ZoneInfo("Europe/London")).astimezone(dt.timezone.utc)
                except Exception:
                    t = None
        if not title or not link:
            continue
        tags = sorted({k for k, rx in TAGS if rx.search(title)} | set(CASHTAG.findall(title)))
        row = {"time": (t or NOW).strftime("%Y-%m-%dT%H:%M:%SZ"), "source": src, "title": title[:240], "link": link,
               "kind": "official" if src in OFFICIAL else "crypto" if src in CRYPTO_NEWS else "markets", "tags": tags}
        if t is None:
            row["time_is_first_seen"] = True  # the feed gives no publication time
        out.append(row)
    return out


def news():
    prev = load(OUT / "news.json", {}) or {}
    old = prev.get("items", [])
    seen = {i["link"] for i in old} | {i["title"] for i in old}
    got, errs, per = [], {}, {}
    for src, url in FEEDS.items():
        try:
            items = _parse_feed(src, get(url, headers=SEC_UA if "sec.gov" in url else None, timeout=25))
            per[src] = len(items)
            got += items
        except Exception as e:
            errs[src] = repr(e)[:100]
    new = []
    for i in sorted(got, key=lambda i: i["time"]):
        if i["link"] in seen or i["title"] in seen:
            continue
        seen |= {i["link"], i["title"]}
        new.append(i)
    keep_from = (NOW - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")  # older headlines live on in the history files
    items = sorted([i for i in old + new if i["time"] >= keep_from], key=lambda i: i["time"], reverse=True)[:400]
    tagcount = {}
    for i in items:
        if i["time"] >= (NOW - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"):
            for t in i["tags"]:
                tagcount[t] = tagcount.get(t, 0) + 1
    save("news", {"source": "RSS headlines and links only (no article text)", "feeds_ok": per, "feeds_failed": errs,
                  "most_mentioned_24h": sorted(tagcount.items(), key=lambda kv: -kv[1])[:20], "items": items})
    history("news", new)
    if not per:
        raise RuntimeError("every news feed failed")
    return "%d new headlines from %d feeds%s" % (len(new), len(per), (", %d feeds failed" % len(errs)) if errs else "")


# ---------- SEC live filings ----------
ITEMS_8K = {"1.01": "Major agreement signed", "1.02": "Major agreement ended", "1.03": "Bankruptcy", "1.05": "Cybersecurity incident",
            "2.01": "Acquisition or sale completed", "2.02": "Results announced", "2.03": "New debt", "2.04": "Debt acceleration",
            "2.05": "Restructuring costs", "2.06": "Impairment (write-down)", "3.01": "Delisting warning", "3.02": "Unregistered share sale",
            "3.03": "Shareholder rights changed", "4.01": "Auditor changed", "4.02": "Past results no longer reliable",
            "5.01": "Change of control", "5.02": "Director or executive change", "5.03": "Bylaws changed", "5.07": "Shareholder vote results",
            "7.01": "Regulation FD disclosure", "8.01": "Other events", "9.01": "Financial statements and exhibits"}
RED_FLAGS = {"1.03", "1.05", "2.04", "2.06", "3.01", "4.01", "4.02"}
FORMS = {"8-K": 100, "SCHEDULE 13D": 40, "10-K": 40, "10-Q": 40}


def _cik_map():
    f = OUT / "cik_tickers.json"
    m = load(f, None)
    if m and m.get("updated", "") >= (NOW - dt.timedelta(days=7)).strftime("%Y-%m-%d"):
        return {int(k): v for k, v in m["map"].items()}
    raw = jget("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA)
    mp = {}
    for r in raw.values():
        mp.setdefault(int(r["cik_str"]), r["ticker"].upper().replace("-", "."))
    f.write_text(json.dumps({"updated": STAMP, "map": mp}))
    return mp


def sec_filings():
    cik2t = _cik_map()
    prev = load(OUT / "sec_filings.json", {}) or {}
    old = prev.get("filings", [])
    seen = {f["accession"] + f.get("role", "") for f in old}
    new = []
    for form, n in FORMS.items():
        x = get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=%s&company=&dateb=&owner=include&start=0&count=%d&output=atom"
                % (form.replace(" ", "+"), n), headers=SEC_UA).decode("latin-1")
        for e in re.findall(r"<entry>(.*?)</entry>", x, re.S):
            m = re.search(r"<title>(.*?) - (.*?) \((\d{10})\) \((\w[\w ]*)\)</title>", e)
            acc = re.search(r"accession-number=([\d-]+)", e)
            if not (m and acc):
                continue
            ftype, name, cik, role = html.unescape(m.group(1)), html.unescape(m.group(2)), int(m.group(3)), m.group(4)
            key = acc.group(1) + role
            if key in seen:
                continue
            seen.add(key)
            summ = html.unescape(re.search(r"<summary[^>]*>(.*?)</summary>", e, re.S).group(1))
            items = re.findall(r"Item (\d+\.\d+)", summ)
            link = re.search(r'href="([^"]+)"', e).group(1)
            when = re.search(r"<updated>(.*?)</updated>", e).group(1)
            flags = RED_FLAGS & set(items)
            if "3.01" in flags and ({"2.01", "5.01"} & set(items)):  # delisting because a takeover completed: not a warning sign
                flags.discard("3.01")
            new.append({"time": dt.datetime.fromisoformat(when).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "form": ftype, "company": name, "cik": cik, "ticker": cik2t.get(cik), "role": role, "accession": acc.group(1),
                        "items": [{"item": i, "meaning": ITEMS_8K.get(i, "")} for i in items],
                        "red_flag": sorted(flags), "link": link})
        time.sleep(0.2)  # SEC fair access: well under 10 requests a second
    keep_from = (NOW - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")  # older filings live on in the history files
    allf = sorted([f for f in old + new if f["time"] >= keep_from], key=lambda f: f["time"], reverse=True)
    listed = [f for f in allf if f["ticker"]]
    act = [f for f in allf if f["form"].startswith("SCHEDULE 13D") and f["role"] == "Subject"]
    save("sec_filings", {"source": "SEC EDGAR live filings (8-K, 10-K, 10-Q, Schedule 13D)",
                         "red_flags_3d": [f for f in listed if f["red_flag"]][:50],
                         "results_announced_3d": [f for f in listed if any(i["item"] == "2.02" for i in f["items"])][:100],
                         "activist_stakes_3d": act[:60], "filings": allf[:800]})
    history("sec_filings", new)
    return "%d new filings (%d with a red flag)" % (len(new), sum(1 for f in new if f["red_flag"]))


# ---------- options ----------
OPT_SYMBOLS = ["SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META", "AMD", "GOOGL", "PLTR"]
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def options():
    out, unusual, errs = {}, [], {}
    for sym in OPT_SYMBOLS:
        try:
            d = jget("https://cdn.cboe.com/api/global/delayed_quotes/options/%s.json" % sym, timeout=60)["data"]
            spot = d.get("current_price")
            cv = pv = coi = poi = 0
            near = {}
            for o in d["options"]:
                m = OCC.match(o["option"])
                if not m:
                    continue
                exp = dt.datetime.strptime(m.group(2), "%y%m%d").date()
                kind, strike = m.group(3), int(m.group(4)) / 1000
                vol, oi = o.get("volume") or 0, o.get("open_interest") or 0
                if kind == "C":
                    cv += vol; coi += oi
                else:
                    pv += vol; poi += oi
                days = (exp - NOW.date()).days
                if 7 <= days <= 45 and spot and o.get("iv"):
                    near.setdefault(exp, []).append((abs(strike - spot), o["iv"]))
                price = o.get("last_trade_price") or 0
                # unusual = fresh positioning: volume far above existing open interest, real money, and not same-day hedging
                if days >= 2 and vol >= 1000 and vol > 5 * max(oi, 1) and vol * price * 100 >= 250_000:
                    unusual.append({"symbol": sym, "contract": o["option"], "type": "call" if kind == "C" else "put", "strike": strike,
                                    "expiry": str(exp), "volume": int(vol), "open_interest": int(oi), "last": price,
                                    "notional_usd": round(vol * price * 100), "iv": fnum(o.get("iv"), 3)})
            atm_iv = None
            if near:
                exp = min(near)
                atm = sorted(near[exp])[:4]
                atm_iv = round(100 * statistics.mean(v for _, v in atm), 1)
            out[sym] = {"spot": spot, "call_volume": int(cv), "put_volume": int(pv), "put_call_volume": round(pv / cv, 3) if cv else None,
                        "put_call_open_interest": round(poi / coi, 3) if coi else None, "near_term_atm_iv_pct": atm_iv, "iv30": d.get("iv30")}
        except Exception as e:
            errs[sym] = repr(e)[:100]
    tv = sum(v["call_volume"] for v in out.values()); tp = sum(v["put_volume"] for v in out.values())
    unusual.sort(key=lambda u: -u["notional_usd"])
    agg = round(tp / tv, 3) if tv else None
    save("options", {"source": "Cboe delayed quotes (15-minute delay; personal use, licence needed before charging)", "aggregate_put_call_volume": agg,
                     "symbols": out, "unusual": unusual[:40], "failed": errs})
    history("options", [{"t": STAMP, "pc": agg, **{k: [v["put_call_volume"], v["near_term_atm_iv_pct"]] for k, v in out.items()}}])
    if not out:
        raise RuntimeError("all option chains failed: %s" % errs)
    return "%d chains, put/call %s, %d unusual" % (len(out), agg, len(unusual))


# ---------- Koochooloo fear & greed ----------
def _pct_rank(series, value):
    s = [x for x in series if x is not None and not math.isnan(x)]
    return 100.0 * sum(x <= value for x in s) / len(s) if s else None


def fear_greed():
    import pandas as pd
    import yfinance as yf
    sect = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC"]
    px = yf.download(["^GSPC", "^VIX", "^VIX3M", "SPY", "TLT"] + sect, period="3y", interval="1d", auto_adjust=True, progress=False, threads=True)["Close"].ffill()
    comp = pd.DataFrame(index=px.index)
    comp["momentum"] = px["^GSPC"] / px["^GSPC"].rolling(125).mean() - 1                       # higher = greed
    comp["breadth"] = sum((px[s] > px[s].rolling(50).mean()).astype(float) for s in sect) / len(sect)
    comp["volatility"] = -(px["^VIX"] / px["^VIX"].rolling(50).mean() - 1)                      # VIX above its average = fear
    comp["vix_term"] = -(px["^VIX"] / px["^VIX3M"])                                              # VIX above 3-month VIX = fear
    comp["safe_haven"] = px["SPY"].pct_change(20) - px["TLT"].pct_change(20)                     # stocks beating bonds = greed
    try:
        txt = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2").decode()
        hy = pd.Series({pd.Timestamp(l.split(",")[0]): float(l.split(",")[1]) for l in txt.splitlines()[1:] if l.split(",")[1] not in ("", ".")})
        comp["junk_bonds"] = -hy.reindex(comp.index).ffill()                                      # wider spread = fear
    except Exception:
        pass
    comp = comp.dropna(how="all")
    window = 252
    scores = pd.DataFrame(index=comp.index[-90:], columns=comp.columns, dtype=float)
    for c in comp.columns:
        s = comp[c]
        for t in scores.index:
            hist = s.loc[:t].iloc[-window:]
            if hist.notna().sum() >= 120 and not math.isnan(s.loc[t]):
                scores.loc[t, c] = _pct_rank(hist.tolist(), s.loc[t])
    pc = None
    hist_opt = sorted((HIST / "options").glob("*/*.jsonl"))
    daily = {}
    for f in hist_opt[-90:]:
        for line in f.read_text().splitlines():
            r = json.loads(line)
            if r.get("pc"):
                daily[r["t"][:10]] = r["pc"]
    if len(daily) >= 20:
        vals = list(daily.values())
        pc = 100 - _pct_rank(vals, statistics.mean(vals[-5:]))
    idx = scores.mean(axis=1, skipna=True)
    now_parts = {c: round(float(scores[c].iloc[-1]), 1) for c in scores.columns if not math.isnan(scores[c].iloc[-1])}
    if pc is not None:
        now_parts["put_call"] = round(pc, 1)
    val = round(statistics.mean(now_parts.values()), 1)

    def label(v):
        return "Extreme fear" if v < 25 else "Fear" if v < 45 else "Neutral" if v <= 55 else "Greed" if v <= 75 else "Extreme greed"
    hist = [[str(t.date()), round(float(v), 1)] for t, v in idx.items() if not math.isnan(v)]
    save("fear_greed", {"source": "Koochooloo Fear & Greed, built the way CNN's index is built, from public data (Yahoo, FRED, Cboe)",
                        "value": val, "label": label(val), "components_0_to_100": now_parts,
                        "week_ago": hist[-6][1] if len(hist) >= 6 else None, "month_ago": hist[-22][1] if len(hist) >= 22 else None,
                        "history_90d": hist, "how": "Each part is ranked against its own past year: 0 = most fearful reading, 100 = most greedy. "
                        "Put/call joins once 20 days of our own options history exist."})
    history("fear_greed", [{"t": STAMP, "v": val, **now_parts}])
    return "%s (%s), %d parts" % (val, label(val), len(now_parts))


# ---------- sentiment ----------
def stocktwits():
    s = jget("https://api.stocktwits.com/api/2/trending/symbols.json")["symbols"]
    rows = [{"symbol": x.get("symbol"), "name": x.get("title"), "class": x.get("instrument_class"), "trending_score": x.get("trending_score"),
             "watchers": x.get("watchlist_count"), "why": ((x.get("trends") or {}).get("summary") or "")[:300]} for x in s[:30]]
    save("stocktwits", {"source": "StockTwits trending symbols", "trending": rows})
    history("stocktwits", [{"t": STAMP, "top": [r["symbol"] for r in rows]}])
    return "%d trending" % len(rows)


SUBS = {"wallstreetbets": "r/wallstreetbets", "stocks": "r/stocks", "options": "r/options", "investing": "r/investing", "CryptoCurrency": "r/CryptoCurrency"}


def reddit_forums():
    out, errs = {}, {}
    for k, label in SUBS.items():
        try:
            r = jget("https://apewisdom.io/api/v1.0/filter/%s/page/1" % k).get("results", [])
            out[label] = [{"symbol": x.get("ticker"), "name": html.unescape(x.get("name") or ""), "mentions": x.get("mentions"),
                           "mentions_24h_ago": x.get("mentions_24h_ago"), "upvotes": x.get("upvotes")} for x in r[:25]]
        except Exception as e:
            errs[label] = repr(e)[:100]
    save("reddit_forums", {"source": "ApeWisdom (counts of mentions on Reddit forums)", "forums": out, "failed": errs})
    history("reddit_forums", [{"t": STAMP, **{k: [(x["symbol"], x["mentions"]) for x in v[:10]] for k, v in out.items()}}])
    if not out:
        raise RuntimeError(str(errs))
    return "%d forums" % len(out)


# ---------- Finnhub (free key) ----------
def finnhub():
    if not FINNHUB_KEY:
        return "skipped: add FINNHUB_KEY secret to switch on"
    k = "&token=" + FINNHUB_KEY
    f, t = NOW.date(), NOW.date() + dt.timedelta(days=14)
    earn = jget("https://finnhub.io/api/v1/calendar/earnings?from=%s&to=%s%s" % (f, t, k)).get("earningsCalendar", [])
    ipo = jget("https://finnhub.io/api/v1/calendar/ipo?from=%s&to=%s%s" % (f - dt.timedelta(days=7), t + dt.timedelta(days=16), k)).get("ipoCalendar", [])
    rec = {}
    for s in list(MEGA):
        try:
            r = jget("https://finnhub.io/api/v1/stock/recommendation?symbol=%s%s" % (s.replace(".", "-"), k))
            if r:
                rec[s] = r[0]
        except Exception:
            pass
        time.sleep(1.1)  # free tier: 60 calls a minute
    save("finnhub", {"source": "Finnhub (free tier)", "earnings_next_14d": earn[:400], "ipos": ipo[:100], "analyst_recommendations": rec})
    return "%d earnings, %d IPOs, %d recommendation sets" % (len(earn), len(ipo), len(rec))


# ---------- runner ----------
# name: (function, minimum minutes between fetches when the US market is open, ... when it is closed)
SOURCES = {
    "crypto": (crypto, 5, 5),
    "crypto_sectors": (crypto_sectors, 30, 30),
    "crypto_derivatives": (crypto_derivatives, 5, 5),
    "crypto_fear_greed": (crypto_fear_greed, 60, 60),
    "onchain": (onchain, 10, 10),
    "onchain_daily": (onchain_daily, 360, 360),
    "futures_fx": (futures_fx, 5, 15),
    "ecb_fx": (ecb_fx, 120, 120),
    "macro_global": (macro_global, 360, 360),
    "calendar": (calendar, 60, 120),
    "news": (news, 5, 10),
    "sec_filings": (sec_filings, 5, 15),
    "options": (options, 15, 720),
    "fear_greed": (fear_greed, 15, 120),
    "stocktwits": (stocktwits, 15, 30),
    "reddit_forums": (reddit_forums, 60, 60),
    "finnhub": (finnhub, 360, 360),
}


def main():
    force = set(a for a in sys.argv[1:] if a in SOURCES) or (set(SOURCES) if "--all" in sys.argv else set())
    st = load(OUT / "status.json", None) or {}
    src = st.get("sources", {})
    is_open = market_open()
    for name, (fn, gap_open, gap_closed) in SOURCES.items():
        s = src.setdefault(name, {})
        gap = gap_open if is_open else gap_closed
        last = s.get("last_try")
        if name not in force and last and (NOW - dt.datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() < gap * 60 - 45:
            continue
        t0 = time.time()
        s["last_try"] = STAMP
        try:
            s["result"] = fn()
            s["last_ok"] = STAMP
            s.pop("error", None)
        except Exception as e:
            s["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
            traceback.print_exc()
        s["seconds"] = round(time.time() - t0, 1)
        s["every_minutes"] = gap
        print("%-20s %5.1fs  %s" % (name, s["seconds"], s.get("error") or s.get("result")), flush=True)
    failed = {k: v["error"] for k, v in src.items() if v.get("error")}
    (OUT / "status.json").write_text(json.dumps({"updated": STAMP, "market_open": is_open, "failed": failed, "sources": src}, indent=1))


if __name__ == "__main__":
    main()
