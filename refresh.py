"""Koochooloo brain refresh v2. Writes JSON files into data/. Every pull is wrapped so one
failure never stops the rest. data/status.json says what worked and what did not."""
import json, datetime, pathlib, subprocess, sys, traceback

try:
    import yfinance as yf
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "yfinance"])
    import yfinance as yf
import pandas as pd

OUT = pathlib.Path("data")
OUT.mkdir(exist_ok=True)
now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
status = {"updated": now, "ok": {}, "failed": {}, "openbb": None}


def save(name, fn):
    try:
        data = fn()
        (OUT / f"{name}.json").write_text(json.dumps({"updated": now, "rows": data}))
        status["ok"][name] = len(data)
    except Exception as e:
        status["failed"][name] = f"{type(e).__name__}: {str(e)[:200]}"
        traceback.print_exc()


TICKERS = ["SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "BTC-USD", "ETH-USD", "^VIX",
           "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC"]


def prices():
    df = yf.download(TICKERS, period="400d", interval="1d", auto_adjust=True, progress=False)
    out = []
    close, vol = df["Close"], df["Volume"]
    for sym in close.columns:
        for ts, c in close[sym].dropna().items():
            v = vol[sym].get(ts)
            out.append({"symbol": sym, "date": ts.strftime("%Y-%m-%d"), "close": round(float(c), 4),
                        "volume": None if pd.isna(v) else int(v)})
    return out


def movers(kind):
    def run():
        res = yf.screen(kind, count=50)
        keep = ["symbol", "shortName", "regularMarketPrice", "regularMarketChangePercent",
                "regularMarketVolume", "marketCap", "fiftyTwoWeekChangePercent"]
        return [{k: q.get(k) for k in keep} for q in res.get("quotes", [])]
    return run


def treasury():
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO,DGS2,DGS5,DGS10,DGS30"
    df = pd.read_csv(url).tail(260)
    df = df.rename(columns={df.columns[0]: "date"})
    return json.loads(df.to_json(orient="records"))


save("prices_daily", prices)
save("gainers", movers("day_gainers"))
save("losers", movers("day_losers"))
save("most_active", movers("most_actives"))
save("treasury_rates", treasury)

# OpenBB probe: records whether OpenBB is installed and what it offers, so we can wire it in next.
try:
    import openbb
    from openbb import obb
    status["openbb"] = {"version": getattr(openbb, "__version__", "unknown"),
                        "routes": sorted(a for a in dir(obb) if not a.startswith("_"))[:40]}
except Exception as e:
    status["openbb"] = f"not available: {type(e).__name__}"

(OUT / "status.json").write_text(json.dumps(status, indent=2))
print(json.dumps(status, indent=2))
