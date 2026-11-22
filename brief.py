"""Koochooloo Morning Brief: a short, general research email built from data/brain.json.

Writes data/brief/latest.md, data/brief/latest.html and data/brief/archive/YYYY-MM-DD.md.
If the BUTTONDOWN_KEY secret is set, it also saves the brief as a DRAFT in Buttondown, so the owner reads it before it
goes to anyone. General research for a broad audience, not personal advice: no "buy this", only what the data shows."""
import datetime as dt
import html
import json
import os
import pathlib
import urllib.request

D = pathlib.Path(__file__).resolve().parent / "data"
OUT = D / "brief"
NOW = dt.datetime.now(dt.timezone.utc)
DISCLAIMER = ("This is general research produced automatically from public data (SEC and Congress filings, market prices). "
              "It is not personal advice or a recommendation to buy or sell anything. Prices can fall as well as rise; "
              "many trades that match these patterns lose money. Do your own research and, if needed, take regulated advice.")
NAMES = {"insider_cluster3": "Insiders buying together", "insider_big_250k": "Big insider buys", "congress_cluster3": "Congress buying together",
         "congress_big_100k": "Big Congress buys", "terminal_top_signals": "Terminal top signals", "insider_cluster2_smallcap": "Small-company insider buying",
         "research_setup": "Research setup", "research_setup_trend": "Research setup + trend brake"}


def load(p, default=None):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return default


def pct(x, nd=1):
    return "n/a" if x is None else ("%+.*f%%" % (nd, x))


def build(b):
    r = b.get("regime") or {}
    day = NOW.strftime("%a %-d %b %Y")
    lines = ["# Koochooloo Morning Brief · %s" % day, ""]
    lines += ["## The market in one line",
              "%s. S&P 500 %s against its 200-day average; VIX %s; Fear & Greed %s (%s)." % (
                  str(r.get("level", "unknown")).capitalize(), pct(r.get("spy_vs_200d_pct")), r.get("vix", "n/a"),
                  r.get("fear_greed", "n/a"), r.get("fear_greed_label", "")), ""]
    setups = [s for s in b.get("stocks", []) if s.get("research_setup") and not s.get("warnings")][:6]
    lines += ["## Insider-buying setups the data flags today",
              "Smaller companies where two or more officers or directors each bought $25,000+ of their own stock within 30 days, "
              "and the business makes more cash than its reported profit. In our 2014–2026 tests this pattern beat the S&P 500 by "
              "about 2.5% per trade on average over 60 days. That is an average, not a promise: many such trades lost.", ""]
    if setups:
        lines += ["| Stock | Price | 1 month | RSI | Who bought |", "|---|---|---|---|---|"]
        for s in setups:
            p = s.get("price") or {}
            who = next((e.get("detail", "") for e in s.get("evidence", []) if e.get("type") == "insider_cluster"), "")
            who = who.split(";")[0].split(" (")[0]
            rsi = p.get("rsi14")
            rs = "n/a" if rsi is None else ("%s%s" % (rsi, " (overbought)" if rsi > 70 else " (oversold)" if rsi < 30 else ""))
            lines.append("| %s | $%s | %s | %s | %s |" % (s["symbol"], p.get("price", "n/a"), pct(p.get("change_1m_pct")), rs, who))
    else:
        lines.append("None today.")
    lines += ["", "RSI above 70 or below 30 means the price has moved fast; our own paper rules skip those until it settles.", ""]
    flags = [f for f in ((b.get("sec") or {}).get("red_flags") or []) if f.get("ticker")][:4]
    if flags:
        lines += ["## Warning signs filed with the SEC"]
        for f in flags:
            lines.append("- **%s** (%s): %s" % (f["ticker"], f["company"], "; ".join(i.get("meaning", "") for i in f.get("items", []) if i.get("item") in f.get("red_flag", []))))
        lines.append("")
    P = b.get("paper") or {}
    board = (P.get("goal") or {}).get("leaderboard") or []
    if board:
        lines += ["## Our public paper-trading scorecard",
                  "Virtual money, real rules: every trade is compared with the S&P 500 over the same days, so sitting in cash earns no credit.", "",
                  "| Strategy | Average per trade vs S&P | Trades beating S&P | Trades measured |", "|---|---|---|---|"]
        for x in board:
            lines.append("| %s | %s | %s | %s |" % (NAMES.get(x["strategy"], x["strategy"]), pct(x.get("skill_vs_spy_pct"), 2),
                                                    ("%s%%" % x["trades_beating_spy_pct"]) if x.get("trades_beating_spy_pct") is not None else "n/a",
                                                    x.get("trades_measured", 0)))
        lines += ["", "Started 1 November 2026. Rankings mean little before 90 days.", ""]
    news = ((b.get("news") or {}).get("latest") or [])[:3]
    if news:
        lines += ["## Headlines"] + ["- [%s](%s) · %s" % (n["title"], n["link"], n.get("source", "")) for n in news] + [""]
    lines += ["---", "*%s*" % DISCLAIMER]
    return "\n".join(lines)


def to_html(md):
    out, table = [], False
    for ln in md.split("\n"):
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if not table:
                out.append('<table style="border-collapse:collapse;font-size:14px">'); table = True
                out.append("<tr>" + "".join('<th style="text-align:left;border-bottom:1px solid #ccc;padding:4px 8px">%s</th>' % html.escape(c) for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join('<td style="padding:4px 8px;border-bottom:1px solid #eee">%s</td>' % html.escape(c) for c in cells) + "</tr>")
            continue
        if table:
            out.append("</table>"); table = False
        e = html.escape(ln)
        if ln.startswith("# "):
            out.append("<h1>%s</h1>" % e[2:])
        elif ln.startswith("## "):
            out.append("<h2>%s</h2>" % e[3:])
        elif ln.startswith("- "):
            out.append("<p>• %s</p>" % e[2:])
        elif ln == "---":
            out.append("<hr>")
        elif ln:
            out.append("<p>%s</p>" % e)
    if table:
        out.append("</table>")
    return '<div style="font-family:Georgia,serif;max-width:640px">' + "\n".join(out) + "</div>"


def main():
    b = load(D / "brain.json")
    if not b:
        print("no brain.json"); return
    md = build(b)
    (OUT / "archive").mkdir(parents=True, exist_ok=True)
    (OUT / "latest.md").write_text(md)
    (OUT / "latest.html").write_text(to_html(md))
    (OUT / "archive" / (NOW.strftime("%Y-%m-%d") + ".md")).write_text(md)
    key = os.environ.get("BUTTONDOWN_KEY")
    if key:
        body = json.dumps({"subject": md.split("\n", 1)[0].lstrip("# "), "body": md, "status": "draft"}).encode()
        req = urllib.request.Request("https://api.buttondown.com/v1/emails", data=body, method="POST",
                                     headers={"Authorization": "Token " + key, "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30)
            print("saved as a Buttondown draft")
        except Exception as e:
            print("Buttondown draft failed:", repr(e)[:200])
    print("brief written:", len(md), "characters")


if __name__ == "__main__":
    main()
