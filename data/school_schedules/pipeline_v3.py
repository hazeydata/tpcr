#!/usr/bin/env python3
"""Production School Calendar Scraper Pipeline v3.

Evolution of pipeline_v2.py with fundamental extraction changes:
- NEW: Extract ALL non-school days (not just named breaks)
- NEW: Try-everything approach (all 3 tiers, pick best result)
- NEW: Direct SQLite output using v3 schema
- NEW: Resume support via database check
- SAME: Three-tier evidence-based strategy, concurrency, cost tracking

Usage:
    python3 pipeline_v3.py                    # Process all districts
    python3 pipeline_v3.py --resume           # Skip already-done districts
    python3 pipeline_v3.py --max-districts 10 # Test mode
    python3 pipeline_v3.py --rescrape         # Re-scrape even if already in DB
"""

from __future__ import annotations
import argparse
import asyncio
import csv
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Set
import threading
import socket
import signal

# Import the generate_days module for SQLite operations
import sys
sys.path.append(str(Path(__file__).parent / "v3"))
from generate_days import (
    make_district_id, upsert_district, upsert_source, generate_days,
    parse_date, detect_school_year, init_db
)

# Global socket timeout
socket.setdefaulttimeout(30)

class DistrictTimeout(Exception):
    """Raised when a district takes too long to process."""
    pass

def _district_timeout_handler(signum, frame):
    raise DistrictTimeout("District processing timed out")

# Global rate limiter for Brave API
_brave_lock = threading.Lock()
_brave_last_call = 0.0

# File paths
BASE_DIR = Path(__file__).parent
V3_DIR = BASE_DIR / "v3"
NCES_FILE = BASE_DIR / "nces_all_districts.csv"
PROFILES_FILE = BASE_DIR / "district_profiles.json"
DB_PATH = V3_DIR / "school_schedules.db"
LOG_FILE = BASE_DIR / "pipeline_v3.log"

# API keys
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BRAVE_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")

# Config
MAX_WORKERS = 1  # Sequential processing for reliability
BRAVE_RATE_LIMIT = 1.1  # seconds between Brave API calls
REQUEST_DELAY = 0.3     # seconds between general requests
SAVE_INTERVAL = 10      # Save results every N districts
MAX_PDF_SIZE = 5_000_000  # 5MB max PDF download
MAX_CONTENT_CHARS = 12000  # Max content to send to LLM

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Known JS platform domains for Tier 3 targeting
JS_PLATFORMS = {
    'finalsite.net', 'finalsite.com',
    'schoolwires.net', 'schoolwires.com', 
    'tandemapp.com',
    'edlio.com', 'edl.io',
    'thrillshare.com',
    'myconnectsuite.com'
}

# NEW EXTRACTION PROMPT - Extract ALL non-school days
NEW_EXTRACTION_PROMPT = """You are extracting school calendar dates for {district_name} in {state}.

PRIORITY: 2025-2026 school year is preferred, but ANY valid school year is acceptable (2024-2025, 2026-2027, etc).

CRITICAL TASK: Extract EVERY non-school day from the calendar content. This includes:
- Spring Break, Winter Break, Fall Break, Thanksgiving Break
- Individual holidays (MLK Day, Presidents Day, Memorial Day, etc.)
- Teacher workdays/professional development days
- Half days/early release days
- Any other non-instructional days

You MUST follow these rules strictly:
1. ONLY extract dates that are EXPLICITLY stated in the content below.
2. For EACH date you extract, provide the EXACT QUOTE from the content that contains it.
3. If the content does not contain clear calendar dates for this specific district, return {{"status": "not_found", "reason": "explain why"}}.
4. Do NOT guess, infer, or use "typical" dates. If it's not written on the page, it's not found.
5. Check that the content is actually about {district_name} — not a different district.
6. Event feeds (sports, PTA meetings, concerts) are NOT school calendars. Ignore them.
7. If the page has MULTIPLE school years, extract 2025-2026 first. If 2025-2026 is not available, extract the most recent year available.
8. ALWAYS specify which school year the dates are from in the "school_year" field.
9. For multi-day breaks, specify BOTH start and end dates. For single-day events, set end_date to null.
10. Categorize each non-school day by type: BREAK, HOLIDAY, TEACHER_WORKDAY, or HALF_DAY.

Return ONLY valid JSON in this format:
{{
  "status": "found",
  "school_year": "YYYY-YYYY (e.g. 2025-2026 or 2024-2025)",
  "first_day": "YYYY-MM-DD or null",
  "last_day": "YYYY-MM-DD or null", 
  "non_school_days": [
    {{"date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD or null", "type": "BREAK|HOLIDAY|TEACHER_WORKDAY|HALF_DAY", "name": "Spring Break|Thanksgiving|MLK Day|etc"}},
    {{"date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD or null", "type": "BREAK|HOLIDAY|TEACHER_WORKDAY|HALF_DAY", "name": "Winter Break"}},
    {{"date": "YYYY-MM-DD", "end_date": null, "type": "HOLIDAY", "name": "Presidents Day"}},
    ...
  ],
  "district_email": "email@district.org or null",
  "contact_name": "Superintendent name or null",
  "evidence": {{
    "first_day_quote": "exact text from page or null",
    "last_day_quote": "exact text from page or null", 
    "non_school_days_quote": "exact text listing breaks/holidays from the calendar or null"
  }},
  "source_type": "district_pdf | district_website | aggregator",
  "confidence": "high | medium | low",
  "calendar_type": "traditional | year_round | modified"
}}

OR if not found:
{{
  "status": "not_found",
  "reason": "brief explanation"
}}

Content from {url}:
{content}"""


