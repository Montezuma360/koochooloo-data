"""Backfill and reconcile SEC Form 4 insider trades from EDGAR's daily form index.

The live feed (refresh_sec.py) reads EDGAR's current-filings list. This script reads the official daily index for
past business days, finds every Form 4 not yet processed, fetches it (one request each, 8 a second) and writes the
open-market buys and sells to data/insider_backfill.json, which refresh_sec.py merges on its next run.

  python backfill_insiders.py 45   # first fill: 45 calendar days, so the 30-day cluster signal works from day one
  python backfill_insiders.py 5    # nightly reconcile: catches anything the live feed missed

The daily index for a day is published after EDGAR closes (10 pm New York), so the latest day appears the next night."""
import datetime, json, re, sys, time
import refresh_sec as S


def index_entries(day):
    q = (day.month - 1) // 3 + 1
    url = "https://www.sec.gov/Archives/edgar/daily-index/%d/QTR%d/form.%s.idx" % (day.year, q, day.strftime("%Y%m%d"))
    try:
        txt = S.get(url, tries=2)
    except Exception:
        return {}  # weekend, holiday, or not yet published
    out = {}
    for line in txt.splitlines():
        m = re.match(r"^(4|4/A)\s{2,}.*?\s(\d{1,10})\s+(\d{8}|\d{4}-\d{2}-\d{2})\s+(edgar/data/\S+?/?([\d-]{20})\.txt)\s*$", line)
        if m:
            d = m.group(3).replace("-", "")
            out.setdefault(m.group(5), (m.group(2), "%s-%s-%s" % (d[:4], d[4:6], d[6:])))
    return out


def main(days):
    t0 = time.time()
    seen = set(S.load("sec_seen.json", {}).get("seen", {}))
    stored = {r["id"].rsplit("-", 1)[0] for r in S.load("insider_trades.json", {}).get("rows", [])}
    prev = S.load("insider_backfill.json", {}).get("rows", [])
    jobs, per_day = {}, {}
    for i in range(1, days + 1):
        day = (S.NOW - datetime.timedelta(days=i)).date()
        if day.weekday() >= 5:
            continue
        ents = index_entries(day)
        per_day[str(day)] = len(ents)
        for acc, v in ents.items():
            if acc not in seen and acc not in stored:
                jobs[acc] = v
    errors = []
    rows = S.fetch_many(jobs, errors)
    cut = (S.NOW - datetime.timedelta(days=S.KEEP_DAYS)).strftime("%Y-%m-%d")
    allrows = S.unique([r for r in prev + rows if r.get("date") and r["date"] >= cut])
    (S.OUT / "insider_backfill.json").write_text(json.dumps({"updated": S.STAMP, "days": days, "rows": allrows}))
    status = {"updated": S.STAMP, "days": days, "filings_in_index": per_day, "fetched": len(jobs), "new_rows": len(rows),
              "rows_in_file": len(allrows), "errors": len(errors), "error_sample": errors[:10], "seconds": round(time.time() - t0)}
    (S.OUT / "sec_backfill_status.json").write_text(json.dumps(status, indent=1))
    print(json.dumps(status, indent=1))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
