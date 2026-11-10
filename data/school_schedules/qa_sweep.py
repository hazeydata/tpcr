#!/usr/bin/env python3
"""QA Sweep — Cross-validate all school calendar dates with Firecrawl-first scraping.

For every district in the target set:
1. Fetch fresh content (Firecrawl primary, web_fetch + Brave fallback)
2. LLM extract dates
3. Compare with existing dates
4. Classify: verified / mismatch / new_find / still_missing

Designed for production quality — this data sells at $1,200/state.
"""

from __future__ import annotations
import argparse, csv, json, os, re, sys, time, threading, urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
COMPREHENSIVE_FILE = BASE_DIR / "districts_comprehensive.csv"
LLM_RESULTS_FILE = BASE_DIR / "llm_scraper_results.json"
CONFIRMATION_FILE = BASE_DIR / "confirmation_results.json"
TARGETS_FILE = BASE_DIR / "scrape_targets.json"
NCES_FILE = BASE_DIR / "nces_all_districts.csv"

QA_RESULTS_FILE = BASE_DIR / "qa_sweep_results.json"
QA_LOG_FILE = BASE_DIR / "qa_sweep.log"

FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BRAVE_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")

SAVE_INTERVAL = 25
FIRECRAWL_DELAY = 0.3    # lighter throttle — we have 50 concurrent slots, using 15
FIRECRAWL_TIMEOUT = 12   # shorter timeout — school sites that don't respond in 12s won't in 25s
BRAVE_DELAY = 1.1
REQUEST_DELAY = 0.2
MAX_FIRECRAWL_FAILURES = 3  # skip district after this many consecutive timeouts
WORKERS = 15             # concurrent Firecrawl requests (plan allows 50)

# ---------------------------------------------------------------------------
# NCES website URLs
# ---------------------------------------------------------------------------
def load_nces_urls() -> dict:
    url_map = {}
    if NCES_FILE.exists():
        with open(NCES_FILE) as f:
            for r in csv.DictReader(f):
                if r.get('website'):
                    url_map[r['leaid']] = r['website']
    return url_map

NCES_URLS = load_nces_urls()

# ---------------------------------------------------------------------------
# LLM extraction prompt (identical to llm_scraper.py)
# ---------------------------------------------------------------------------
LLM_EXTRACTION_PROMPT = """Extract the 2025-2026 school calendar dates from this content for {district_name} ({state}).

Return ONLY a JSON object with these fields (use YYYY-MM-DD format, null if not found):
{{
  "spring_break_start": "YYYY-MM-DD or null",
  "spring_break_end": "YYYY-MM-DD or null", 
  "winter_break_start": "YYYY-MM-DD or null",
  "winter_break_end": "YYYY-MM-DD or null",
  "first_day": "YYYY-MM-DD or null",
  "last_day": "YYYY-MM-DD or null",
  "school_year": "2025-2026 or null",
  "confidence": "high/medium/low"
}}

Rules:
- First day should be Aug-Sep 2025
- Last day should be May-Jun 2026
- Spring break should be Feb-May 2026
- Winter break should be Dec 2025 - Jan 2026
- Only extract 2025-2026 data, ignore other years
- "confidence" = "high" if dates are explicitly stated, "low" if inferred

Content:
{content}"""

# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()
_results_lock = threading.Lock()
_llm_semaphore = threading.Semaphore(3)  # max 3 concurrent LLM calls to avoid 429s

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        print(line, flush=True)
        with open(QA_LOG_FILE, "a") as f:
            f.write(line + "\n")