class CostTracker:
    """Track API costs across all services."""
    
    def __init__(self):
        self.brave_calls = 0
        self.anthropic_calls = 0
        self.anthropic_input_tokens = 0
        self.anthropic_output_tokens = 0
        self.firecrawl_calls = 0
        
    def log_brave_call(self):
        self.brave_calls += 1
        
    def log_anthropic_call(self, input_tokens: int = 0, output_tokens: int = 0):
        self.anthropic_calls += 1
        self.anthropic_input_tokens += input_tokens
        self.anthropic_output_tokens += output_tokens
        
    def log_firecrawl_call(self):
        self.firecrawl_calls += 1
        
    def get_summary(self) -> Dict[str, Any]:
        # Rough cost estimates based on current pricing
        anthropic_cost = (self.anthropic_input_tokens / 1000 * 0.003 + 
                         self.anthropic_output_tokens / 1000 * 0.015)
        brave_cost = self.brave_calls * 0.005  # ~$5 per 1000 searches
        firecrawl_cost = self.firecrawl_calls * 0.01  # ~$1 per 100 scrapes
        
        return {
            "brave_calls": self.brave_calls,
            "brave_estimated_cost": round(brave_cost, 2),
            "anthropic_calls": self.anthropic_calls,
            "anthropic_input_tokens": self.anthropic_input_tokens,
            "anthropic_output_tokens": self.anthropic_output_tokens,
            "anthropic_estimated_cost": round(anthropic_cost, 2),
            "firecrawl_calls": self.firecrawl_calls,
            "firecrawl_estimated_cost": round(firecrawl_cost, 2),
            "total_estimated_cost": round(anthropic_cost + brave_cost + firecrawl_cost, 2)
        }


