"""Koochooloo SEC insider layer. Reads the newest Form 4 filings from SEC EDGAR (free, no key),
keeps open-market insider buys and sells for 90 days, and flags clusters of insider buying.
Writes data/insider_trades.json, data/insider_clusters.json and data/sec_status.json."""
import json, os, re, time, pathlib, datetime, urllib.request, xml.etree.ElementTree as ET

UA = os.environ.get("SEC_CONTACT") or "KoochoolooResearch github.com/Montezuma360/koochooloo-data"
OUT = pathlib.Path("data")
OUT.mkdir(exist_ok=True)
now = datetime.datetime.utcnow()
status = {"updated": now.replace(microsecond=0).isoformat() + "Z", "filings_seen": 0, "new_rows": 0, "errors": []}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        time.sleep(0.15)  # stay well under the SEC's 10 requests per second limit
        return r.read().decode("utf-8", "replace")


def txt(node, path):
    el = node.find(path)
    return el.text.strip() if el is not None and el.text else None


def parse_form4(xml, accession):
    root = ET.fromstring(xml)
    issuer = txt(root, "issuer/issuerName")
    sym = txt(root, "issuer/issuerTradingSymbol")
    rows = []
    owners = root.findall("reportingOwner")
    owner = txt(owners[0], "reportingOwnerId/rptOwnerName") if owners else None
    rel = owners[0].find("reportingOwnerRelationship") if owners else None
    role = None
    if rel is not None:
        role = txt(rel, "officerTitle") or ("Director" if txt(rel, "isDirector") in ("1", "true") else
               "10% owner" if txt(rel, "isTenPercentOwner") in ("1", "true") else None)
    for i, t in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction")):
        code = txt(t, "transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        try:
            shares = float(txt(t, "transactionAmounts/transactionShares/value") or 0)
            price = float(txt(t, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        rows.append({"id": f"{accession}-{i}", "symbol": sym, "issuer": issuer, "insider": owner, "role": role,
                     "side": "buy" if code == "P" else "sell", "date": txt(t, "transactionDate/value"),
                     "shares": round(shares), "price": round(price, 4), "value": round(shares * price)})
    return rows


def latest_filings():
    atom = get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb=&owner=include&start=0&count=100&output=atom")
    return re.findall(r'href="(https://www\.sec\.gov/Archives/edgar/data/[^"]+-index\.htm)"', atom)


path = OUT / "insider_trades.json"
old = json.loads(path.read_text())["rows"] if path.exists() else []
seen = {r["id"] for r in old}
new = []
try:
    links = list(dict.fromkeys(latest_filings()))
    status["filings_seen"] = len(links)
    for link in links:
        try:
            acc = link.rsplit("/", 1)[1].replace("-index.htm", "")
            if any(s.startswith(acc) for s in seen):
                continue
            page = get(link)
            m = re.search(r'href="(/Archives/edgar/data/[^"]+\.xml)"', page)
            if not m:
                continue
            xml_url = re.sub(r"/xsl[^/]*/", "/", "https://www.sec.gov" + m.group(1))
            new += parse_form4(get(xml_url), acc)
        except Exception as e:
            status["errors"].append(f"{link[-40:]}: {type(e).__name__}")
except Exception as e:
    status["errors"].append(f"feed: {type(e).__name__}: {str(e)[:150]}")

cut = (now - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
rows = [r for r in old + new if r.get("date") and r["date"] >= cut]
status["new_rows"] = len(new)
status["total_rows"] = len(rows)
path.write_text(json.dumps({"updated": status["updated"], "rows": rows}))

# clusters: 2+ different insiders buying the same stock in the last 30 days
c30 = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
agg = {}
for r in rows:
    if r["side"] == "buy" and r["date"] >= c30 and r["symbol"]:
        a = agg.setdefault(r["symbol"], {"symbol": r["symbol"], "issuer": r["issuer"], "insiders": set(), "value": 0, "last": ""})
        a["insiders"].add(r["insider"]); a["value"] += r["value"]; a["last"] = max(a["last"], r["date"])
clusters = sorted(({**a, "insiders": sorted(i for i in a["insiders"] if i), "n": len(a["insiders"])} for a in agg.values() if len(a["insiders"]) >= 2),
                  key=lambda a: (-a["n"], -a["value"]))
(OUT / "insider_clusters.json").write_text(json.dumps({"updated": status["updated"], "rows": clusters}))
status["clusters"] = len(clusters)
(OUT / "sec_status.json").write_text(json.dumps(status, indent=2))
print(json.dumps(status, indent=2))
