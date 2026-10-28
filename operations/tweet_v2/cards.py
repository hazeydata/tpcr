#!/usr/bin/env python
"""TPCR daily cards: forecast card (weather5 style) + scorecard reply (design C).
usage: cards.py DATE OUTDIR --history WTI_PARQUET (--predicted JSON [--observed JSON] | --csv PATH) [--forecast-only]
Live inputs (what operations/scripts/wti_*_tweet.py pass): the Step 14 content JSONs predicted_DATE.json / observed_DATE.json
and the pipeline's wti.parquet (source=="historical" rows give each park's normal box).
Timing rule (Fred, Oct 26 2026): the forecast for DATE posts at 4 PM ET on DATE-1, so the forecast card never says "today" (kicker is the date only).
Writes OUTDIR/F-DATE.html, F-DATE.alt.txt and (if observed exists) R-DATE.html, R-DATE.alt.txt.
REAL numbers only: any missing forecast (or, for the reply, observed) value is a hard error. Never estimates."""
import csv, json, sys, os, argparse, datetime as dt
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import headline_copy as HC  # all fallback headline copy (closed-row R2)

P = ["MK", "EP", "HS", "AK"]
N = {"MK": "Magic Kingdom", "EP": "EPCOT", "HS": "Hollywood Studios", "AK": "Animal Kingdom"}
ALT_MAX = 1000    # X media alt-text limit
CSS = """<style>*{margin:0;box-sizing:border-box}body{width:1600px;height:900px;background:#0F1B2D;color:#E8EDF4;font-family:'Space Grotesk';padding:60px 96px;position:relative}
.m{font-family:'Geist Mono'}.k{font-size:30px;color:#9FB0C6}.h{font-size:54px;font-weight:700;margin:4px 0 26px;white-space:nowrap}
.brand{position:absolute;top:60px;right:96px;font-size:28px;color:#9FB0C6}"""
FCSS = CSS + """
.r{display:grid;grid-template-columns:420px 230px 380px 1fr;align-items:center;height:118px;border-top:1px solid #22324A}
.strip{position:absolute;left:96px;right:96px;bottom:30px;border-top:1px solid #2A3A52;padding-top:12px;font-size:27px;color:#8A9AB0;white-space:nowrap}</style>"""
RCSS = CSS + ".lab{font-size:22px;color:#7C8CA3}</style>"
ON_TARGET = 2.0   # |observed - forecast| <= 2 WTI counts as on target
LIM = 20          # fixed scorecard scale, -20..+20
WORD = {1: "One", 2: "Two", 3: "Three", 4: "Four"}


def die(msg):
    sys.exit(f"REFUSING TO BUILD: {msg}")


def load_day(path, D):
    rows = {r["park_code"]: r for r in csv.DictReader(open(path)) if r["park_date"] == D}
    fc, ob = {}, {}
    for p in P:
        r = rows.get(p)
        if not r or not r.get("forecast_wti", "").strip():
            die(f"{D} {p}: forecast_wti missing in {path}")
        fc[p] = float(r["forecast_wti"])
        ob[p] = float(r["actual_wti"]) if r.get("actual_wti", "").strip() else None
    return fc, ob


def _content_wti(path, D, kind, closed=()):
    """Step 14 content JSON -> {park: wti}. Requires status ready/released and target_date == D."""
    d = json.load(open(path))
    if d.get("target_date") != D: die(f"{path}: target_date {d.get('target_date')} != {D}")
    if d.get("status") not in ("ready", "released"): die(f"{path}: status {d.get('status')} ({d.get('held_reasons')})")
    v = {r.get("park_code"): r.get("wti") for r in d.get("parks", [])}
    out = {}
    for p in P:
        if p in closed:  # closed park: never shows a value; stray values are logged, not shown (AC 5)
            if v.get(p) is not None: print(f"closed_park_stray_{kind} {D} {p}={v[p]} ignored", file=sys.stderr)
            out[p] = None; continue
        if v.get(p) is None: die(f"{D} {p}: {kind} wti missing in {path}")
        out[p] = float(v[p])
    return out


