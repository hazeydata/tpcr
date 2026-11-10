#!/usr/bin/env python3
"""School Calendar Scraper v2 — Brave Search + web_fetch approach.

Strategy:
1. Brave Search for "{district name} school calendar 2025-2026"
2. Prioritize educounty.net, schoolcalendarinfo.com hits
3. Fetch page content (free web fetch, no Firecrawl needed for most)
4. Parse dates using battle-tested regex from confirmation_scraper
5. Firecrawl only as fallback for JS-heavy sites

This is MUCH cheaper than the Firecrawl-first approach.
"""

from __future__ import annotations
import argparse, csv, json, os, re, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import date, datetime
from pathlib import Path
from collections import defaultdict

# Import the date parsing functions from confirmation_scraper
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))
from confirmation_scraper import extract_dates, validate, MONTHS, parse_month_day

# --- Config ---
COMPREHENSIVE_FILE = BASE_DIR / "districts_comprehensive.csv"
RESULTS_FILE = BASE_DIR / "brave_scraper_results.json"
LOG_FILE = BASE_DIR / "brave_scraper.log"

BRAVE_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")

SAVE_INTERVAL = 25
REQUEST_DELAY = 0.5  # Be nice to APIs
BRAVE_DELAY = 1.1    # Brave free tier: 1 req/sec

