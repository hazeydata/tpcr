"""Tweet text for the new-format (v2) posts (TPCR tweet text restyle, Oct 28 2026). Old-format text lives in
operations/scripts/tweet_v1_legacy.py and is unchanged. Values always 1 decimal. No colour-coded circles.
A text over X's 280 weighted limit returns None and the caller stays silent."""
import datetime as dt

LIMIT = 280
TAGS = "#WDW #DisneyWorld #WaitTimes"
EMOJI = ("⬇️", "⬆️", "⏬", "⏫", "✅")
LIGHT = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
COUNT = {1: "One park", 2: "Two parks", 3: "Three parks"}


def weighted_len(t):
    """X weighted count (twitter-text v3): each emoji counts 2, Latin and common punctuation 1, anything else 2."""
    n = 0
    for e in EMOJI:
        n += 2 * t.count(e); t = t.replace(e, "")
    return n + sum(1 if any(a <= ord(c) <= b for a, b in LIGHT) else 2 for c in t)


def _day(D):
    return dt.date.fromisoformat(D).strftime("%a, %b %-d")


def _names(xs):
    return xs[0] if len(xs) == 1 else f"{', '.join(xs[:-1])} and {xs[-1]}"


def _fit(t):
    return t if weighted_len(t) <= LIMIT else None


def forecast_summary(labels):
    """labels = {park: card label} in MK, EP, HS, AK order. Busier parks, then quieter, then closed, then the rest:
    one normal park left is named, two or more are "Other parks normal", none normal anywhere is "All four parks normal"."""
    parts = [f"{_names([p for p, q in labels.items() if q == lab])} {lab.lower()}."
             for lab in ("Busier than normal", "Quieter than normal", "Closed") if lab in labels.values()]
    normal = [p for p, q in labels.items() if q == "Normal"]
    if not parts:
        return "All four parks normal."
    if len(normal) == 1:
        parts.append(f"{normal[0]} normal.")
    elif normal:
        parts.append("Other parks normal.")
    return " ".join(parts)


def forecast_text(m):
    """m = cards.py F-DATE.meta.json."""
    if all(q == "Closed" for q in m["labels"].values()):
        return None
    return _fit(f"WDW Crowd FORECAST: {_day(m['date'])}\n{forecast_summary(m['labels'])}\n{TAGS}")


def tier(e, tiers):
    """|e| <= A on target, <= B off, else far off (tiers = (A, B) from config/tweet_format.json)."""
    a, b = tiers
    return "on" if abs(e) <= a else ("low" if e < 0 else "high") + ("" if abs(e) <= b else "2")


def reply_summary(m, tiers):
    """Counts the graded parks within A of forecast and names the largest miss (MK, EP, HS, AK order on a tie)."""
    g = {p: m["parks"][p]["gap"] for p in m["graded"]}
    miss = sorted([p for p in g if tier(g[p], tiers) != "on"], key=lambda p: -abs(g[p]))
    if not miss:
        return "All four parks close to forecast." if len(g) == len(m["parks"]) else "All open parks close to forecast."
    on = len(g) - len(miss)
    lead = f"{COUNT[on]} close to forecast." if on else "No park close to forecast."
    t = miss[0]
    return f"{lead} {t} {abs(g[t]):.1f} {'quieter' if g[t] < 0 else 'busier'}."


def reply_line(p, v, tiers):
    if v is None:
        return f"{p}: closed"
    mark = {"on": "✅", "low": "⬇️", "high": "⬆️", "low2": "⏬", "high2": "⏫"}[tier(v["gap"], tiers)]
    return f"{mark} {p}: {v['ob']:.1f} (forecast {v['fc']:.1f})"


def reply_text(m, tiers):
    """m = cards.py R-DATE.meta.json. No tiers (unset in config) -> None."""
    if not m.get("graded") or not tiers:
        return None
    rows = [reply_line(p, v, tiers) for p, v in m["parks"].items()]
    return _fit(f"WDW Crowds OBSERVED: {_day(m['date'])}\n{reply_summary(m, tiers)}\n\n" + "\n".join(rows) + "\n\nLower WTI = shorter waits.")