def load_content(predicted, observed, D, closed=()):
    fc = _content_wti(predicted, D, "forecast", closed)
    ob = _content_wti(observed, D, "observed", closed) if observed else {p: None for p in P}
    return fc, ob


def boxes(hist, D):
    """Each park's own normal box: p25-p75 of historical WTI, Jan 1 2022 .. day before D."""
    h = pd.read_parquet(hist); h["park_date"] = pd.to_datetime(h.park_date); h = h[h.source == "historical"]
    y = str(dt.date.fromisoformat(D) - dt.timedelta(days=1))
    h = h[(h.park_date >= "2022-01-01") & (h.park_date <= y)]
    S = {}
    for p in P:
        s = h[h.park_code == p].wti
        if len(s) < 365: die(f"{p}: only {len(s)} history days before {D}")
        S[p] = s.quantile([.25, .75]).tolist()
    return S


def check_alt(al):
    if len(al) > ALT_MAX: die(f"alt text is {len(al)} chars, over the X limit of {ALT_MAX}")
    return al


def dist(S, p, v):  # how far outside its own box, in box widths (0 = inside)
    return max(v - S[p][1], S[p][0] - v, 0) / (S[p][1] - S[p][0])


def qual(S, p, v):
    return "Busier than normal" if v > S[p][1] else "Quieter than normal" if v < S[p][0] else "Normal"


def pick_line(vals, S, status=None):
    """Most-extreme line. Only open parks compete (closed and unknown are left out; a closed park is never a
    tie partner). Exact ties name every tied park in fixed order MK, EP, HS, AK. With one open park the line
    names it. No open park -> None (no line)."""
    status = status or {}
    op = [p for p in P if status.get(p, "open") == "open" and vals.get(p) is not None]
    if not op: return None
    word = lambda p: "busy" if vals[p] > S[p][1] else "quiet" if vals[p] < S[p][0] else "normal"
    o = [p for p in op if dist(S, p, vals[p]) > 0]
    if not o:
        if len(op) == 1: return HC.ONE_PARK_NORMAL.format(park=N[op[0]])
        return HC.ALL_FOUR_NORMAL if len(op) == len(P) else HC.ALL_OPEN_NORMAL
    top = max(dist(S, p, vals[p]) for p in o)
    t = [p for p in o if abs(dist(S, p, vals[p]) - top) < 1e-9]
    w = {p: word(p) for p in t}
    if len(t) == 1: return f"{N[t[0]]} {w[t[0]]}"
    if len(set(w.values())) == 1: return f"{', '.join(N[p] for p in t[:-1])} and {N[t[-1]]} {w[t[0]]}"
    return ", ".join(f"{N[p]} {w[p]}" for p in t)


def forecast_headline(fc, S, status=None):
    """No "today" (Fred, Oct 26: "Drop today from the headline"; the date is the kicker).
    "Name both parks on a tie" (Fred, Oct 26), in order MK, EP, HS, AK."""
    return pick_line(fc, S, status)


def gap(fc, ob, p): return round(ob[p] - fc[p] + 1e-9, 1)


