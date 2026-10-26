#!/usr/bin/env python
"""TPCR daily cards: forecast card (weather5 style) + scorecard reply (design C).
usage: cards.py DATE OUTDIR [--csv PATH] [--history PATH] [--forecast-only]
Writes OUTDIR/F-DATE.html, F-DATE.alt.txt and (if observed exists) R-DATE.html, R-DATE.alt.txt.
REAL numbers only: any missing forecast (or, for the reply, observed) value is a hard error. Never estimates."""
import csv, sys, os, argparse, datetime as dt
import pandas as pd

P = ["MK", "EP", "HS", "AK"]
N = {"MK": "Magic Kingdom", "EP": "EPCOT", "HS": "Hollywood Studios", "AK": "Animal Kingdom"}
CSV = "/workspace/tpcr-weekly-2026-09-07/wti_forecast_vs_actual_last14.csv"
HIST = "/workspace/tpcr-ak-wti/wti.parquet"
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


def dist(S, p, v):  # how far outside its own box, in box widths (0 = inside)
    return max(v - S[p][1], S[p][0] - v, 0) / (S[p][1] - S[p][0])


def qual(S, p, v):
    return "Busier than normal" if v > S[p][1] else "Quieter than normal" if v < S[p][0] else "Normal"


def forecast_headline(fc, S):
    """Lead with the park furthest outside its own box; never ranks parks against each other."""
    o = [p for p in P if dist(S, p, fc[p]) > 0]
    if not o: return "All four parks normal today"
    p = max(o, key=lambda p: dist(S, p, fc[p]))
    return f"{N[p]} {'busy' if fc[p] > S[p][1] else 'quiet'} today"


def gap(fc, ob, p): return round(ob[p] - fc[p] + 1e-9, 1)


def reply_headline(fc, ob):
    """Error-based: within 2 WTI = on target. Name the largest miss; count on-target parks."""
    e = {p: gap(fc, ob, p) for p in P}
    miss = sorted([p for p in P if abs(e[p]) > ON_TARGET], key=lambda p: -abs(e[p]))
    on = len(P) - len(miss)
    if not miss: return "All four parks within 2 of forecast"
    t = miss[0]; x = f"{abs(e[t]):.1f}"; lh = "low" if e[t] < 0 else "high"
    if on == 0:
        if all(e[p] < 0 for p in P): return f"Every park came in low. {N[t]} by {x}"
        if all(e[p] > 0 for p in P): return f"Every park came in high. {N[t]} by {x}"
        return f"Every park missed. {N[t]} {x} {lh}"
    if on == 1: return f"{N[t]} came in {x} {'below' if e[t] < 0 else 'above'} forecast"
    if on == 2: return f"{N[t]} {x} {lh}. Two parks on target"
    return f"{WORD[on]} parks on target. {N[t]} {x} {lh}"


def forecast_html(D, fc, S):
    d = dt.date.fromisoformat(D); title = forecast_headline(fc, S)
    X = lambda v: (v - 10) / 25 * 300
    rows = ""
    for p in P:
        v = fc[p]; qq = qual(S, p, v); hi = qq != "Normal"
        bar = (f'<svg width="300" height="34" style="overflow:visible"><line x1="0" x2="300" y1="17" y2="17" stroke="#22324A" stroke-width="2"/><rect x="{X(S[p][0]):.1f}" y="7" width="{X(S[p][1])-X(S[p][0]):.1f}" height="20" rx="3" fill="#E8EDF4" fill-opacity="0.12" stroke="#5A6B85"/>'
               + f'<circle cx="{X(v):.1f}" cy="17" r="8" fill="{"#994879" if hi else "#BD73A0"}"/>' + '</svg>')
        num = f'<span class="m" style="font-size:72px;font-weight:600;color:{"#fff" if hi else "#E8EDF4"}">{v:.1f}</span>'
        rows += (f'<div class="r"><div style="font-size:42px;font-weight:600;white-space:nowrap">{N[p]}</div><div>{num}</div>'
                 f'<div style="font-size:32px;white-space:nowrap;color:{"#BD73A0" if hi else "#9FB0C6"}{";font-weight:600" if hi else ""}">{qq}</div><div>{bar}</div></div>')
    kick = f"Today · {d.strftime('%A, %b %-d')}"
    sub = '<div style="display:grid;grid-template-columns:420px 230px 380px 1fr;font-size:22px;color:#7C8CA3;margin-bottom:6px"><span></span><span class="m">WTI forecast</span><span></span><span>normal range, 10–35</span></div>'
    html = f'<html><head>{FCSS}</head><body><div class="k">{kick}</div><div class="h">{title}</div>{sub}{rows}<div class="brand">TPCR · HazeyData</div></body></html>'
    pr = "; ".join(f"{N[p]} {fc[p]:.1f}, {qual(S,p,fc[p])} (normal range {S[p][0]:.1f}–{S[p][1]:.1f})" for p in P)
    alt = (f"{title}: TPCR Wait Time Index (WTI) forecast, {kick}. Each park shows its forecast WTI, a one-word qualifier and a small bar "
           f"for its normal range (middle half of its days since Jan 2022) on a 10–35 scale. {pr}.")
    return title, html, alt


