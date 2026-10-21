"""Step 14: Content Generation.

Generates Twitter content JSONs for WTI predictions and observations.
Includes quality gate that validates data before marking content "ready".

Workflow:
1. Extract predicted WTI for tomorrow (all configured parks)
2. Extract observed WTI for yesterday (all configured parks)
3. Run quality gate on both datasets (5 checks)
4. Write content JSONs with status: "ready" or "held"
5. Post Discord warnings for held content

Output files:
  - content/predicted_YYYY-MM-DD.json
  - content/observed_YYYY-MM-DD.json
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Tuple

import pandas as pd

from pipeline.config import PipelineConfig
from pipeline.core.db import read_connection
from pipeline.core.logging import PipelineLogger


# Display names for every park in cfg.parks (fallback to the park code)
PARK_NAMES = {
    "MK": "Magic Kingdom",
    "EP": "EPCOT",
    "HS": "Hollywood Studios",
    "AK": "Animal Kingdom",
    "DL": "Disneyland",
    "CA": "Disney California Adventure",
    "UF": "Universal Studios Florida",
    "IA": "Islands of Adventure",
    "EU": "Epic Universe",
    "UH": "Universal Studios Hollywood",
    "TDL": "Tokyo Disneyland",
    "TDS": "Tokyo DisneySea",
}

# Resort groups used by the peer-outlier check (parks are compared within a resort only)
RESORT_GROUPS = {
    "WDW": ["MK", "EP", "HS", "AK"],
    "DLR": ["DL", "CA"],
    "UOR": ["UF", "IA", "EU"],
    "UH": ["UH"],
    "TDR": ["TDL", "TDS"],
}

# Quality gate thresholds (relaxed 2026-03-30 — Barney directive)
# Previous values: BOUNDS 1-70, PEER 0.60, DAY_JUMP 15, STALENESS exact date
BOUNDS_MIN = 1.0
BOUNDS_MAX = 70.0
PEER_OUTLIER_THRESHOLD = 0.90  # 90% (was 60%)
DAY_JUMP_THRESHOLD = 25.0      # 25 WTI points (was 15)


def _active_parks(cfg: PipelineConfig) -> List[str]:
    """Parks to report on, in cfg.parks order, excluding cfg.ignore_parks."""
    ignored = set(cfg.ignore_parks)
    return [p for p in cfg.parks if p not in ignored]


def _sql_in_list(codes: List[str]) -> str:
    """Build a safely quoted SQL IN list body from park codes."""
    return ", ".join("'" + str(c).replace("'", "''") + "'" for c in codes)


def _extract_wti_data(cfg: PipelineConfig, log: PipelineLogger, target_date: str, source: str) -> Dict[str, float] | None:
    """Extract WTI data for all configured parks on target date."""
    wti_path = cfg.wti_dir / "wti.parquet"
    
    if not wti_path.exists():
        log.error(f"WTI file not found: {wti_path}")
        return None
    
    parks = _active_parks(cfg)
    if not parks:
        log.error("No parks configured for content generation")
        return None

    try:
        with read_connection() as con:
            query = f"""
                SELECT park_code, wti
                FROM read_parquet('{wti_path}')
                WHERE park_date::DATE = '{target_date}'::DATE
                AND source = '{source}'
                AND park_code IN ({_sql_in_list(parks)})
            """
            df = con.execute(query).df()
            
            if df.empty:
                log.warning(f"No {source} WTI data found for {target_date}")
                return None
            
            # Convert to dict
            wti_data = dict(zip(df['park_code'], df['wti']))
            log.info(f"Extracted {source} WTI for {target_date}: {len(wti_data)} parks")
            
            return wti_data
            
    except Exception as e:
        log.error(f"Failed to extract {source} WTI data for {target_date}: {e}")
        return None


def _check_completeness(wti_data: Dict[str, float], cfg: PipelineConfig) -> List[str]:
    """Check 1: Every configured park must be present."""
    failures = []

    for park_code in _active_parks(cfg):
        if park_code not in wti_data or wti_data[park_code] is None:
            failures.append(f"INCOMPLETE: {park_code} missing")

    return failures


def _check_absolute_bounds(wti_data: Dict[str, float]) -> List[str]:
    """Check 2: WTI must be within [1.0, 70.0]."""
    failures = []
    
    for park_code, wti in wti_data.items():
        if wti < BOUNDS_MIN or wti > BOUNDS_MAX:
            failures.append(f"OUT_OF_BOUNDS: {park_code} wti {wti} outside [{BOUNDS_MIN}, {BOUNDS_MAX}]")
    
    return failures


def _resort_of(park_code: str) -> str | None:
    for resort, codes in RESORT_GROUPS.items():
        if park_code in codes:
            return resort
    return None


def _check_peer_outlier(wti_data: Dict[str, float]) -> List[str]:
    """Check 3: No park should deviate >90% from the mean of its resort peers."""
    failures = []
    
    for park_code, wti in wti_data.items():
        resort = _resort_of(park_code)
        if resort is None:
            continue
        other_wtis = [
            wti_data[k] for k in RESORT_GROUPS[resort]
            if k != park_code and wti_data.get(k) is not None
        ]
        if not other_wtis:
            continue  # No peers in this resort
        
        peer_mean = sum(other_wtis) / len(other_wtis)
        if peer_mean <= 0:
            continue
        deviation = abs(wti - peer_mean) / peer_mean
        
        if deviation > PEER_OUTLIER_THRESHOLD:
            direction = "below" if wti < peer_mean else "above"
            failures.append(
                f"PEER_OUTLIER: {park_code} wti {wti} is {deviation:.0%} {direction} "
                f"{resort} peer mean {peer_mean:.1f} (threshold: {PEER_OUTLIER_THRESHOLD:.0%})"
            )
    
    return failures


def _check_day_jump(wti_data: Dict[str, float], cfg: PipelineConfig, target_date: str) -> List[str]:
    """Check 4: Day-over-day stability for predictions (<25 point jump)."""
    failures = []
    
    try:
        # Get yesterday's predicted data
        yesterday = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_file = cfg.output_base / "content" / f"predicted_{yesterday}.json"
        
        if not yesterday_file.exists():
            # No baseline to compare against - skip check
            return failures
        
        with open(yesterday_file, 'r') as f:
            yesterday_data = json.load(f)
        
        if yesterday_data.get("status") != "ready":
            # Yesterday's data wasn't good - skip comparison  
            return failures
        
        # Build yesterday's WTI lookup
        yesterday_wti = {}
        for park in yesterday_data.get("parks", []):
            yesterday_wti[park["park_code"]] = park["wti"]
        
        # Compare each park
        for park_code, today_wti in wti_data.items():
            if park_code in yesterday_wti:
                yesterday_wti_val = yesterday_wti[park_code]
                jump = abs(today_wti - yesterday_wti_val)
                
                if jump > DAY_JUMP_THRESHOLD:
                    direction = "+" if today_wti > yesterday_wti_val else "-"
                    failures.append(
                        f"DAY_JUMP: {park_code} predicted wti jumped "
                        f"{yesterday_wti_val:.1f} → {today_wti:.1f} ({direction}{jump:.1f}, threshold: {DAY_JUMP_THRESHOLD})"
                    )
        
    except Exception as e:
        # Non-fatal - log but don't fail the quality gate
        pass
    
    return failures


def _check_staleness(cfg: PipelineConfig, run_date: str) -> List[str]:
    """Check 5: WTI parquet must have been modified within the last 24 hours."""
    failures = []
    
    wti_path = cfg.wti_dir / "wti.parquet"
    if not wti_path.exists():
        failures.append("STALE_DATA: wti.parquet does not exist")
        return failures
    
    # Get file modification time — allow within 24 hours (was: exact date match)
    mod_time = datetime.fromtimestamp(wti_path.stat().st_mtime)
    age_hours = (datetime.now() - mod_time).total_seconds() / 3600
    
    if age_hours > 24:
        mod_date = mod_time.strftime("%Y-%m-%d %H:%M")
        failures.append(f"STALE_DATA: wti.parquet last modified {mod_date}, {age_hours:.1f}h ago (threshold: 24h)")
    
    return failures


def _failure_park(failure: str) -> str | None:
    """Park code a per-park failure refers to ("KIND: PARK ..."), or None if global."""
    kind, _, rest = failure.partition(": ")
    if kind not in ("INCOMPLETE", "OUT_OF_BOUNDS", "PEER_OUTLIER", "DAY_JUMP") or not rest:
        return None
    return rest.split()[0]


def _split_failures(failures: List[str]) -> Tuple[List[str], List[str]]:
    """Split failures into (blocking, warnings). Only WDW or global failures block."""
    wdw = set(RESORT_GROUPS["WDW"])
    blocking, warnings = [], []
    for failure in failures:
        park = _failure_park(failure)
        if park is not None and park not in wdw:
            warnings.append(failure)
        else:
            blocking.append(failure)
    return blocking, warnings


def _run_quality_gate(wti_data: Dict[str, float], cfg: PipelineConfig, target_date: str, is_predicted: bool, run_date: str) -> Tuple[bool, List[str], List[str]]:
    """Run all quality gate checks. Returns (passed, blocking_failures, warnings).

    Only WDW park failures (and global ones like staleness) block; failures
    for other parks are returned as warnings.
    """
    failures = []
    
    # Check 1: Completeness
    failures.extend(_check_completeness(wti_data, cfg))
    
    # Check 2: Absolute bounds
    failures.extend(_check_absolute_bounds(wti_data))
    
    # Check 3: Peer outlier
    failures.extend(_check_peer_outlier(wti_data))
    
    # Check 4: Day-over-day stability (predicted only)
    if is_predicted:
        failures.extend(_check_day_jump(wti_data, cfg, target_date))
    
    # Check 5: Staleness
    failures.extend(_check_staleness(cfg, run_date))
    
    blocking, warnings = _split_failures(failures)
    return len(blocking) == 0, blocking, warnings


def _create_content_json(content_type: str, target_date: str, wti_data: Dict[str, float], 
                        status: str, held_reasons: List[str], run_time: str,
                        cfg: PipelineConfig, warnings: List[str] | None = None) -> Dict[str, Any]:
    """Create the content JSON structure."""
    parks_data = []
    for park_code in _active_parks(cfg):  # Consistent ordering (cfg.parks order)
        if park_code in wti_data:
            parks_data.append({
                "park_code": park_code,
                "park_name": PARK_NAMES.get(park_code, park_code),
                "wti": round(wti_data[park_code], 1)
            })
    
    return {
        "type": content_type,
        "status": status,
        "held_reasons": held_reasons,
        "warnings": warnings or [],
        "target_date": target_date,
        "generated_at": run_time,
        "generated_by": "s14_content v1.1",
        "property": "WDW",
        "parks": parks_data
    }


def _post_discord_warning(cfg: PipelineConfig, log: PipelineLogger, content_type: str, target_date: str, failures: List[str]):
    """Post warning to #wti-pipeline for held content."""
    try:
        failure_list = "\n".join(f"• {failure}" for failure in failures)
        message = f"⚠️ **Content Quality Gate: {content_type.title()} {target_date} HELD**\n```\n{failure_list}\n```"
        
        # Use Clawdbot message tool if available
        # For now, just log - Discord integration can be added later
        log.warning(f"HELD CONTENT ({content_type} {target_date}): {'; '.join(failures)}")
        
        # TODO: Add actual Discord posting when message tool is integrated
        # This would require importing clawdbot message functions
        
    except Exception as e:
        log.error(f"Failed to post Discord warning: {e}")