def reply_headline(fc, ob, status=None):
    """Error-based: within 2 WTI = on target. Name the largest miss; count on-target parks.
    Only open parks are graded (closed/unknown left out of the pick, AC 8). Unchanged when all four are open."""
    status = status or {}
    Q = [p for p in P if status.get(p, "open") == "open" and ob.get(p) is not None and fc.get(p) is not None]
    if not Q: return None
    e = {p: gap(fc, ob, p) for p in Q}
    miss = sorted([p for p in Q if abs(e[p]) > ON_TARGET], key=lambda p: -abs(e[p]))
    on = len(Q) - len(miss)
    if not miss: return HC.ALL_FOUR_ON_TARGET if len(Q) == len(P) else HC.ALL_OPEN_ON_TARGET
    t = miss[0]; x = f"{abs(e[t]):.1f}"; lh = "low" if e[t] < 0 else "high"
    if on == 0:
        if all(e[p] < 0 for p in Q): return f"Every park came in low. {N[t]} by {x}"
        if all(e[p] > 0 for p in Q): return f"Every park came in high. {N[t]} by {x}"
        return f"Every park missed. {N[t]} {x} {lh}"
    if on == 1: return f"{N[t]} came in {x} {'below' if e[t] < 0 else 'above'} forecast"
    if on == 2: return f"{N[t]} {x} {lh}. Two parks on target"
    return f"{WORD[on]} parks on target. {N[t]} {x} {lh}"


def forecast_html(D, fc, S, status=None):
    status = status or {}; cl = [p for p in P if status.get(p) == "closed"]
    d = dt.date.fromisoformat(D); title = forecast_headline(fc, S, status)
    X = lambda v: (v - 10) / 25 * 300
    rows = ""
    for p in P:
        if p in cl:
            rows += f'<div class="r closed"><div style="font-size:42px;font-weight:600;white-space:nowrap">{N[p]}</div><div style="font-size:42px;color:#9FB0C6">Closed</div><div></div><div></div></div>'; continue
        v = fc[p]; qq = qual(S, p, v); hi = qq != "Normal"
        bar = (f'<svg width="300" height="34" style="overflow:visible"><line x1="0" x2="300" y1="17" y2="17" stroke="#22324A" stroke-width="2"/><rect x="{X(S[p][0]):.1f}" y="7" width="{X(S[p][1])-X(S[p][0]):.1f}" height="20" rx="3" fill="#E8EDF4" fill-opacity="0.12" stroke="#5A6B85"/>'
               + f'<circle cx="{X(v):.1f}" cy="17" r="8" fill="{"#994879" if hi else "#BD73A0"}"/>' + '</svg>')
        num = f'<span class="m" style="font-size:72px;font-weight:600;color:{"#fff" if hi else "#E8EDF4"}">{v:.1f}</span>'
        rows += (f'<div class="r"><div style="font-size:42px;font-weight:600;white-space:nowrap">{N[p]}</div><div>{num}</div>'
                 f'<div style="font-size:32px;white-space:nowrap;color:{"#BD73A0" if hi else "#9FB0C6"}{";font-weight:600" if hi else ""}">{qq}</div><div>{bar}</div></div>')
    kick = d.strftime('%A, %b %-d')
    sub = '<div style="display:grid;grid-template-columns:420px 230px 380px 1fr;font-size:22px;color:#7C8CA3;margin-bottom:6px"><span></span><span class="m">WTI forecast</span><span></span><span>shared scale, 10–35</span></div>'
    html = f'<html><head>{FCSS}</head><body><div class="k">{kick}</div><div class="h">{title or ""}</div>{sub}{rows}<div class="brand">TPCR · HazeyData</div></body></html>'
    pr = "; ".join(f"{N[p]} closed. No forecast shown" if p in cl else f"{N[p]} {fc[p]:.1f}, {qual(S,p,fc[p])} (normal range {S[p][0]:.1f}–{S[p][1]:.1f})" for p in P)
    alt = (f"{title + ': ' if title else ''}TPCR Wait Time Index (WTI) forecast, {kick}. Each park shows its forecast WTI, a one-word qualifier and a small bar "
           f"for its normal range (middle half of its days since Jan 2022) on a 10–35 scale. {pr}.")
    return title, html, alt