def reply_html(D, fc, ob):
    d = dt.date.fromisoformat(D); T = reply_headline(fc, ob)
    for p in P:
        if abs(ob[p] - fc[p]) > LIM: die(f"{D} {p}: gap {ob[p]-fc[p]:+.1f} exceeds the fixed ±{LIM} scale")
    W = 560; c0 = W / 2; X = lambda e: c0 + e / LIM * c0; minus = lambda e: f"{e:+.1f}".replace("-", "−")
    G = "420px 640px 1fr"
    hd = (f'<div style="display:grid;grid-template-columns:{G};margin-bottom:6px" class="lab"><span></span><div style="position:relative;width:{W}px;height:28px;margin-left:20px">'
          f'<span style="position:absolute;left:0">← quieter than forecast</span><span style="position:absolute;right:0">busier than forecast →</span></div><span class="m" style="white-space:nowrap">WTI <span style="color:#E8EDF4">Observed</span> · Forecast</span></div>')
    rows = ""
    for p in P:
        e = gap(fc, ob, p); a, b = sorted([c0, X(e)]); lx = X(e) + (14 if e >= 0 else -14)
        rows += (f'<div style="display:grid;grid-template-columns:{G};align-items:center;height:118px;border-top:1px solid #22324A"><div style="font-size:42px;font-weight:600;white-space:nowrap">{N[p]}</div>'
                 f'<svg width="{W}" height="60" style="margin-left:20px;overflow:visible"><rect x="{a:.1f}" y="18" width="{max(b-a,3):.1f}" height="24" rx="3" fill="#BD73A0"/>'
                 f'<text x="{lx:.1f}" y="41" font-family="Geist Mono" font-size="30" font-weight="600" fill="#E8EDF4" text-anchor="{"start" if e>=0 else "end"}">{minus(e)}</text></svg>'
                 f'<div style="display:flex;align-items:baseline;gap:18px;white-space:nowrap"><span class="m" style="font-size:64px;font-weight:600">{ob[p]:.1f}</span><span class="m" style="font-size:28px;color:#9FB0C6">{fc[p]:.1f}</span></div></div>')
    ticks = "".join(f'<span class="m lab" style="position:absolute;left:{420+20+X(t)}px;transform:translateX(-50%)">{f"{t:+d}".replace("-","−") if t else "0"}</span>' for t in (-20, -10, 0, 10, 20))
    html = (f'<html><head>{RCSS}</head><body><div class="k">Observed · {d.strftime("%A, %b %-d")}</div><div class="h">{T}</div>{hd}'
            f'<div style="position:relative">{rows}<div style="position:absolute;left:{420+20+c0-1}px;top:0;bottom:0;width:2px;background:#9FB0C6"></div></div>'
            f'<div style="position:relative;height:34px;margin-top:8px">{ticks}</div><div class="lab" style="margin-left:440px;width:{W}px;text-align:center;margin-top:4px">Bar = observed − forecast</div><div class="brand">TPCR · HazeyData</div></body></html>')
    vals = "; ".join(f"{N[p]} observed {ob[p]:.1f}, forecast {fc[p]:.1f}, gap {minus(gap(fc,ob,p))}" for p in P)
    alt = (f"{T}: TPCR observed Wait Time Index (WTI) for {d:%A, %b %-d, %Y}, the reply to that day's forecast. In the middle, a vertical zero line marks a perfect forecast; "
           f"each park's bar extends right if the day was busier than forecast or left if quieter, on a shared fixed −20 to +20 WTI scale, labelled with the gap (observed minus forecast). "
           f"On the right, each park's observed WTI in large type with its forecast WTI in small type beside it. {vals}.")
    return T, html, alt


if __name__ == "__main__":
    a = argparse.ArgumentParser(); a.add_argument("date"); a.add_argument("out")
    a.add_argument("--csv", default=CSV); a.add_argument("--history", default=HIST); a.add_argument("--forecast-only", action="store_true")
    a = a.parse_args(); D = str(dt.date.fromisoformat(a.date)); os.makedirs(a.out, exist_ok=True)
    fc, ob = load_day(a.csv, D); S = boxes(a.history, D)
    t, h, al = forecast_html(D, fc, S)
    open(f"{a.out}/F-{D}.html", "w").write(h); open(f"{a.out}/F-{D}.alt.txt", "w").write(al + "\n"); print(f"FORECAST {D}: {t}")
    if a.forecast_only: sys.exit(0)
    if all(ob[p] is None for p in P): print(f"REPLY {D}: no observed yet, skipped"); sys.exit(0)
    if any(ob[p] is None for p in P): die(f"{D}: observed missing for {[p for p in P if ob[p] is None]} (partial)")
    t, h, al = reply_html(D, fc, ob)
    open(f"{a.out}/R-{D}.html", "w").write(h); open(f"{a.out}/R-{D}.alt.txt", "w").write(al + "\n"); print(f"REPLY {D}: {t}")
