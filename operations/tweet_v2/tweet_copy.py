"""Tweet text for the v2 posts, from Writing's edge copy v2 (tweet-edge-copy-v2.md, Oct 26 2026). No invented copy:
when a case has no approved text, forecast_text/reply_text return None and the caller stays silent
(Fred, Oct 26: default silent). Values always 1 decimal, never rounded away (E8)."""
import datetime as dt

LIMIT = 280
SHORT = {"Magic Kingdom": "MK", "EPCOT": "EPCOT", "Hollywood Studios": "HS", "Animal Kingdom": "AK"}


def _day(D):
    return dt.date.fromisoformat(D).strftime("%a %b %-d")


def _fit(full, steps):
    """E8 cut order: try each shorter form in turn; the first that fits wins. None if nothing fits."""
    for t in [full] + steps:
        if len(t) <= LIMIT:
            return t
    return None


def forecast_text(m):
    """m = cards.py F-DATE.meta.json. Base pair / E2 / E3 / E6."""
    day, named, closed = _day(m["date"]), m["named"], m["closed"]
    if not m["open"]:
        return None  # E7: every park closed -> no post (also skipped_all_closed upstream)
    if not named:
        if closed:
            return None  # no approved copy for "all open parks normal" with a park closed
        return f"All four parks look normal tomorrow, {day}. Each one inside its own usual range. A good day to just go. Scorecard in this thread ↓"  # E3
    if len(named) == 2 and not closed:  # E2: tie, both the same way (copy exists for quiet only)
        if m["dirs"] == ["quiet", "quiet"]:
            return f"{named[0]} and {named[1]} look quiet. Tomorrow, {day}, both sit below their normal ranges. Every park vs its own normal, never each other. Scorecard in this thread ↓"
        return None
    if len(named) != 1:
        return None
    pk = named[0]; v = m["parks"][pk]
    if closed:
        if len(closed) != 1:
            return None  # E6 copy covers one closed park
        full = (f"{m['headline']}. Tomorrow, {day}: {pk} WTI {v['wti']:.1f} vs its normal {v['lo']:.1f}–{v['hi']:.1f}. "
                f"{closed[0]} is closed. Every park vs its own normal. Scorecard in this thread ↓")
        return _fit(full, [])
    base = f"{m['headline']}. Tomorrow, {day}: {pk} WTI {v['wti']:.1f} vs its normal {v['lo']:.1f}–{v['hi']:.1f}."
    tail = " Scorecard in this thread ↓"
    s1 = base + tail                                          # E8 step 1: drop the "every park" line
    s2 = s1.replace(pk, SHORT.get(pk, pk))                    # E8 step 2: short park names in text
    s3 = f"{m['headline']}. Tomorrow, {day}: {SHORT.get(pk, pk)} WTI {v['wti']:.1f}.{tail}"  # step 3: drop range
    return _fit(base + " Every park vs its own normal, never each other." + tail, [s1, s2, s3])


def reply_text(m):
    """Base pair reply. m = cards.py R-DATE.meta.json."""
    if not m.get("headline"):
        return None
    return _fit(f"{m['headline']}. Yesterday, {_day(m['date'])}, vs our forecast. Bar = observed − forecast.", [])