def reply_html(D, fc, ob, status=None):
    status = status or {}; cl = [p for p in P if status.get(p) == "closed"]
    d = dt.date.fromisoformat(D); T = reply_headline(fc, ob, status)
    for p in P:
        if p in cl: continue
        if abs(ob[p] - fc[p]) > LIM: die(f"{D} {p}: gap {ob[p]-fc[p]:+.1f} exceeds the fixed ±{LIM} scale")
    W = 560; c0 = W / 2; X = lambda e: c0 + e / LIM * c0; minus = lambda e: f"{e:+.1f}".replace("-", "−")
    G = "420px 640px 1fr"
    hd = (f'<div style="display:grid;grid-template-columns:{G};margin-bottom:6px" class="lab"><span></span><div style="position:relative;width:{W}px;height:28px;margin-left:20px">'
          f'<span style="position:absolute;left:0">← quieter than forecast</span><span style="position:absolute;right:0">busier than forecast →</span></div><span class="m" style="white-space:nowrap">WTI <span style="color:#E8EDF4">Observed</span> · Forecast</span></div>')
    rows = ""
    for p in P:
        if p in cl:
            rows += (f'<div class="closed" style="display:grid;grid-template-columns:{G};align-items:center;height:118px;border-top:1px solid #22324A"><div style="font-size:42px;font-weight:600;white-space:nowrap">{N[p]}</div>'
                     f'<div style="font-size:42px;color:#9FB0C6;margin-left:20px">Closed</div><div></div></div>'); continue
        e = gap(fc, ob, p); a, b = sorted([c0, X(e)]); lx = X(e) + (14 if e >= 0 else -14)
        rows += (f'<div style="display:grid;grid-template-columns:{G};align-items:center;height:118px;border-top:1px solid #22324A"><div style="font-size:42px;font-weight:600;white-space:nowrap">{N[p]}</div>'
                 f'<svg width="{W}" height="60" style="margin-left:20px;overflow:visible"><rect x="{a:.1f}" y="18" width="{max(b-a,3):.1f}" height="24" rx="3" fill="#BD73A0"/>'
                 f'<text x="{lx:.1f}" y="41" font-family="Geist Mono" font-size="30" font-weight="600" fill="#E8EDF4" text-anchor="{"start" if e>=0 else "end"}">{minus(e)}</text></svg>'
                 f'<div style="display:flex;align-items:baseline;gap:18px;white-space:nowrap"><span class="m" style="font-size:64px;font-weight:600">{ob[p]:.1f}</span><span class="m" style="font-size:28px;color:#9FB0C6">{fc[p]:.1f}</span></div></div>')
    ticks = "".join(f'<span class="m lab" style="position:absolute;left:{420+20+X(t)}px;transform:translateX(-50%)">{f"{t:+d}".replace("-","−") if t else "0"}</span>' for t in (-20, -10, 0, 10, 20))
    html = (f'<html><head>{RCSS}</head><body><div class="k">Observed · {d.strftime("%A, %b %-d")}</div><div class="h">{T or ""}</div>{hd}'
            f'<div style="position:relative">{rows}<div style="position:absolute;left:{420+20+c0-1}px;top:0;bottom:0;width:2px;background:#9FB0C6"></div></div>'
            f'<div style="position:relative;height:34px;margin-top:8px">{ticks}</div><div class="lab" style="margin-left:440px;width:{W}px;text-align:center;margin-top:4px">Bar = observed − forecast</div><div class="brand">TPCR · HazeyData</div></body></html>')
    vals = "; ".join(f"{N[p]} closed" if p in cl else f"{N[p]} observed {ob[p]:.1f}, forecast {fc[p]:.1f}, gap {minus(gap(fc,ob,p))}" for p in P)
    alt = (f"{T + ': ' if T else ''}TPCR observed Wait Time Index (WTI) for {d:%A, %b %-d, %Y}, the reply to that day's forecast. In the middle, a vertical zero line marks a perfect forecast; "
           f"each park's bar extends right if the day was busier than forecast or left if quieter, on a shared fixed −20 to +20 WTI scale, labelled with the gap (observed minus forecast). "
           f"On the right, each park's observed WTI in large type with its forecast WTI in small type beside it. {vals}.")
    return T, html, alt