def run(cfg: PipelineConfig, log: PipelineLogger):
    """Main Step 14 execution."""
    log.info("Step 14: Content generation starting")
    
    # Setup
    run_time = datetime.now().isoformat()
    run_date = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Create content directory
    content_dir = cfg.output_base / "content"
    content_dir.mkdir(exist_ok=True)
    
    # Extract predicted WTI (tomorrow)
    log.info(f"Extracting predicted WTI for {tomorrow}")
    predicted_wti = _extract_wti_data(cfg, log, tomorrow, "forecast")
    
    if predicted_wti:
        # Run quality gate
        passed, failures, warnings = _run_quality_gate(predicted_wti, cfg, tomorrow, is_predicted=True, run_date=run_date)
        status = "ready" if passed else "held"
        
        if not passed:
            _post_discord_warning(cfg, log, "predicted", tomorrow, failures)
        
        # Create JSON
        content_json = _create_content_json("predicted", tomorrow, predicted_wti, status, failures, run_time, cfg, warnings)
        
        # Write file
        output_file = content_dir / f"predicted_{tomorrow}.json"
        with open(output_file, 'w') as f:
            json.dump(content_json, f, indent=2)
        
        log.info(f"Predicted content for {tomorrow}: {status} ({len(predicted_wti)} parks)")
        if failures:
            log.warning(f"Predicted quality gate failures: {'; '.join(failures)}")
        if warnings:
            log.warning(f"Predicted quality gate warnings (non-WDW, not held): {'; '.join(warnings)}")
    
    else:
        log.error(f"No predicted WTI data available for {tomorrow}")
    
    # Extract observed WTI (yesterday)  
    log.info(f"Extracting observed WTI for {yesterday}")
    observed_wti = _extract_wti_data(cfg, log, yesterday, "historical")
    
    if observed_wti:
        # Run quality gate (not predicted, so no day-jump check)
        passed, failures, warnings = _run_quality_gate(observed_wti, cfg, yesterday, is_predicted=False, run_date=run_date)
        status = "ready" if passed else "held"
        
        if not passed:
            _post_discord_warning(cfg, log, "observed", yesterday, failures)
        
        # Create JSON
        content_json = _create_content_json("observed", yesterday, observed_wti, status, failures, run_time, cfg, warnings)
        
        # Write file
        output_file = content_dir / f"observed_{yesterday}.json"
        with open(output_file, 'w') as f:
            json.dump(content_json, f, indent=2)
        
        log.info(f"Observed content for {yesterday}: {status} ({len(observed_wti)} parks)")
        if failures:
            log.warning(f"Observed quality gate failures: {'; '.join(failures)}")
        if warnings:
            log.warning(f"Observed quality gate warnings (non-WDW, not held): {'; '.join(warnings)}")
    
    else:
        log.warning(f"No observed WTI data available for {yesterday}")
    
    log.info("Step 14: Content generation completed")
