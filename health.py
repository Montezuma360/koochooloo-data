"""Koochooloo data health check: how old is every feed, did its last run fail, and is anything stale?

Writes data/health.json (read by the terminal and Setareh) and appends one line per run to
data/history/health/YYYY-MM.csv so uptime can be measured over the long run."""
import datetime as dt, json, os, pathlib, urllib.request

D = pathlib.Path("data")
NOW = dt.datetime.now(dt.timezone.utc)

# feed: (file, key holding the timestamp, hours before it counts as stale, weekdays-only feed?)
FEEDS = {
    "prices": ("status.json", "updated", 2, False),            # 5-min in US hours, hourly otherwise
    "sec_insiders": ("sec_status.json", "updated", 1, True),  # every 5 minutes on weekdays
    "signals": ("signals_status.json", "updated", 3, False),  # every 5 minutes (GitHub skips many)
    "treasury_rates": ("status.json", "slow_last", 26, False),
    "alt_data": ("alt/status.json", "updated", 30, True),     # after each US close
    "paper_trading": ("paper/summary.json", "updated", 30, True),
    "intelligence": ("intel/status.json", "updated", 2, False),  # crypto, news, SEC filings, options, macro, sentiment
}
# While the US market is open these feeds should be no more than this many minutes old (a 5-minute refresh plus run time).
# Missing the target is a warning, not "stale": stale data stops the paper engine trading, a slow refresh should not.
TARGET_MIN = {"prices": 10, "sec_insiders": 10, "signals": 10, "intelligence": 10}


def market_open():
    from zoneinfo import ZoneInfo
    et = NOW.astimezone(ZoneInfo("America/New_York"))
    return et.weekday() < 5 and dt.time(9, 40) <= et.time() <= dt.time(16, 0)


def ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def weekend_gap():
    """Extra hours a weekdays-only feed may be quiet: from Friday's last run to Monday."""
    wd = NOW.weekday()
    if wd >= 5:
        return 24 * (wd - 4) + 6
    if wd == 0 and NOW.hour < 23:
        return 72
    return 0


def runs():
    """Latest conclusion of each workflow, from the GitHub API (needs actions: read)."""
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (tok and repo):
        return {}
    try:
        req = urllib.request.Request("https://api.github.com/repos/%s/actions/runs?per_page=100" % repo,
                                     headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"})
        R = json.loads(urllib.request.urlopen(req, timeout=30).read())["workflow_runs"]
    except Exception as e:
        return {"error": repr(e)[:120]}
    out = {}
    for r in R:  # newest first
        n = r["name"]
        if r["status"] != "completed":
            continue
        o = out.setdefault(n, {"last": r["conclusion"], "at": r["updated_at"], "failures_in_last_100": 0, "runs_in_last_100": 0})
        o["runs_in_last_100"] += 1
        o["failures_in_last_100"] += r["conclusion"] == "failure"
    return out


def repo_size_mb():
    """Size of the data store on GitHub, so its growth can be watched (GitHub suggests staying under about 5 GB)."""
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (tok and repo):
        return None
    try:
        req = urllib.request.Request("https://api.github.com/repos/" + repo, headers={"Authorization": "Bearer " + tok})
        return round(json.loads(urllib.request.urlopen(req, timeout=30).read())["size"] / 1024, 1)
    except Exception:
        return None


def main():
    feeds, problems, warnings = {}, [], []
    is_open = market_open()
    for name, (f, key, limit, weekdays) in FEEDS.items():
        try:
            j = json.loads((D / f).read_text())
        except Exception:
            feeds[name] = {"ok": False, "why": "file missing or unreadable"}
            problems.append(name + ": missing")
            continue
        t = ts(j.get(key))
        age = round((NOW - t).total_seconds() / 3600, 2) if t else None
        allowed = limit + (weekend_gap() if weekdays else 0)
        failed = j.get("failed") or ({"errors": j["errors"]} if j.get("errors") else {})
        stale = age is None or age > allowed
        feeds[name] = {"updated": j.get(key), "age_hours": age, "stale_after_hours": allowed, "stale": stale,
                       "failed_parts": failed, "ok": not stale and not failed}
        if name in TARGET_MIN:
            on_target = None if not is_open else (age is not None and age * 60 <= TARGET_MIN[name])
            feeds[name].update({"target_minutes_when_market_open": TARGET_MIN[name], "on_target": on_target})
            if on_target is False:
                warnings.append("%s: %s min since last update, target %d (refresh running slow)" % (
                    name, round(age * 60) if age is not None else "?", TARGET_MIN[name]))
        if stale:
            problems.append("%s: %s hours old" % (name, age))
        if failed:
            problems.append("%s: %d part(s) failed" % (name, len(failed)))
    wf = runs()
    for n, r in wf.items():
        if isinstance(r, dict) and r.get("last") == "failure":
            problems.append("workflow '%s' failed last run" % n)
    out = {"updated": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "healthy": not problems, "problems": problems,
           "market_open": is_open, "warnings": warnings, "repo_size_mb": repo_size_mb(), "feeds": feeds, "workflows": wf}
    (D / "health.json").write_text(json.dumps(out, indent=1))
    h = D / "history" / "health"
    h.mkdir(parents=True, exist_ok=True)
    f = h / (NOW.strftime("%Y-%m") + ".csv")
    new = not f.exists()
    with f.open("a") as fh:
        if new:
            fh.write("time,healthy,stale_feeds,problems\n")
        fh.write('%s,%d,%d,"%s"\n' % (out["updated"], out["healthy"], sum(v.get("stale", True) for v in feeds.values()),
                                       "; ".join(problems).replace('"', "'")))
    print(json.dumps({"healthy": out["healthy"], "problems": problems, "warnings": warnings}, indent=1))


if __name__ == "__main__":
    main()
