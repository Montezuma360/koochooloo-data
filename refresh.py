"""Koochooloo brain refresh. Pulls market data with OpenBB and writes JSON files into data/.
Every pull is wrapped so one failure never stops the rest. status.json says what worked."""
import json, datetime, pathlib, traceback

from openbb import obb

OUT = pathlib.Path("data")
OUT.mkdir(exist_ok=True)
now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
status = {"updated": now, "ok": {}, "failed": {}}


def rows(result):
    df = result.to_df().reset_index()
    return json.loads(df.to_json(orient="records", date_format="iso"))


def save(name, fn):
    try:
        data = fn()
        (OUT / f"{name}.json").write_text(json.dumps({"updated": now, "rows": data}))
        status["ok"][name] = len(data)
    except Exception as e:
        status["failed"][name] = f"{type(e).__name__}: {str(e)[:200]}"
        traceback.print_exc()


INDEX = ["SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "BTC-USD", "ETH-USD", "^VIX"]
SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC"]
start = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()

save("prices_daily", lambda: rows(obb.equity.price.historical(INDEX + SECTORS, start_date=start, provider="yfinance")))
save("gainers", lambda: rows(obb.equity.discovery.gainers(provider="yfinance")))
save("losers", lambda: rows(obb.equity.discovery.losers(provider="yfinance")))
save("most_active", lambda: rows(obb.equity.discovery.active(provider="yfinance")))
save("treasury_rates", lambda: rows(obb.fixedincome.government.treasury_rates(provider="federal_reserve")))

(OUT / "status.json").write_text(json.dumps(status, indent=2))
print(json.dumps(status, indent=2))