# Priority sources (these have structured calendar data)
PRIORITY_DOMAINS = [
    'educounty.net',
    'schoolcalendarinfo.com',
]


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def brave_search(query: str) -> list[dict]:
    """Search Brave and return results."""
    params = urllib.parse.urlencode({
        'q': query,
        'count': 5,
    })
    url = f"https://api.search.brave.com/res/v1/web/search?{params}"
    req = urllib.request.Request(url, headers={
        'Accept': 'application/json',
        'X-Subscription-Token': BRAVE_API_KEY,
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        results = []
        for r in data.get('web', {}).get('results', []):
            results.append({
                'title': r.get('title', ''),
                'url': r.get('url', ''),
                'description': r.get('description', ''),
            })
        return results
    except Exception as e:
        log(f"    Brave search error: {e}")
        return []


def web_fetch(url: str, max_chars: int = 5000) -> str:
    """Fetch a URL and return text content."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; SchoolCalendarBot/1.0)',
        'Accept': 'text/html,application/xhtml+xml',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        content_type = resp.headers.get('Content-Type', '')
        if 'pdf' in content_type.lower():
            return ""  # Can't parse PDFs inline
        raw = resp.read(max_chars * 2)
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('latin-1')
        # Simple HTML to text: strip tags
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.S)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.S)
        text = re.sub(r'<[^>]+>', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&#\d+;', '', text)
        return text[:max_chars]
    except Exception as e:
        return ""


def firecrawl_scrape(url: str) -> str:
    """Firecrawl scrape as fallback."""
    if not FIRECRAWL_API_KEY:
        return ""
    payload = json.dumps({
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "timeout": 15000,
    }).encode()
    req = urllib.request.Request(
        "https://api.firecrawl.dev/v2/scrape",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=25)
        result = json.loads(resp.read())
        if result.get("success"):
            return result.get("data", {}).get("markdown", "")
    except Exception:
        pass
    return ""


def rank_urls(results: list[dict], district_name: str) -> list[str]:
    """Rank search result URLs by likelihood of containing calendar data."""
    scored = []
    for r in results:
        url = r['url']
        url_lower = url.lower()
        desc = r.get('description', '').lower()
        title = r.get('title', '').lower()
        score = 0

        # Priority domains
        for domain in PRIORITY_DOMAINS:
            if domain in url_lower:
                score += 30

        # Calendar-related content
        if '2025-2026' in url_lower or '25-26' in url_lower:
            score += 15
        if 'calendar' in url_lower:
            score += 10
        if 'spring break' in desc or 'first day' in desc:
            score += 10
        if any(m in desc for m in ['march', 'april', 'august', 'september']):
            score += 5
            
        # Specific date mentions in description
        date_pattern = r'(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}'
        if re.search(date_pattern, desc, re.I):
            score += 15

        # Penalize PDFs (can't parse easily)
        if url_lower.endswith('.pdf'):
            score -= 20
            
        # Penalize wrong year
        if '2024-2025' in url_lower or '2023-2024' in url_lower:
            score -= 20

        scored.append((score, url))

    scored.sort(key=lambda x: -x[0])
    return [url for _, url in scored if _ > -10]


def extract_from_description(desc: str) -> dict:
    """Try to extract dates directly from search result descriptions."""
    result = {}
    desc_lower = desc.lower()
    
    # Spring break from description: "Spring Break is from March 23-27, 2026"
    sb_match = re.search(
        r'spring\s+break\s+(?:is\s+)?(?:from\s+)?(\w+)\s+(\d{1,2})\s*[-–to]+\s*(\d{1,2}),?\s*(\d{4})',
        desc, re.I
    )
    if sb_match:
        month_str, d1, d2, year = sb_match.groups()
        start = parse_month_day(month_str, d1)
        end = parse_month_day(month_str, d2)
        if start and end:
            result['spring_break_start'] = start.isoformat()
            result['spring_break_end'] = end.isoformat()

    # Also try cross-month: "Spring Break is from March 30 - April 3"
    sb_match2 = re.search(
        r'spring\s+break[^.]*?(\w+)\s+(\d{1,2})[,\s]*[-–to]+\s*(\w+)\s+(\d{1,2})',
        desc, re.I
    )
    if sb_match2 and 'spring_break_start' not in result:
        m1, d1, m2, d2 = sb_match2.groups()
        start = parse_month_day(m1, d1)
        end = parse_month_day(m2, d2)
        if start and end:
            result['spring_break_start'] = start.isoformat()
            result['spring_break_end'] = end.isoformat()

    # Winter break
    wb_match = re.search(
        r'winter\s+(?:break|recess)[^.]*?(\w+)\s+(\d{1,2})',
        desc, re.I
    )
    if wb_match:
        start = parse_month_day(wb_match.group(1), wb_match.group(2))
        if start and start.month in (11, 12):
            result['winter_break_start'] = start.isoformat()

    return result


def process_district(district: dict) -> tuple[dict | None, str, str]:
    """Process one district. Returns (dates, source_url, method)."""
    name = district['district_name']
    state = district['state']
    
    # Build search query
    # Clean up district name for search
    clean_name = re.sub(r'\s*\(.*?\)', '', name)  # Remove parenthetical
    query = f'"{clean_name}" {state} school calendar 2025-2026 spring break'
    
    # Step 1: Brave Search
    results = brave_search(query)
    time.sleep(BRAVE_DELAY)
    
    if not results:
        # Try simpler query
        query2 = f'{clean_name} {state} school calendar 2025-2026'
        results = brave_search(query2)
        time.sleep(BRAVE_DELAY)
    
    if not results:
        return None, "", "no_results"

    # Step 1.5: Try extracting dates from search descriptions
    for r in results:
        desc_dates = extract_from_description(r.get('description', ''))
        if desc_dates and validate(desc_dates):
            return desc_dates, r['url'], "description"

    # Step 2: Rank and fetch URLs
    urls = rank_urls(results, name)
    
    for url in urls[:3]:
        url_lower = url.lower()
        
        # Skip PDFs
        if url_lower.endswith('.pdf'):
            continue
        
        # Try free web fetch first
        content = web_fetch(url)
        if content and len(content) > 100:
            dates = extract_dates(content)
            validated = validate(dates)
            if validated:
                return validated, url, "web_fetch"
        
        # Fallback to Firecrawl for JS-heavy sites
        if not content or len(content) < 100:
            md = firecrawl_scrape(url)
            if md and len(md) > 100:
                dates = extract_dates(md)
                validated = validate(dates)
                if validated:
                    return validated, url, "firecrawl"
        
        time.sleep(REQUEST_DELAY)

    return None, "", "parse_failed"


def load_results() -> dict:
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE) as f:
            return json.load(f)
    return {"confirmed": {}, "failed": {}, "stats": {}}


def save_results(results: dict):
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2, default=str)


def run_batch(limit: int = 0, min_enrollment: int = 0, max_enrollment: int = 999999,
              resume: bool = True, dry_run: bool = False):
    log("=" * 70)
    log("Brave Search Scraper v2")
    log("=" * 70)
    
    if not BRAVE_API_KEY and not dry_run:
        log("ERROR: Set BRAVE_SEARCH_API_KEY")
        sys.exit(1)
    
    # Load data
    log("Loading data...")
    with open(COMPREHENSIVE_FILE) as f:
        districts = list(csv.DictReader(f))
    
    results = load_results() if resume else {"confirmed": {}, "failed": {}, "stats": {}}
    already_done = set(results["confirmed"].keys()) | set(results["failed"].keys())
    
    # Also skip districts already confirmed in previous scraper
    try:
        with open(BASE_DIR / "confirmation_results.json") as f:
            prev = json.load(f)
        prev_confirmed = set(prev.get("confirmed", {}).keys())
        prev_failed = set(prev.get("failed", {}).keys())
    except:
        prev_confirmed = set()
        prev_failed = set()
    
    # Filter targets
    targets = []
    for d in districts:
        if d['confidence'] in ('confirmed', 'high'):
            continue
        enrollment = int(d.get('enrollment', 0) or 0)
        if enrollment < min_enrollment or enrollment > max_enrollment:
            continue
        leaid = d['nces_leaid']
        if leaid in already_done:
            continue
        # Don't re-attempt districts that previously confirmed (they're already good)
        if leaid in prev_confirmed:
            continue
        targets.append(d)
    
    targets.sort(key=lambda x: -int(x.get('enrollment', 0) or 0))
    
    log(f"Already done (this scraper): {len(already_done)}")
    log(f"Skipping prev confirmed: {len(prev_confirmed)}")
    log(f"To process: {len(targets)} districts")
    
    if limit:
        targets = targets[:limit]
        log(f"Limited to {len(targets)}")
    
    total_enrollment = sum(int(d.get('enrollment', 0) or 0) for d in targets)
    log(f"Total enrollment: {total_enrollment:,}")
    
    if dry_run:
        log("\n--- DRY RUN ---")
        for i, d in enumerate(targets[:30]):
            log(f"  {i+1}. {d['district_name']} ({d['state']}) — {int(d.get('enrollment',0)):,}")
        return
    
    confirmed_count = 0
    failed_count = 0
    method_counts = defaultdict(int)
    
    for i, d in enumerate(targets):
        leaid = d['nces_leaid']
        enrollment = int(d.get('enrollment', 0) or 0)
        log(f"[{i+1}/{len(targets)}] {d['district_name']} ({d['state']}) — {enrollment:,}")
        
        dates, source_url, method = process_district(d)
        
        if dates:
            confirmed_count += 1
            method_counts[method] += 1
            results["confirmed"][leaid] = {
                "name": d["district_name"], "state": d["state"],
                "enrollment": enrollment, "dates": dates,
                "source_url": source_url, "method": method,
                "timestamp": datetime.now().isoformat(),
            }
            sb = dates.get("spring_break_start", "N/A")
            fd = dates.get("first_day", "N/A")
            log(f"  ✅ spring={sb}, first={fd} [{method}] {source_url}")
        else:
            failed_count += 1
            method_counts[method] += 1
            results["failed"][leaid] = {
                "name": d["district_name"], "state": d["state"],
                "enrollment": enrollment, "method": method,
                "timestamp": datetime.now().isoformat(),
            }
            log(f"  ❌ {method}")
        
        if (confirmed_count + failed_count) % SAVE_INTERVAL == 0:
            save_results(results)
            total = confirmed_count + failed_count
            rate = confirmed_count / total * 100 if total else 0
            log(f"  --- Save: {confirmed_count}✅ {failed_count}❌ ({rate:.0f}%) | Methods: {dict(method_counts)} ---")
    
    # Final save
    results["stats"] = {
        "batch_size": len(targets),
        "confirmed": confirmed_count,
        "failed": failed_count,
        "success_rate": confirmed_count / max(1, confirmed_count + failed_count) * 100,
        "methods": dict(method_counts),
        "completed_at": datetime.now().isoformat(),
    }
    save_results(results)
    
    log("\n" + "=" * 70)
    log("BATCH COMPLETE")
    log(f"  Confirmed: {confirmed_count}")
    log(f"  Failed: {failed_count}")
    log(f"  Rate: {results['stats']['success_rate']:.1f}%")
    log(f"  Methods: {dict(method_counts)}")
    log("=" * 70)


def main():
    import urllib.parse  # needed for brave_search
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-enrollment", type=int, default=0)
    parser.add_argument("--max-enrollment", type=int, default=999999)
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    
    run_batch(
        limit=args.limit,
        min_enrollment=args.min_enrollment,
        max_enrollment=args.max_enrollment,
        resume=args.resume,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