class DistrictProcessor:
    """Process a single district through the three-tier strategy with try-everything approach."""
    
    def __init__(self, cost_tracker: CostTracker, profiles: Dict, nces_urls: Dict, db: sqlite3.Connection):
        self.cost_tracker = cost_tracker
        self.profiles = profiles
        self.nces_urls = nces_urls
        self.db = db
        
    def score_search_result(self, result: Dict, district_name: str) -> int:
        """Score a search result per collection_methodology.md strategy."""
        score = 0
        url = result.get('url', '').lower()
        title = result.get('title', '').lower()
        desc = result.get('description', '').lower()
        combined = url + ' ' + title + ' ' + desc
        
        # PDF bonus (Tier 1 priority)
        if url.endswith('.pdf') or 'pdf' in title:
            score += 4
        # Calendar-specific terms in title
        if any(t in title for t in ['student calendar', 'school calendar', 'academic calendar', 'school year calendar']):
            score += 5
        elif 'calendar' in title:
            score += 3
        # Year reference
        if '2025-2026' in combined or ('2025' in combined and '2026' in combined):
            score += 3
        # Official domain
        if any(d in url for d in ['.k12.', '.edu']):
            score += 3
        elif '.gov' in url:
            score += 2
        # Known hosting platforms
        if any(p in url for p in ['finalsite', 'thrillshare', 'core-docs', 'myconnectsuite',
                                   'campussuite', 'sharpschool', 'edl.io']):
            score += 2
        # District name in URL
        clean = re.sub(r'[^a-z]', '', district_name.lower().split()[0])
        if len(clean) > 3 and clean in url:
            score += 2
        # Penalty: aggregator sites
        if any(a in url for a in ['schools-calendar.com', 'educounty', 'schooldistrictcalendar',
                                   'niche.com', 'greatschools.org', 'usnews.com']):
            score -= 3
        # Penalty: definitely-not-calendar content
        if any(t in combined for t in ['salary', 'handbook', 'budget', 'employment',
                                        'nces.ed.gov', 'census.gov']):
            score -= 5
            
        return score
        
    def brave_search(self, query: str) -> List[Dict]:
        """Search Brave with global rate limiting."""
        global _brave_last_call
        if not BRAVE_API_KEY:
            return []
        
        # Global rate limit
        with _brave_lock:
            now = time.time()
            elapsed = now - _brave_last_call
            if elapsed < BRAVE_RATE_LIMIT:
                time.sleep(BRAVE_RATE_LIMIT - elapsed)
            _brave_last_call = time.time()
            
        params = urllib.parse.urlencode({'q': query, 'count': 5})
        url = f"https://api.search.brave.com/res/v1/web/search?{params}"
        req = urllib.request.Request(url, headers={
            'Accept': 'application/json',
            'X-Subscription-Token': BRAVE_API_KEY,
        })
        
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            self.cost_tracker.log_brave_call()
            
            return [
                {
                    'title': r.get('title', ''),
                    'url': r.get('url', ''),
                    'description': r.get('description', '')
                }
                for r in data.get('web', {}).get('results', [])
            ]
        except Exception as e:
            logger.warning(f"Brave search error: {e}")
            return []
            
    def fetch_pdf_text(self, url: str) -> str:
        """Download PDF and extract text via pdftotext."""
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            resp = urllib.request.urlopen(req, timeout=20)
            
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
                f.write(resp.read(MAX_PDF_SIZE))
                tmp_path = f.name
                
            result = subprocess.run(
                ['pdftotext', '-layout', tmp_path, '-'],
                capture_output=True, text=True, timeout=30
            )
            os.unlink(tmp_path)
            
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout[:MAX_CONTENT_CHARS]
                
        except Exception as e:
            logger.debug(f"PDF fetch/extract error: {e}")
            
        return ""
        
    def web_fetch(self, url: str) -> str:
        """Simple web fetch for HTML content."""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml',
        }
        req = urllib.request.Request(url, headers=headers)
        
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            content_type = resp.headers.get('Content-Type', '')
            
            if 'pdf' in content_type.lower():
                return "[PDF_DETECTED]"
                
            raw = resp.read(MAX_CONTENT_CHARS * 2)
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                text = raw.decode('latin-1')
                
            # Basic HTML cleaning
            text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.S)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.S)
            text = re.sub(r'<[^>]+>', '\n', text)
            text = re.sub(r'\n{3,}', '\n\n', text)
            text = re.sub(r'&amp;', '&', text)
            text = re.sub(r'&nbsp;', ' ', text)
            text = re.sub(r'&#\d+;', '', text)
            
            return text.strip()[:MAX_CONTENT_CHARS]
            
        except Exception as e:
            logger.debug(f"Web fetch error: {e}")
            return ""
            
    def firecrawl_scrape(self, url: str) -> str:
        """Firecrawl scrape for JS-rendered pages."""
        if not FIRECRAWL_API_KEY:
            return ""
            
        payload = json.dumps({
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
            "timeout": 20000,
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
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read())
            self.cost_tracker.log_firecrawl_call()
            
            if result.get("success"):
                md = result.get("data", {}).get("markdown", "")
                return md[:MAX_CONTENT_CHARS]
                
        except Exception as e:
            logger.debug(f"Firecrawl error: {e}")
            
        return ""
        
    def llm_extract_with_evidence(self, content: str, district_name: str, state: str, url: str) -> Optional[Dict]:
        """Use Claude Sonnet for evidence-based extraction with NEW prompt."""
        if not ANTHROPIC_API_KEY or not content or len(content) < 50:
            return None
            
        prompt = NEW_EXTRACTION_PROMPT.format(
            district_name=district_name,
            state=state,
            url=url,
            content=content[:8000]
        )
        
        payload = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": 800,  # Increased for longer non_school_days array
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
        
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read())
            self.cost_tracker.log_anthropic_call()
            
            text = result.get("content", [{}])[0].get("text", "")
            json_match = re.search(r'\{.*\}', text, re.S)
            
            if json_match:
                return json.loads(json_match.group())
                
        except Exception as e:
            logger.debug(f"LLM extraction error: {e}")
            
        return None
        
    def is_js_platform(self, url: str) -> bool:
        """Check if URL is from a known JS platform."""
        return any(platform in url.lower() for platform in JS_PLATFORMS)
        
    def _is_wrong_pdf(self, url: str, title: str) -> bool:
        """Filter out non-calendar PDFs."""
        lower_url = url.lower()
        lower_title = title.lower()
        combined = lower_url + ' ' + lower_title
        reject_terms = ['salary', 'handbook', 'budget', 'financial', 'application',
                        'employment', 'job', 'bid', 'contract', 'audit', 'meal',
                        'nutrition', 'transportation', 'bus route', 'supply list',
                        'dress code', 'enrollment form', 'registration form']
        return any(term in combined for term in reject_terms)

    def tier1_pdf_search(self, district_name: str, state: str) -> List[Dict]:
        """Tier 1: PDF-first search strategy."""
        attempts = []
        clean_name = re.sub(r'\s*\(.*?\)', '', district_name)
        
        queries = [
            f'"{clean_name}" "2025-2026" school calendar',
            f'"{clean_name}" {state} student calendar 2025-2026 PDF',
            f'"{clean_name}" school district calendar PDF',
        ]
        
        seen_urls = set()
        
        for query in queries:
            results = self.brave_search(query)
            
            # Score and sort results
            scored = [(self.score_search_result(r, district_name), r) for r in results]
            scored.sort(key=lambda x: -x[0])
            
            for score, result in scored[:3]:
                url = result['url']
                title = result.get('title', '')
                
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                
                if self._is_wrong_pdf(url, title):
                    logger.debug(f"    Skipping non-calendar PDF: {title[:60]}")
                    continue
                
                # Try PDFs first
                if url.lower().endswith('.pdf') or 'pdf' in title.lower():
                    content = self.fetch_pdf_text(url)
                    if content and len(content) > 100:
                        attempts.append({
                            'url': url,
                            'content': content,
                            'source_type': 'district_pdf',
                            'method': 'tier1_pdf_search',
                            'search_query': query,
                            'score': score
                        })
                        if len(attempts) >= 3:
                            return attempts
                else:
                    # Also try non-PDF HTML pages from search results
                    content = self.web_fetch(url)
                    if content and len(content) > 200:
                        attempts.append({
                            'url': url,
                            'content': content,
                            'source_type': 'district_website',
                            'method': 'tier1_html_search',
                            'search_query': query,
                            'score': score
                        })
                        if len(attempts) >= 5:
                            return attempts
                        
                time.sleep(REQUEST_DELAY)
                
            if len(attempts) >= 2:
                break
                
        return attempts
        
    def tier2_district_website(self, nces_id: str, district_name: str) -> List[Dict]:
        """Tier 2: District website HTML search."""
        attempts = []
        base_url = self.nces_urls.get(nces_id, "")
        
        if not base_url:
            return attempts
            
        # Normalize URL
        if not base_url.startswith('http'):
            base_url = 'https://' + base_url
        base_url = base_url.rstrip('/')
        
        # Try calendar page paths
        for path in ['/calendar', '/school-calendar', '/calendars', '']:
            try_url = base_url + path
            content = self.web_fetch(try_url)
            
            if content == "[PDF_DETECTED]":
                content = self.fetch_pdf_text(try_url)
                if content:
                    attempts.append({
                        'url': try_url,
                        'content': content,
                        'source_type': 'district_pdf',
                        'method': 'tier2_website_pdf',
                        'score': 5
                    })
                    return attempts
                    
            elif content and len(content) > 200:
                if ('2025' in content or '2026' in content or 
                    'calendar' in content.lower()):
                    attempts.append({
                        'url': try_url,
                        'content': content,
                        'source_type': 'district_website',
                        'method': 'tier2_website_html',
                        'score': 3
                    })
                    return attempts
                    
            time.sleep(REQUEST_DELAY)
            
        return attempts
        
    def tier3_firecrawl_js(self, nces_id: str, district_name: str, state: str) -> List[Dict]:
        """Tier 3: Firecrawl for known JS platforms."""
        attempts = []
        base_url = self.nces_urls.get(nces_id, "")
        
        # Only use Firecrawl if district is on known JS platform
        if not base_url or not self.is_js_platform(base_url):
            return attempts
            
        # Normalize URL
        if not base_url.startswith('http'):
            base_url = 'https://' + base_url
        base_url = base_url.rstrip('/')
        
        # Try calendar page with Firecrawl
        try_url = base_url + '/calendar'
        content = self.firecrawl_scrape(try_url)
        
        if content and len(content) > 200:
            attempts.append({
                'url': try_url,
                'content': content,
                'source_type': 'district_website',
                'method': 'tier3_firecrawl',
                'score': 2
            })
            
        return attempts
        
    def score_extraction(self, extraction: Dict) -> int:
        """Score an extraction result to pick the best one."""
        if not extraction or extraction.get('status') != 'found':
            return 0
        
        score = 0
        
        # Score based on number of non-school days found
        non_school_days = extraction.get('non_school_days', [])
        score += len(non_school_days) * 2  # 2 points per non-school day
        
        # Bonus for having first/last day
        if extraction.get('first_day'):
            score += 5
        if extraction.get('last_day'):
            score += 5
        
        # Confidence bonus
        confidence = extraction.get('confidence', 'medium')
        if confidence == 'high':
            score += 10
        elif confidence == 'medium':
            score += 5
        
        # Year bonus (prefer 2025-2026)
        school_year = extraction.get('school_year', '')
        if '2025-2026' in school_year:
            score += 15
        elif '2024-2025' in school_year or '2026-2027' in school_year:
            score += 10
        
        # Contact info bonus
        if extraction.get('district_email'):
            score += 3
        if extraction.get('contact_name'):
            score += 2
        
        return score

    def _has_minimum_data(self, extraction: Dict) -> bool:
        """Check if extraction has enough useful data to count as 'found'."""
        if not extraction or extraction.get('status') != 'found':
            return False
        
        # Must have at least 3 non-school days OR (first_day + last_day)
        non_school_days = extraction.get('non_school_days', [])
        has_non_school_days = len(non_school_days) >= 3
        has_year_boundaries = extraction.get('first_day') and extraction.get('last_day')
        
        return has_non_school_days or has_year_boundaries

    def update_district_profile(self, nces_id: str, district: Dict, attempts: List[Dict], 
                              result: Dict, tier_used: str):
        """Update district profile with collection intelligence."""
        profile = self.profiles.get(nces_id, {
            'nces_id': nces_id,
            'name': district['lea_name'],
            'state': district['st'],
            'sources': [],
            'failed_sources': [],
            'search_strategies': {'queries_tried': []},
            'collection_history': {}
        })
        
        # Update search queries tried
        for attempt in attempts:
            if 'search_query' in attempt:
                query = attempt['search_query']
                if query not in profile['search_strategies']['queries_tried']:
                    profile['search_strategies']['queries_tried'].append(query)
                    
        # Record successful source
        if result['status'] == 'found':
            source_record = {
                'url': result['source_url'],
                'type': 'pdf' if 'pdf' in result['source_type'] else 'html',
                'method': result.get('method', tier_used),
                'school_year': result.get('school_year', '2025-2026'),
                'quality': result.get('confidence', 'medium'),
                'last_checked': datetime.now().isoformat()
            }
            profile['sources'].append(source_record)
            
            # Update collection history
            detected_year = result.get('school_year', '2025-2026')
            profile['collection_history'][detected_year] = {
                'non_school_days': result.get('non_school_days', []),
                'confidence': result.get('confidence', 'medium'),
                'tier_used': tier_used,
                'collected_date': datetime.now().isoformat()
            }
        else:
            # Record failed attempts
            for attempt in attempts:
                failed_record = {
                    'url': attempt['url'],
                    'reason': 'no_evidence_found',
                    'attempted': datetime.now().isoformat()
                }
                profile['failed_sources'].append(failed_record)
                
        # Detect hosting platform
        if result.get('source_url'):
            url = result['source_url'].lower()
            for platform in JS_PLATFORMS:
                if platform in url:
                    profile['website'] = profile.get('website', {})
                    profile['website']['platform'] = platform.split('.')[0]
                    break
                    
        self.profiles[nces_id] = profile

    def save_to_database(self, district: Dict, extraction: Dict, source_url: str, 
                        method: str, nces_data: Dict) -> bool:
        """Save extraction results directly to SQLite database."""
        try:
            nces_id = district['leaid']
            name = district['lea_name']
            state = district.get('st') or district.get('state', 'XX')
            district_id = make_district_id(state, nces_id)
            
            # Determine school year
            non_school_days = extraction.get('non_school_days', [])
            first_day = parse_date(extraction.get('first_day'))
            last_day = parse_date(extraction.get('last_day'))
            
            # Try to get first spring break date for school year detection
            spring_break_date = None
            for nsd in non_school_days:
                if 'spring' in nsd.get('name', '').lower():
                    spring_break_date = parse_date(nsd.get('date'))
                    break
                    
            school_year = extraction.get('school_year') or detect_school_year(
                first_day, last_day, spring_break_date
            )
            
            # Upsert district
            upsert_district(self.db, district_id, nces_id, name, state, nces_data)
            
            # Convert v3 format to v2-compatible format for generate_days
            key_dates = {
                'first_day': extraction.get('first_day'),
                'last_day': extraction.get('last_day'),
                'non_school_days': non_school_days,
            }
            
            # Also add contact info if available
            if extraction.get('district_email'):
                self.db.execute("""
                    UPDATE dim_district SET district_email=?, updated_at=datetime('now')
                    WHERE district_id=?
                """, (extraction['district_email'], district_id))
                
            if extraction.get('contact_name'):
                self.db.execute("""
                    UPDATE dim_district SET contact_name=?, updated_at=datetime('now')
                    WHERE district_id=?
                """, (extraction['contact_name'], district_id))
            
            # Upsert source
            source_id = upsert_source(
                self.db, district_id, school_year, source_url, method,
                key_dates, extraction.get('confidence', 'medium')
            )
            
            # Generate day-level rows
            rows = generate_days(district_id, school_year, key_dates, source_id)
            
            # Delete existing days for this district/year and insert new ones
            self.db.execute("""
                DELETE FROM fact_school_day 
                WHERE district_id=? AND school_year=?
            """, (district_id, school_year))
            
            self.db.executemany("""
                INSERT INTO fact_school_day 
                (district_id, source_id, date, day_of_week, day_name, is_in_session,
                 day_type, break_name, notes, school_year)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            
            self.db.commit()
            return True
            
        except Exception as e:
            logger.error(f"Database save error for {district['lea_name']}: {e}")
            return False

    def process_district(self, district: Dict, nces_data: Dict) -> Dict:
        """Process a single district through all tiers with try-everything approach."""
        nces_id = district['leaid']
        name = district['lea_name']
        state = district.get('st') or district.get('state', 'XX')
        
        if not state or len(state) > 2:
            logger.warning(f"  Skipping {name} — no valid state code")
            return {
                'status': 'error', 'name': name, 'state': state,
                'error': 'no_valid_state_code', 'timestamp': datetime.now().isoformat()
            }
        
        logger.info(f"Processing: {name} ({state})")
        
        # Hard 90-second timeout per district
        old_handler = signal.signal(signal.SIGALRM, _district_timeout_handler)
        signal.alarm(90)
        try:
            return self._process_district_inner(district, nces_data)
        except DistrictTimeout:
            logger.warning(f"  ⏰ TIMEOUT: {name} ({state}) — skipping after 90s")
            return {
                'status': 'not_found', 'name': name, 'state': state,
                'error': 'timeout', 'search_queries_tried': [],
                'urls_tried': [], 'timestamp': datetime.now().isoformat()
            }
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

    def _process_district_inner(self, district: Dict, nces_data: Dict) -> Dict:
        """Inner processing logic with try-everything approach."""
        nces_id = district['leaid']
        name = district['lea_name']
        state = district.get('st') or district.get('state', 'XX')
        
        all_attempts = []
        all_extractions = []
        urls_tried = []
        queries_tried = []
        
        # NEW: Try ALL three tiers and collect all attempts
        
        # Tier 1: PDF-first search
        tier1_attempts = self.tier1_pdf_search(name, state)
        all_attempts.extend(tier1_attempts)
        
        # Tier 2: District website HTML
        tier2_attempts = self.tier2_district_website(nces_id, name)
        all_attempts.extend(tier2_attempts)
        
        # Tier 3: Firecrawl for JS platforms
        tier3_attempts = self.tier3_firecrawl_js(nces_id, name, state)
        all_attempts.extend(tier3_attempts)
        
        # Track all URLs and queries tried
        for attempt in all_attempts:
            urls_tried.append(attempt['url'])
            if 'search_query' in attempt:
                queries_tried.append(attempt['search_query'])
        
        # Try extraction on ALL attempts
        for attempt in all_attempts:
            extraction = self.llm_extract_with_evidence(
                attempt['content'], name, state, attempt['url']
            )
            
            if extraction and self._has_minimum_data(extraction):
                # Score this extraction
                score = self.score_extraction(extraction)
                all_extractions.append({
                    'extraction': extraction,
                    'attempt': attempt,
                    'score': score
                })
        
        # Pick the BEST extraction (highest score)
        if all_extractions:
            all_extractions.sort(key=lambda x: -x['score'])
            best = all_extractions[0]
            extraction = best['extraction']
            attempt = best['attempt']
            
            # Save to database
            if self.save_to_database(district, extraction, attempt['url'], 
                                   attempt['method'], nces_data):
                
                result = {
                    'status': 'found',
                    'name': name,
                    'state': state,
                    'enrollment': nces_data.get('enrollment'),
                    'tier_used': attempt['method'],
                    'non_school_days': extraction.get('non_school_days', []),
                    'first_day': extraction.get('first_day'),
                    'last_day': extraction.get('last_day'),
                    'district_email': extraction.get('district_email'),
                    'contact_name': extraction.get('contact_name'),
                    'evidence': extraction.get('evidence', {}),
                    'source_url': attempt['url'],
                    'source_type': attempt['source_type'],
                    'confidence': extraction.get('confidence', 'medium'),
                    'school_year': extraction.get('school_year', '2025-2026'),
                    'calendar_type': extraction.get('calendar_type', 'traditional'),
                    'search_queries_tried': queries_tried,
                    'urls_tried': urls_tried,
                    'firecrawl_used': len(tier3_attempts) > 0,
                    'method': attempt['method'],
                    'extraction_score': best['score'],
                    'alternatives_found': len(all_extractions) - 1,
                    'timestamp': datetime.now().isoformat()
                }
                
                # Update district profile
                self.update_district_profile(nces_id, district, all_attempts, result, attempt['method'])
                
                return result
                
        # No successful extraction
        result = {
            'status': 'not_found',
            'name': name,
            'state': state,
            'enrollment': nces_data.get('enrollment'),
            'tier_used': None,
            'non_school_days': None,
            'evidence': None,
            'source_url': None,
            'source_type': None,
            'confidence': None,
            'search_queries_tried': queries_tried,
            'urls_tried': urls_tried,
            'firecrawl_used': len(tier3_attempts) > 0,
            'attempts': len(all_attempts),
            'timestamp': datetime.now().isoformat()
        }
        
        # Update district profile with failed attempt
        self.update_district_profile(nces_id, district, all_attempts, result, None)
        
        return result


def load_nces_districts() -> List[Dict]:
    """Load regular public school districts from NCES data."""
    districts = []
    with open(NCES_FILE) as f:
        for row in csv.DictReader(f):
            # Only include regular public school districts
            if 'Regular' not in row.get('lea_type', ''):
                continue
            # Fix state field
            if not row.get('st') and row.get('state'):
                state_val = row['state'].strip()
                if len(state_val) == 2:
                    row['st'] = state_val.upper()
                else:
                    # Map full names to abbreviations
                    STATE_ABBREVS = {
                        'ALABAMA':'AL','ALASKA':'AK','ARIZONA':'AZ','ARKANSAS':'AR','CALIFORNIA':'CA',
                        'COLORADO':'CO','CONNECTICUT':'CT','DELAWARE':'DE','FLORIDA':'FL','GEORGIA':'GA',
                        'HAWAII':'HI','IDAHO':'ID','ILLINOIS':'IL','INDIANA':'IN','IOWA':'IA',
                        'KANSAS':'KS','KENTUCKY':'KY','LOUISIANA':'LA','MAINE':'ME','MARYLAND':'MD',
                        'MASSACHUSETTS':'MA','MICHIGAN':'MI','MINNESOTA':'MN','MISSISSIPPI':'MS',
                        'MISSOURI':'MO','MONTANA':'MT','NEBRASKA':'NE','NEVADA':'NV',
                        'NEW HAMPSHIRE':'NH','NEW JERSEY':'NJ','NEW MEXICO':'NM','NEW YORK':'NY',
                        'NORTH CAROLINA':'NC','NORTH DAKOTA':'ND','OHIO':'OH','OKLAHOMA':'OK',
                        'OREGON':'OR','PENNSYLVANIA':'PA','RHODE ISLAND':'RI','SOUTH CAROLINA':'SC',
                        'SOUTH DAKOTA':'SD','TENNESSEE':'TN','TEXAS':'TX','UTAH':'UT','VERMONT':'VT',
                        'VIRGINIA':'VA','WASHINGTON':'WA','WEST VIRGINIA':'WV','WISCONSIN':'WI',
                        'WYOMING':'WY','DISTRICT OF COLUMBIA':'DC',
                    }
                    row['st'] = STATE_ABBREVS.get(state_val.upper(), state_val[:2].upper())
            districts.append(row)
    return districts


def load_nces_urls() -> Dict[str, str]:
    """Load NCES website URLs."""
    url_map = {}
    if NCES_FILE.exists():
        with open(NCES_FILE) as f:
            for r in csv.DictReader(f):
                if r.get('website'):
                    url_map[r['leaid']] = r['website']
    return url_map


def load_profiles() -> Dict[str, Any]:
    """Load district profiles."""
    if PROFILES_FILE.exists():
        with open(PROFILES_FILE) as f:
            return json.load(f)
    return {}


def save_profiles(profiles: Dict[str, Any]):
    """Save district profiles."""
    with open(PROFILES_FILE, 'w') as f:
        json.dump(profiles, f, indent=2)


def get_already_processed_districts(db: sqlite3.Connection) -> Set[str]:
    """Get set of NCES IDs that are already in the database."""
    cursor = db.execute("SELECT nces_id FROM dim_district")
    return {row[0] for row in cursor.fetchall()}


def main():
    parser = argparse.ArgumentParser(description='Production School Calendar Pipeline v3')
    parser.add_argument('--resume', action='store_true', help='Skip already-done districts')
    parser.add_argument('--max-districts', type=int, default=0, help='Max districts to process (0=all)')
    parser.add_argument('--min-enrollment', type=int, default=0, help='Minimum enrollment threshold')
    parser.add_argument('--rescrape', action='store_true', help='Re-scrape even if already in DB')
    args = parser.parse_args()
    
    logger.info("=" * 70)
    logger.info("Production School Calendar Scraper Pipeline v3")
    logger.info("=" * 70)
    
    # Check API keys
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set")
        return 1
    if not BRAVE_API_KEY:
        logger.warning("BRAVE_SEARCH_API_KEY not set - search will be limited")
    if not FIRECRAWL_API_KEY:
        logger.warning("FIRECRAWL_API_KEY not set - Tier 3 disabled")
        
    # Initialize database
    logger.info("Initializing database...")
    db = init_db()
    
    # Load data
    logger.info("Loading districts and existing data...")
    all_districts = load_nces_districts()
    nces_urls = load_nces_urls()
    profiles = load_profiles()
    
    # Create NCES lookup
    nces_lookup = {d['leaid']: d for d in all_districts}
    
    # Filter and prioritize districts
    districts = all_districts
    if args.min_enrollment:
        districts = [d for d in districts if int(d.get('enrollment', 0) or 0) >= args.min_enrollment]
        
    # Sort by enrollment descending (biggest = most commercial value first)
    districts.sort(key=lambda d: int(d.get('enrollment', 0) or 0), reverse=True)
    
    if args.max_districts:
        districts = districts[:args.max_districts]
        
    logger.info(f"Total districts to process: {len(districts)}")
    
    # Resume support - skip already-processed districts
    if args.resume and not args.rescrape:
        already_processed = get_already_processed_districts(db)
        districts = [d for d in districts if d['leaid'] not in already_processed]
        logger.info(f"Resume mode: {len(already_processed)} already done, {len(districts)} remaining")
    elif args.rescrape:
        logger.info("Rescrape mode: will re-process districts even if already in DB")
        
    if not districts:
        logger.info("No districts to process!")
        return 0
        
    # Initialize tracking
    cost_tracker = CostTracker()
    stats = defaultdict(int)
    
    logger.info(f"Processing {len(districts)} districts sequentially")
    logger.info(f"Workers: {MAX_WORKERS} | Database: {DB_PATH}")
    
    processor = DistrictProcessor(cost_tracker, profiles, nces_urls, db)
    
    for i, district in enumerate(districts, 1):
        nces_id = district['leaid']
        nces_data = nces_lookup.get(nces_id, {})
        
        try:
            result = processor.process_district(district, nces_data)
            
            # Update stats
            stats[result['status']] += 1
            if result['status'] == 'found':
                tier = result.get('tier_used', 'unknown').replace('tier', '').replace('_', '')
                stats[f"tier_{tier}"] += 1
                
            # Logging
            if result['status'] == 'found':
                non_school_days = result.get('non_school_days', [])
                logger.info(f"  ✅ {district['lea_name']} ({district['st']}) — {result.get('tier_used')} — {len(non_school_days)} non-school days")
            else:
                logger.info(f"  ❌ {district['lea_name']} ({district['st']}) — {result['status']}")
                
            # Periodic saves
            if i % SAVE_INTERVAL == 0:
                save_profiles(profiles)
                logger.info(f"  Progress: {i}/{len(districts)} districts processed")
                
        except Exception as e:
            logger.error(f"  Error processing {district['lea_name']}: {e}")
            stats['error'] += 1
    
    # Final save
    save_profiles(profiles)
    db.close()
    
    # Final report
    total = len(districts)
    cost_summary = cost_tracker.get_summary()
    
    logger.info("=" * 70)
    logger.info("PIPELINE v3 COMPLETE")
    logger.info(f"Total processed: {total}")
    logger.info(f"Found: {stats['found']} ({stats['found']/total*100:.1f}%)")
    logger.info(f"Not found: {stats['not_found']} ({stats['not_found']/total*100:.1f}%)")
    logger.info(f"Errors: {stats['error']} ({stats['error']/total*100:.1f}%)")
    logger.info("")
    logger.info("Tier breakdown:")
    for tier in ['tier1pdfsearch', 'tier2websitehtml', 'tier2websitepdf', 'tier3firecrawl']:
        count = stats.get(f"tier_{tier}", 0)
        if count > 0:
            logger.info(f"  {tier}: {count}")
    logger.info("")
    logger.info("Cost summary:")
    logger.info(f"  Brave calls: {cost_summary['brave_calls']} (${cost_summary['brave_estimated_cost']})")
    logger.info(f"  Anthropic calls: {cost_summary['anthropic_calls']} (${cost_summary['anthropic_estimated_cost']})")
    logger.info(f"  Firecrawl calls: {cost_summary['firecrawl_calls']} (${cost_summary['firecrawl_estimated_cost']})")
    logger.info(f"  Total estimated cost: ${cost_summary['total_estimated_cost']}")
    logger.info("")
    logger.info(f"District profiles updated: {PROFILES_FILE}")
    logger.info(f"Database updated: {DB_PATH}")
    logger.info("=" * 70)
    
    return 0


if __name__ == '__main__':
    exit(main())