# ---------------------------------------------------------------------------
# Scrapers (Firecrawl-first for QA)
# ---------------------------------------------------------------------------
def firecrawl_scrape(url: str, max_chars: int = 10000) -> str:
    """Firecrawl scrape — returns markdown. Primary method for QA."""
    if not FIRECRAWL_API_KEY:
        return ""
    payload = json.dumps({
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "timeout": FIRECRAWL_TIMEOUT * 1000,
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
        resp = urllib.request.urlopen(req, timeout=FIRECRAWL_TIMEOUT + 3)
        result = json.loads(resp.read())
        if result.get("success"):
            md = result.get("data", {}).get("markdown", "")
            return md[:max_chars]
    except Exception as e:
        log(f"    Firecrawl error: {e}")
    return ""


def web_fetch(url: str, max_chars: int = 10000) -> str:
    """Simple web fetch — returns text."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; SchoolCalendarBot/1.0)',
        'Accept': 'text/html,application/xhtml+xml',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        content_type = resp.headers.get('Content-Type', '')
        if 'pdf' in content_type.lower():
            return ""
        raw = resp.read(max_chars * 2)
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('latin-1')
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.S)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.S)
        text = re.sub(r'<[^>]+>', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&#\d+;', '', text)
        return text[:max_chars]
    except Exception:
        return ""


def brave_search(query: str) -> list[dict]:
    """Search Brave and return results."""
    if not BRAVE_API_KEY:
        return []
    params = urllib.parse.urlencode({'q': query, 'count': 5})
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
        log(f"    Brave error: {e}")
        return []


def llm_extract(content: str, district_name: str, state: str) -> dict | None:
    """Use Claude Haiku to extract dates from content. Rate-limited for concurrency."""
    if not ANTHROPIC_API_KEY or not content or len(content) < 50:
        return None
    
    _llm_semaphore.acquire()
    try:
        return _llm_extract_inner(content, district_name, state)
    finally:
        _llm_semaphore.release()


def _llm_extract_inner(content: str, district_name: str, state: str) -> dict | None:
    """Inner LLM extraction with retry on 429."""
    prompt = LLM_EXTRACTION_PROMPT.format(
        district_name=district_name,
        state=state,
        content=content[:6000]
    )
    
    payload = json.dumps({
        "model": "claude-3-haiku-20240307",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()
    
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    
    for attempt in range(3):
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read())
            text = result.get("content", [{}])[0].get("text", "")
            json_match = re.search(r'\{[^}]+\}', text, re.S)
            if json_match:
                dates = json.loads(json_match.group())
                return dates
            return None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(2 ** (attempt + 1))  # 2s, 4s backoff
                continue
            log(f"    LLM error: {e}")
        except Exception as e:
            log(f"    LLM error: {e}")
    return None


def validate_dates(dates: dict) -> dict | None:
    """Validate extracted dates are reasonable for 2025-2026 school year."""
    if not dates:
        return None
    
    validated = {}
    
    def parse_date(s):
        if not s or s == 'null' or s == 'None':
            return None
        try:
            return date.fromisoformat(str(s))
        except (ValueError, TypeError):
            return None
    
    sb_start = parse_date(dates.get('spring_break_start'))
    sb_end = parse_date(dates.get('spring_break_end'))
    if sb_start and date(2026, 2, 1) <= sb_start <= date(2026, 5, 31):
        validated['spring_break_start'] = sb_start.isoformat()
        if sb_end and sb_start <= sb_end <= date(2026, 6, 15):
            validated['spring_break_end'] = sb_end.isoformat()
    
    wb_start = parse_date(dates.get('winter_break_start'))
    wb_end = parse_date(dates.get('winter_break_end'))
    if wb_start and date(2025, 11, 15) <= wb_start <= date(2026, 1, 15):
        validated['winter_break_start'] = wb_start.isoformat()
        if wb_end and wb_start <= wb_end <= date(2026, 1, 31):
            validated['winter_break_end'] = wb_end.isoformat()
    
    fd = parse_date(dates.get('first_day'))
    if fd and date(2025, 7, 15) <= fd <= date(2025, 9, 30):
        validated['first_day'] = fd.isoformat()
    
    ld = parse_date(dates.get('last_day'))
    if ld and date(2026, 5, 1) <= ld <= date(2026, 7, 15):
        validated['last_day'] = ld.isoformat()
    
    if 'spring_break_start' in validated:
        validated['school_year'] = '2025-2026'
        validated['confidence'] = dates.get('confidence', 'medium')
        return validated
    
    return None

# ---------------------------------------------------------------------------
# QA-specific content fetching (Firecrawl-first, more aggressive)
# ---------------------------------------------------------------------------
def firecrawl_fetch(url: str) -> str:
    """Firecrawl-only fetch with throttling. This is a QA run — Firecrawl is the point."""
    if url.lower().endswith('.pdf'):
        return ""
    
    content = firecrawl_scrape(url)
    time.sleep(FIRECRAWL_DELAY)
    if content and len(content) > 200:
        return content
    
    return ""


def find_calendar_content_qa(nces_id: str, name: str, state: str, 
                              was_missing: bool = False) -> tuple[str, str, str]:
    """QA-grade content fetching via Firecrawl. Returns (url, content, fetch_method).
    
    Strategy:
    1. Try NCES website + 2-3 calendar paths via Firecrawl (quick, limited attempts)
    2. Use Brave to FIND the right URL, then Firecrawl to scrape it
    3. For missing districts, try one extra Brave query variation
    
    Bail early on broken sites to avoid wasting time on timeouts.
    """
    fc_failures = 0  # track consecutive Firecrawl failures
    
    # Strategy 1: NCES website URL + limited calendar paths
    base_url = NCES_URLS.get(nces_id, "")
    if base_url:
        if not base_url.startswith('http'):
            base_url = 'https://' + base_url
        base_url = base_url.rstrip('/')
        
        # Only try 3 paths max — don't burn time on a broken site
        for path in ['/calendar', '/school-calendar', '']:
            if fc_failures >= MAX_FIRECRAWL_FAILURES:
                log(f"    Skipping remaining paths ({fc_failures} consecutive timeouts)")
                break
            try_url = base_url + path
            content = firecrawl_fetch(try_url)
            if content:
                fc_failures = 0
                if '2025' in content or '2026' in content or 'calendar' in content.lower():
                    return try_url, content, 'firecrawl_direct'
            else:
                fc_failures += 1
    
    # Strategy 2: Brave to find URL, Firecrawl to scrape
    if BRAVE_API_KEY:
        clean_name = re.sub(r'\s*\(.*?\)', '', name)
        
        queries = [f'"{clean_name}" {state} school calendar 2025-2026']
        if was_missing:
            queries.append(f'{clean_name} {state} spring break 2026')
        
        for query in queries:
            results = brave_search(query)
            time.sleep(BRAVE_DELAY)
            
            for r in results[:2]:  # only top 2 results
                url = r['url']
                content = firecrawl_fetch(url)
                if content and len(content) > 100:
                    return url, content, 'brave_firecrawl'
    
    return "", "", "none"

# ---------------------------------------------------------------------------
# Date comparison
# ---------------------------------------------------------------------------
def compare_dates(old_dates: dict, new_dates: dict) -> tuple[str, list[str]]:
    """Compare old and new dates. Returns (status, mismatches).
    
    status: 'verified' | 'mismatch' | 'partial_match'
    mismatches: list of fields that differ
    """
    if not old_dates or not new_dates:
        return 'no_comparison', []
    
    fields = ['spring_break_start', 'spring_break_end', 'first_day', 'last_day',
              'winter_break_start', 'winter_break_end']
    
    mismatches = []
    matches = 0
    compared = 0
    
    for field in fields:
        old_val = old_dates.get(field)
        new_val = new_dates.get(field)
        
        if old_val and new_val:
            compared += 1
            if old_val == new_val:
                matches += 1
            else:
                mismatches.append(f"{field}: {old_val} → {new_val}")
    
    if compared == 0:
        return 'no_comparison', []
    elif len(mismatches) == 0:
        return 'verified', []
    elif matches > len(mismatches):
        return 'partial_match', mismatches
    else:
        return 'mismatch', mismatches

# ---------------------------------------------------------------------------
# Load existing data
# ---------------------------------------------------------------------------
def load_existing_dates() -> dict:
    """Load all existing dates from both confirmed and LLM results."""
    existing = {}  # nces_id -> {dates: {...}, source: str}
    
    # Load confirmed results
    if CONFIRMATION_FILE.exists():
        with open(CONFIRMATION_FILE) as f:
            conf = json.load(f)
        confirmed = conf.get('confirmed', {})
        for nces_id, data in confirmed.items():
            if isinstance(data, dict) and data.get('dates'):
                existing[nces_id] = {
                    'dates': data['dates'],
                    'source': 'confirmed',
                }
    
    # Load LLM scraper results (override if found, since more recent)
    if LLM_RESULTS_FILE.exists():
        with open(LLM_RESULTS_FILE) as f:
            llm = json.load(f)
        for nces_id, data in llm.items():
            if isinstance(data, dict) and data.get('status') == 'found' and data.get('dates'):
                existing[nces_id] = {
                    'dates': data['dates'],
                    'source': 'llm_scraper',
                    'url': data.get('url', ''),
                }
    
    return existing

# ---------------------------------------------------------------------------
# Load all districts
# ---------------------------------------------------------------------------
def load_all_districts() -> list[dict]:
    """Load ALL districts from comprehensive file."""
    with open(COMPREHENSIVE_FILE) as f:
        all_districts = list(csv.DictReader(f))
    all_districts.sort(key=lambda r: int(r.get('enrollment', 0) or 0), reverse=True)
    return all_districts

# ---------------------------------------------------------------------------
# QA Results I/O
# ---------------------------------------------------------------------------
def load_qa_results() -> dict:
    if QA_RESULTS_FILE.exists():
        with open(QA_RESULTS_FILE) as f:
            return json.load(f)
    return {}

def save_qa_results(results: dict):
    with open(QA_RESULTS_FILE, 'w') as f:
        json.dump(results, f, indent=2)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='QA Sweep — cross-validate all school calendar dates')
    parser.add_argument('--resume', action='store_true', help='Resume from saved QA results')
    parser.add_argument('--test', type=int, default=0, help='Test mode: process N districts then stop')
    parser.add_argument('--missing-only', action='store_true', help='Only process previously missing districts')
    args = parser.parse_args()
    
    log("=" * 70)
    log("QA SWEEP — School Calendar Cross-Validation")
    log("=" * 70)
    
    if not ANTHROPIC_API_KEY:
        log("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)
    if not FIRECRAWL_API_KEY:
        log("WARNING: FIRECRAWL_API_KEY not set — QA quality will be limited")
    
    # Load everything
    log("Loading existing dates...")
    existing = load_existing_dates()
    log(f"  Existing dates: {len(existing)} districts")
    
    log("Loading all districts...")
    all_districts = load_all_districts()
    log(f"  Total districts: {len(all_districts)}")
    
    # Load scrape targets to know which are in scope
    with open(TARGETS_FILE) as f:
        targets = json.load(f)
    target_ids = {t['leaid'] for t in targets}
    log(f"  Target districts: {len(target_ids)}")
    
    # Filter to target set only
    districts = [d for d in all_districts if d['nces_leaid'] in target_ids]
    log(f"  Districts in scope: {len(districts)}")
    
    # Identify previously missing
    missing_ids = target_ids - set(existing.keys())
    log(f"  Previously missing: {len(missing_ids)}")
    
    if args.missing_only:
        districts = [d for d in districts if d['nces_leaid'] in missing_ids]
        log(f"  Missing-only mode: processing {len(districts)} districts")
    
    # Load existing QA results for resume
    qa_results = load_qa_results() if args.resume else {}
    done_ids = set(qa_results.keys())
    
    to_process = [d for d in districts if d['nces_leaid'] not in done_ids]
    log(f"  Already QA'd: {len(done_ids)}")
    log(f"  To process: {len(to_process)}")
    
    if not to_process:
        log("Nothing to process!")
        print_summary(qa_results)
        return
    
    # Stats
    stats = {'verified': 0, 'mismatch': 0, 'partial_match': 0, 
             'new_find': 0, 'still_missing': 0, 'no_comparison': 0}
    
    # Count existing stats from resumed results
    for r in qa_results.values():
        s = r.get('qa_status', 'still_missing')
        if s in stats:
            stats[s] += 1
    
    processed_count = 0
    
    def process_district(i: int, district: dict) -> tuple[str, dict]:
        """Process a single district. Returns (nces_id, result_dict)."""
        nces_id = district['nces_leaid']
        name = district['district_name']
        state = district['state']
        enrollment = int(district.get('enrollment', 0) or 0)
        was_missing = nces_id in missing_ids
        
        tag = " [MISSING]" if was_missing else ""
        log(f"[{i+1}/{len(to_process)}] {name} ({state}) — {enrollment:,}{tag}")
        
        # Fetch fresh content
        url, content, fetch_method = find_calendar_content_qa(
            nces_id, name, state, was_missing=was_missing
        )
        
        if not content:
            log(f"  ❌ no_content ({fetch_method}) — {name}")
            return nces_id, {
                'name': name, 'state': state, 'enrollment': enrollment,
                'qa_status': 'still_missing',
                'fetch_method': fetch_method,
                'had_existing': nces_id in existing,
                'existing_dates': existing.get(nces_id, {}).get('dates'),
                'existing_source': existing.get(nces_id, {}).get('source'),
                'timestamp': datetime.now().isoformat(),
            }
        
        # LLM extraction
        raw_dates = llm_extract(content, name, state)
        new_dates = validate_dates(raw_dates) if raw_dates else None
        
        if new_dates:
            old_data = existing.get(nces_id, {})
            old_dates = old_data.get('dates', {})
            
            if was_missing or not old_dates:
                qa_status = 'new_find'
                mismatches = []
                log(f"  🆕 NEW: spring={new_dates.get('spring_break_start', 'N/A')}, first={new_dates.get('first_day', 'N/A')} — {name}")
            else:
                qa_status, mismatches = compare_dates(old_dates, new_dates)
                if qa_status == 'verified':
                    log(f"  ✅ VERIFIED: spring={new_dates.get('spring_break_start', 'N/A')} — {name}")
                elif qa_status == 'mismatch':
                    log(f"  ⚠️  MISMATCH: {'; '.join(mismatches)} — {name}")
                elif qa_status == 'partial_match':
                    log(f"  🔶 PARTIAL: {'; '.join(mismatches)} — {name}")
                else:
                    log(f"  ℹ️  {qa_status}: spring={new_dates.get('spring_break_start', 'N/A')} — {name}")
            
            return nces_id, {
                'name': name, 'state': state, 'enrollment': enrollment,
                'qa_status': qa_status,
                'new_dates': new_dates,
                'existing_dates': old_dates if old_dates else None,
                'existing_source': old_data.get('source'),
                'mismatches': mismatches if mismatches else None,
                'url': url,
                'fetch_method': fetch_method,
                'timestamp': datetime.now().isoformat(),
            }
        else:
            old_data = existing.get(nces_id, {})
            status_key = 'still_missing' if was_missing else 'no_comparison'
            log(f"  ❌ llm_no_dates ({fetch_method}) — {name}")
            return nces_id, {
                'name': name, 'state': state, 'enrollment': enrollment,
                'qa_status': status_key,
                'fetch_method': fetch_method,
                'had_existing': nces_id in existing,
                'existing_dates': old_data.get('dates'),
                'existing_source': old_data.get('source'),
                'url': url,
                'note': 'Had content but LLM could not extract dates',
                'timestamp': datetime.now().isoformat(),
            }
    
    # Test mode: sequential
    if args.test:
        for i, district in enumerate(to_process[:args.test]):
            nces_id, result = process_district(i, district)
            with _results_lock:
                qa_results[nces_id] = result
                stats[result['qa_status']] += 1
                processed_count += 1
        save_qa_results(qa_results)
        log(f"Test mode: stopped after {args.test} districts")
        print_summary(qa_results)
        return
    
    # Full run: threaded
    workers = WORKERS
    log(f"Running with {workers} concurrent workers")
    
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for i, district in enumerate(to_process):
            future = executor.submit(process_district, i, district)
            futures[future] = district
        
        for future in as_completed(futures):
            try:
                nces_id, result = future.result()
                with _results_lock:
                    qa_results[nces_id] = result
                    qa_status = result.get('qa_status', 'still_missing')
                    if qa_status in stats:
                        stats[qa_status] += 1
                    processed_count += 1
                    
                    if processed_count % SAVE_INTERVAL == 0:
                        save_qa_results(qa_results)
                        log(f"  --- [{processed_count}/{len(to_process)}] Save: {dict(stats)} ---")
            except Exception as e:
                log(f"  ❗ Worker error: {e}")
    
    # Final save
    save_qa_results(qa_results)
    print_summary(qa_results)


def print_summary(qa_results: dict):
    """Print final QA summary."""
    from collections import Counter
    
    status_counts = Counter()
    status_enrollment = Counter()
    
    for r in qa_results.values():
        s = r.get('qa_status', 'unknown')
        status_counts[s] += 1
        status_enrollment[s] += r.get('enrollment', 0)
    
    total = sum(status_counts.values())
    total_enrollment = sum(status_enrollment.values())
    
    log("=" * 70)
    log("QA SWEEP — FINAL RESULTS")
    log("=" * 70)
    log(f"Total processed: {total}")
    log("")
    
    for status in ['verified', 'partial_match', 'mismatch', 'new_find', 
                    'no_comparison', 'still_missing']:
        count = status_counts.get(status, 0)
        enroll = status_enrollment.get(status, 0)
        pct = count / total * 100 if total else 0
        icon = {'verified': '✅', 'partial_match': '🔶', 'mismatch': '⚠️ ',
                'new_find': '🆕', 'no_comparison': 'ℹ️ ', 'still_missing': '❌'}.get(status, '?')
        log(f"  {icon} {status:20s}: {count:6d} ({pct:5.1f}%) — {enroll:>12,} students")
    
    log("")
    
    # Quality score
    high_quality = status_counts.get('verified', 0) + status_counts.get('new_find', 0)
    questionable = status_counts.get('mismatch', 0)
    quality_pct = high_quality / total * 100 if total else 0
    
    log(f"Quality score: {quality_pct:.1f}% high-confidence")
    if questionable:
        log(f"⚠️  {questionable} mismatches need manual review")
    log("=" * 70)


if __name__ == '__main__':
    main()
