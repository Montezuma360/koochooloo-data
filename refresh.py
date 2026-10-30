"""Koochooloo price engine v3. Fast pulls (quotes, 5-minute bars, movers) run every time.
Slow pulls (daily history, sector board, Treasury rates) run once an hour.
Every pull is wrapped so one failure never stops the rest. data/status.json says what worked."""
import json, datetime, pathlib, traceback
import pandas as pd
import yfinance as yf

OUT = pathlib.Path("data")
OUT.mkdir(exist_ok=True)
NOW = datetime.datetime.utcnow().replace(microsecond=0)
now = NOW.isoformat() + "Z"
status_path = OUT / "status.json"
prev = json.loads(status_path.read_text()) if status_path.exists() else {}
status = {"updated": now, "ok": {}, "failed": {}, "slow_last": prev.get("slow_last")}

INDEXES = ["SPY", "QQQ", "IWM", "DIA", "^GSPC", "^IXIC", "^VIX", "^FTSE", "^GDAXI", "^N225"]
SECTORS = {"XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health care",
           "XLY": "Consumer discretionary", "XLP": "Consumer staples", "XLI": "Industrials",
           "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real estate", "XLC": "Communication"}
MACRO = ["GLD", "SLV", "TLT", "HYG", "GC=F", "CL=F", "NG=F", "DX-Y.NYB", "GBPUSD=X", "EURUSD=X", "USDJPY=X"]
CRYPTO = ["BTC-USD", "ETH-USD", "SOL-USD"]
MEGA = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "BRK-B", "JPM", "LLY", "V",
        "XOM", "UNH", "MA", "COST", "NFLX", "AMD", "PLTR", "ORCL"]
TICKERS = INDEXES + list(SECTORS) + MACRO + CRYPTO + MEGA


def save(name, fn):
    try:
        data = fn()
        (OUT / f"{name}.json").write_text(json.dumps({"updated": now, "rows": data}))
        status["ok"][name] = len(data)
    except Exception as e:
        status["failed"][name] = f"{type(e).__name__}: {str(e)[:200]}"
        traceback.print_exc()


def long_rows(df, fmt):
    out = []
    close, vol = df["Close"], df["Volume"]
    for sym in close.columns:
        for ts, c in close[sym].dropna().items():
            v = vol[sym].get(ts)
            out.append({"symbol": sym, "t": ts.strftime(fmt), "close": round(float(c), 4),
                        "volume": None if pd.isna(v) else int(v)})
    return out


def intraday():
    df = yf.download(TICKERS, period="1d", interval="5m", auto_adjust=True, progress=False, threads=True)
    return long_rows(df, "%Y-%m-%d %H:%M")


def quotes():
    df = yf.download(TICKERS, period="5d", interval="1d", auto_adjust=True, progress=False, threads=True)
    close = df["Close"]
    out = []
    for sym in close.columns:
        s = close[sym].dropna()
        if len(s) == 0:
            continue
        last = float(s.iloc[-1]); prior = float(s.iloc[-2]) if len(s) > 1 else None
        out.append({"symbol": sym, "price": round(last, 4), "date": s.index[-1].strftime("%Y-%m-%d"),
                    "change_pct": round((last / prior - 1) * 100, 2) if prior else None})
    return out


def movers(kind):
    def run():
        res = yf.screen(kind, count=100)
        keep = ["symbol", "shortName", "regularMarketPrice", "regularMarketChangePercent",
                "regularMarketVolume", "averageDailyVolume3Month", "marketCap", "fiftyTwoWeekChangePercent"]
        return [{k: q.get(k) for k in keep} for q in res.get("quotes", [])]
    return run


def daily():
    df = yf.download(TICKERS, period="400d", interval="1d", auto_adjust=True, progress=False, threads=True)
    return long_rows(df, "%Y-%m-%d")


def sector_board():
    df = yf.download(list(SECTORS) + ["SPY"], period="400d", interval="1d", auto_adjust=True, progress=False)["Close"]
    spy = df["SPY"].dropna()
    out = []
    for sym, name in SECTORS.items():
        s = df[sym].dropna()
        row = {"symbol": sym, "sector": name, "price": round(float(s.iloc[-1]), 2)}
        for label, n in (("1d", 1), ("1w", 5), ("1m", 21), ("3m", 63), ("1y", 252)):
            if len(s) > n:
                r = s.iloc[-1] / s.iloc[-1 - n] - 1
                b = spy.iloc[-1] / spy.iloc[-1 - n] - 1
                row[label] = round(r * 100, 2)
                row[label + "_vs_spy"] = round((r - b) * 100, 2)
        out.append(row)
    return sorted(out, key=lambda r: -(r.get("1m_vs_spy") or -999))


def treasury():
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO,DGS2,DGS5,DGS10,DGS30"
    df = pd.read_csv(url).tail(260)
    df = df.rename(columns={df.columns[0]: "date"})
    return json.loads(df.to_json(orient="records"))


# fast layer: every run
save("quotes", quotes)
save("intraday_5m", intraday)
save("gainers", movers("day_gainers"))
save("losers", movers("day_losers"))
save("most_active", movers("most_actives"))

# slow layer: at most once an hour, or if a file is missing
last = status.get("slow_last")
due = (not last or NOW - datetime.datetime.fromisoformat(last.rstrip("Z")) >= datetime.timedelta(minutes=55)
       or not (OUT / "prices_daily.json").exists() or not (OUT / "sector_board.json").exists())
if due:
    save("prices_daily", daily)
    save("sector_board", sector_board)
    save("treasury_rates", treasury)
    status["slow_last"] = now

status_path.write_text(json.dumps(status, indent=2))
print(json.dumps(status, indent=2))
