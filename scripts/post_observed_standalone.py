#!/usr/bin/env python3
"""
Post observed WTI tweet as standalone (when no prediction tweet to reply to)
Based on post_observed_tweet.py but simplified for standalone posting.
"""

import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import tweepy

# Setup paths
CONTENT_DIR = Path("/home/wilma/hazeydata/pipeline/content")
VIDEO_PATH = Path("/home/wilma/clawd-anthropic/remotion-experiments/remotion-tpcr/out")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


WDW_PARK_CODES = ("MK", "EP", "HS", "AK")


def _wdw_parks(content: dict) -> list:
    """Step 14 emits all parks; these tweets cover Walt Disney World only."""
    return [p for p in content.get("parks", []) if p.get("park_code") in WDW_PARK_CODES]


def load_content_json(target_date: str) -> dict | None:
    """Load and validate observed content JSON."""
    content_file = CONTENT_DIR / f"observed_{target_date}.json"
    
    if not content_file.exists():
        logger.error(f"Content file not found: {content_file}")
        return None
    
    try:
        with open(content_file, 'r') as f:
            content = json.load(f)
        
        status = content.get("status", "unknown")
        if status not in ("ready", "released"):
            logger.warning(f"Content status is '{status}' - not posting.")
            return None
        
        logger.info(f"Loaded observed content for {target_date} (status: {status})")
        return content
        
    except Exception as e:
        logger.error(f"Failed to load content JSON: {e}")
        return None


def post_standalone_tweet(video_path: Path, content: dict) -> str | None:
    """Post observed tweet as standalone tweet."""
    try:
        # Load Twitter API credentials from environment
        api_key = os.getenv("TWITTER_CONSUMER_KEY")
        api_secret = os.getenv("TWITTER_CONSUMER_SECRET") 
        access_token = os.getenv("TWITTER_ACCESS_TOKEN")
        access_token_secret = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
        
        if not all([api_key, api_secret, access_token, access_token_secret]):
            logger.error("Twitter API credentials not found in environment")
            return None
        
        # Initialize Twitter API v2
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
            wait_on_rate_limit=True
        )
        
        # Also need v1.1 API for media upload
        auth = tweepy.OAuth1UserHandler(
            api_key, api_secret, access_token, access_token_secret
        )
        api = tweepy.API(auth)
        
        # Upload video
        logger.info(f"Uploading video: {video_path}")
        media = api.media_upload(str(video_path), media_category="tweet_video")
        
        # Create tweet text for observed data (standalone format)
        target_date = content["target_date"]
        date_obj = date.fromisoformat(target_date)
        formatted_date = date_obj.strftime("%A, %B %-d")
        
        tweet_text = f"📊 Observed WTI — Walt Disney World\n{formatted_date} (Actual Wait Times)\n\n#DisneyWorld #WaitTimes #TPCR #Observed"
        
        # Post standalone tweet
        logger.info("Posting standalone observed tweet...")
        response = client.create_tweet(
            text=tweet_text, 
            media_ids=[media.media_id]
        )
        
        tweet_id = response.data["id"]
        logger.info(f"Standalone tweet posted successfully: {tweet_id}")
        logger.info(f"Tweet URL: https://twitter.com/ThemeParkCR/status/{tweet_id}")
        return tweet_id
        
    except Exception as e:
        logger.error(f"Failed to post standalone tweet: {e}")
        return None


def main():
    target_date = "2026-03-25"  # Yesterday
    
    logger.info(f"Starting standalone observed tweet for {target_date}")
    
    # Load observed content JSON
    content = load_content_json(target_date)
    if not content:
        sys.exit(1)
    
    # Find the rendered video
    video_path = VIDEO_PATH / f"observed-wti-{target_date}.mp4"
    if not video_path.exists():
        logger.error(f"Video file not found: {video_path}")
        sys.exit(1)
    
    # Post standalone tweet
    tweet_id = post_standalone_tweet(video_path, content)
    if tweet_id:
        logger.info(f"✅ Successfully posted observed WTI tweet: {tweet_id}")
        
        # Print summary of posted data
        logger.info("Posted WTI data:")
        for park in _wdw_parks(content):
            logger.info(f"  {park['park_name']}: {park['wti']}")
            
    else:
        logger.error("❌ Failed to post observed WTI tweet")
        sys.exit(1)


if __name__ == "__main__":
    main()