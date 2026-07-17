#!/usr/bin/env python3

"""
Server-side fetcher — runs in GitHub Actions (or locally).
Pulls Elexon (GB), EIA (US), ENTSO-E (DK/DE/NL zone context), converts physical
generation to Ørsted share via consolidation %, and writes docs/data/production.json
which the static GitHub Pages site reads. Keys come from environment variables
(GitHub Secrets): EIA_KEY, ENTSOE_TOKEN.

    python3 fetch_data.py                       # full history to today
    python3 fetch_data.py --start 2024-01-01
    python3 fetch_data.py --demo                # no network; synthesise from reported

Stdlib only.
"""
import json, os, sys, argparse, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from datetime import date, timedelta, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
XW = json.load(open(os.path.join(HERE, "crosswalk.json")))
OUT = os.path.join(HERE, "docs", "data", "production.json")
QUARTERS = XW["quarters"]
EIA_KEY = os.environ.get("EIA_KEY", "")
ENTSOE_TOKEN = os.environ.get("ENTSOE_TOKEN", "")
DEMO = False
UA = {"User-Agent": "orsted-production-site/1.0", "Accept": "application/json"}

def qtag(y, m): return "Q%d %d" % ((m - 1)//3 + 1, y)
def monthkey(y, m): return "%04d-%02d" % (y, m)
def months(s, e):
    y, m = int(s[:4]), int(s[5:7]); Y, M = int(e[:4]), int(e[5:7]); out = []
    while (y, m) <= (Y, M):
        out.append((y, m)); m += 1
        if m > 12: y += 1; m = 1
    return out
def get(url, timeout=180):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r: return r.read()

# ----------------------------------------------------------------- Elexon
ELEXON = "https://data.elexon.co.uk/bmrs/api/v1/datasets/B1610/stream"
BMU2ASSET = {}
for a in XW["assets"]:
    for b in a.get("bmus") or []:
        BMU2ASSET[b.upper()] = a["name"]; BMU2ASSET[b.upper().replace("T_", "")] = a["name"]
BMU_FIELDS = ["bMUnitID", "marketGenerationBMUId", "nGCBMUnitID", "bmUnit"]

def pull_elexon(start, end, phys, warn):
    if not BMU2ASSET:
        warn.append("Elexon: no BMUs mapped"); return
    allb = sorted({b for a in XW["assets"] for b in (a.get("bmus") or [])})
    for (y, m) in months(start, end):
        frm = "%s-01T00:00Z" % monthkey(y, m)
        to = ("%04d-01-01T00:00Z" % (y+1)) if m == 12 else ("%04d-%02d-01T00:00Z" % (y, m+1))
        q = "from=%s&to=%s&" % (urllib.parse.quote(frm), urllib.parse.quote(to)) + "&".join("bmUnit=%s" % urllib.parse.quote(b) for b in allb)
        try:
            raw = json.loads(get(ELEXON + "?" + q))
            rows = raw if isinstance(raw, list) else raw.get("data", [])
        except Exception as ex:
            warn.append("Elexon %s: %s" % (monthkey(y, m), ex)); continue
        field = next((f for f in BMU_FIELDS if rows and f in rows[0]), None)
        mk = monthkey(y, m)
        for x in rows:
            idv = str(x.get(field) if field else (x.get("bMUnitID") or x.get("bmUnit") or "")).upper()
            a = BMU2ASSET.get(idv) or BMU2ASSET.get(idv.replace("T_", ""))
            if not a: continue
            val = float(x.get("quantity", 0) or 0)          # metered MWh per SP
            phys.setdefault(a, {}); phys[a][mk] = phys[a].get(mk, 0.0) + val/1000.0

# ----------------------------------------------------------------- EIA
EIA = "https://api.eia.gov/v2/electricity/facility-fuel/data/"
def pull_eia(start, end, phys, warn):
    codes = {str(a["eia_plant"]): a["name"] for a in XW["assets"] if a.get("eia_plant")}
    if not codes: warn.append("EIA: no plant codes"); return
    if not EIA_KEY: warn.append("EIA: no EIA_KEY secret"); return
    q = [("api_key", EIA_KEY), ("frequency", "monthly"), ("data[0]", "generation"),
         ("start", start[:7]), ("end", end[:7]), ("length", "5000"),
         ("sort[0][column]", "period"), ("sort[0][direction]", "asc")]
    for i, c in enumerate(codes): q.append(("facets[plantCode][%d]" % i, c))
    off = 0
    while True:
        try:
            raw = json.loads(get(EIA + "?" + urllib.parse.urlencode(q + [("offset", str(off))])))
            rows = raw.get("response", {}).get("data", [])
        except Exception as ex:
            warn.append("EIA: %s" % ex); break
        if not rows: break
        for x in rows:
            a = codes.get(str(x.get("plantCode"))); 
            if not a: continue
            mk = str(x.get("period"))[:7]
            phys.setdefault(a, {}); phys[a][mk] = phys[a].get(mk, 0.0) + (float(x.get("generation", 0) or 0))/1000.0
        if len(rows) < 5000: break
        off += 5000

# ----------------------------------------------------------------- ENTSO-E (per-unit, A73)
#  documentType A73 = "Actual Generation Output per Generation Unit" (16.1.A).
#  Query per zone returns every unit with its EIC + name + generation; we match the
#  unit name to Ørsted farms via crosswalk psr_match tokens and sum to energy (MWh).
ENTSOE = "https://web-api.tp.entsoe.eu/api"
RES_HOURS = {"PT15M": 0.25, "PT30M": 0.5, "PT60M": 1.0, "P1D": 24.0}

def parse_a73(xml_bytes):
    """-> list of dicts: {name, eic, mwh} per generation unit TimeSeries."""
    root = ET.fromstring(xml_bytes)
    ns = root.tag.split("}")[0].strip("{")
    def t(el, path): 
        x = el.find(path.replace("ns", ns) if "ns" in path else path); return x.text if x is not None else None
    out = []
    for ts in root.iter("{%s}TimeSeries" % ns):
        name = eic = None
        psr = ts.find(".//{%s}MktPSRType/{%s}PowerSystemResources" % (ns, ns))
        if psr is not None:
            eic = psr.findtext("{%s}mRID" % ns); name = psr.findtext("{%s}name" % ns)
        mwh = 0.0
        for period in ts.iter("{%s}Period" % ns):
            res = period.findtext("{%s}resolution" % ns) or "PT60M"
            h = RES_HOURS.get(res, 1.0)
            for pt in period.iter("{%s}Point" % ns):
                try: mwh += float(pt.findtext("{%s}quantity" % ns)) * h
                except (TypeError, ValueError): pass
        out.append({"name": name, "eic": eic, "mwh": mwh})
    return out

def match_farm(name):
    n = (name or "").lower()
    for a in XW["assets"]:
        for tok in a.get("psr_match", []):
            if tok in n: return a["name"]
    return None

def pull_entsoe(start, end, phys, warn, seen_units):
    if not ENTSOE_TOKEN: warn.append("ENTSO-E: no ENTSOE_TOKEN secret"); return
    zones = XW.get("entsoe_zones", {})
    for (y, m) in months(start, end):
        mk = monthkey(y, m)
        ps = "%04d%02d010000" % (y, m)
        pe = ("%04d01010000" % (y+1)) if m == 12 else ("%04d%02d010000" % (y, m+1))
        for zname, eic in zones.items():
            q = {"securityToken": ENTSOE_TOKEN, "documentType": "A73", "processType": "A16",
                 "in_Domain": eic, "periodStart": ps, "periodEnd": pe}
            try:
                units = parse_a73(get(ENTSOE + "?" + urllib.parse.urlencode(q)))
            except urllib.error.HTTPError as ex:
                if ex.code not in (400, 404):
                    warn.append("ENTSO-E %s %s: HTTP %s" % (zname, mk, ex.code))
                continue
            except Exception as ex:
                warn.append("ENTSO-E %s %s: %s" % (zname, mk, ex)); continue
            for u in units:
                if u["name"]: seen_units.add(u["name"])
                farm = match_farm(u["name"])
                if farm and u["mwh"]:
                    phys.setdefault(farm, {}); phys[farm][mk] = phys[farm].get(mk, 0.0) + u["mwh"]/1000.0

# ----------------------------------------------------------------- demo
def demo(start, end, phys):
    seed = [7]
    def rng():
        seed[0] = (seed[0]*1103515245 + 12345) & 0x7fffffff; return seed[0]/0x7fffffff
    keep = {qtag(y, m) for (y, m) in months(start, end)}
    for a in XW["assets"]:
        if a["pullsrc"] not in ("elexon","eia","entsoe") or not a["cons"] or not (a.get("bmus") or a.get("eia_plant") or a.get("psr_match")): continue
        for i, q in enumerate(QUARTERS):
            if q not in keep: continue
            rep = a["reported"][i]
            if rep is None or rep <= 0: continue
            # spread across the 3 months of the quarter
            yy = int(q.split()[1]); qq = int(q[1]); 
            for mm in range(3*(qq-1)+1, 3*(qq-1)+4):
                phys.setdefault(a["name"], {})[monthkey(yy, mm)] = rep/a["cons"]/3*(1+(rng()-0.5)*0.1)

# ----------------------------------------------------------------- assemble
def build(start, end):
    phys = {}; warn = []; seen_units = set()
    if DEMO:
        demo(start, end, phys); warn.append("DEMO: physical synthesised from reported. Not real grid data.")
    else:
        pull_elexon(start, end, phys, warn)
        pull_eia(start, end, phys, warn)
        pull_entsoe(start, end, phys, warn, seen_units)
        unmatched = [a["name"] for a in XW["assets"] if a.get("psr_match") and not phys.get(a["name"])]
        if unmatched:
            warn.append("ENTSO-E: no unit matched for " + ", ".join(unmatched)
                        + ". Seen unit names: " + "; ".join(sorted(seen_units)[:40]))
    idx = {a["name"]: a for a in XW["assets"]}
    assets = []
    for a in XW["assets"]:
        pm = phys.get(a["name"], {})
        physQ, shareQ = {}, {}
        for mk, gwh in pm.items():
            y, m = int(mk[:4]), int(mk[5:7]); qk = qtag(y, m)
            physQ[qk] = physQ.get(qk, 0.0) + gwh
            if a["cons"] is not None: shareQ[qk] = shareQ.get(qk, 0.0) + gwh*a["cons"]
        assets.append({"name": a["name"], "geo": a["geo"], "seg": a["seg"], "cons": a["cons"],
                       "src": a["pullsrc"], "reported": a["reported"],
                       "phys": {k: round(v, 2) for k, v in physQ.items()},
                       "share": {k: round(v, 2) for k, v in shareQ.items()},
                       "phys_m": {k: round(v, 2) for k, v in pm.items()}})
    doc = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "window": {"start": start, "end": end}, "quarters": QUARTERS,
           "assets": assets, "zones": {}, "warnings": warn, "demo": DEMO}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(doc, open(OUT, "w"), ensure_ascii=False, separators=(",", ":"))
    pulled = sum(1 for a in assets if a["phys"])
    print("wrote %s | %d/%d assets with data | warnings %d"
          % (OUT, pulled, len(assets), len(warn)))
    for w in warn: print("  -", w)

def main():
    global DEMO
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01"); ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args(); DEMO = args.demo
    build(args.start, args.end)

if __name__ == "__main__":
    main()