def meta(D, fc, S, status, title):
    """Facts the forecast tweet text needs (tweet_copy.py): each park's card label, MK, EP, HS, AK order."""
    status = status or {}
    return {"date": D, "headline": title,
            "labels": {N[p]: "Closed" if status.get(p) == "closed" else qual(S, p, fc[p]) for p in P}}


def reply_meta(D, fc, ob, status, title):
    """Facts the reply tweet text needs (tweet_copy.py): each park's observed, forecast and gap (None = closed), and the
    graded parks (open, the ones the card headline counts)."""
    status = status or {}
    parks = {N[p]: None if status.get(p) == "closed" else {"ob": round(ob[p], 1), "fc": round(fc[p], 1), "gap": gap(fc, ob, p)} for p in P}
    return {"date": D, "headline": title, "parks": parks,
            "graded": [N[p] for p in P if status.get(p, "open") == "open" and parks[N[p]]]}


if __name__ == "__main__":
    a = argparse.ArgumentParser(); a.add_argument("date"); a.add_argument("out")
    a.add_argument("--history", required=True); a.add_argument("--predicted"); a.add_argument("--observed"); a.add_argument("--csv")
    a.add_argument("--forecast-only", action="store_true")
    a.add_argument("--park-hours", help="dimparkhours.csv: open/unknown only, never closure (AC 1d). Unset = non-closed parks open")
    a.add_argument("--closures", help="closures.csv (park,date,source_url,note); default data/closures.csv. Only closure source (R3)")
    a = a.parse_args(); D = str(dt.date.fromisoformat(a.date)); os.makedirs(a.out, exist_ok=True)
    if bool(a.predicted) == bool(a.csv): die("give exactly one of --predicted (live Step 14 content) or --csv")
    status = {}
    import park_status  # closures.csv is the only closure source (R3); dimparkhours only gives open/unknown
    try:
        closures = park_status.load_closures(a.closures or park_status.DEFAULT_CLOSURES)
    except park_status.ClosuresError as e:
        die(str(e))
    status = park_status.load_statuses(a.park_hours, P, D, closures)
    if a.park_hours or any(v == "closed" for v in status.values()):
        print(f"PARK STATUS {D}: {json.dumps(status)}")
    cl = {p for p in P if status.get(p) == "closed"}
    if len(cl) == len(P):
        open(f"{a.out}/skipped_all_closed", "w").write(D + "\n"); print(f"skipped_all_closed {D}"); sys.exit(0)
    if a.csv and cl: die("closed rows need --predicted content input")
    fc, ob = load_content(a.predicted, a.observed, D, cl) if a.predicted else load_day(a.csv, D); S = boxes(a.history, D)
    t, h, al = forecast_html(D, fc, S, status)
    json.dump(meta(D, fc, S, status, t), open(f"{a.out}/F-{D}.meta.json", "w"), indent=1)
    open(f"{a.out}/F-{D}.html", "w").write(h); open(f"{a.out}/F-{D}.alt.txt", "w").write(check_alt(al) + "\n"); print(f"FORECAST {D}: {t}")
    if a.forecast_only: sys.exit(0)
    if all(ob[p] is None for p in P if p not in cl): print(f"REPLY {D}: no observed yet, skipped"); sys.exit(0)
    if any(ob[p] is None for p in P if p not in cl): die(f"{D}: observed missing for {[p for p in P if ob[p] is None and p not in cl]} (partial)")
    t, h, al = reply_html(D, fc, ob, status)
    json.dump(reply_meta(D, fc, ob, status, t), open(f"{a.out}/R-{D}.meta.json", "w"), indent=1)
    open(f"{a.out}/R-{D}.html", "w").write(h); open(f"{a.out}/R-{D}.alt.txt", "w").write(check_alt(al) + "\n"); print(f"REPLY {D}: {t}")
