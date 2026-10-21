#!/usr/bin/env python3
"""
Post predicted WTI tweet (~4:00 PM ET)

Reads Step 14 content JSON, renders Remotion composition, posts to Twitter.
Part of V4 Amendment 001 content pipeline.

Usage:
    python scripts/post_predicted_tweet.py [--dry-run]
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import tweepy

# Setup paths
REPO_ROOT = Path(__file__).parent.parent
CONTENT_DIR = Path("/home/wilma/hazeydata/pipeline/content")
REMOTION_DIR = Path("/home/wilma/clawd-anthropic/remotion-experiments/remotion-tpcr")
TWEET_STATE_FILE = CONTENT_DIR / "tweet_state.json"

# Discord channel for confirmations
DISCORD_CHANNEL = "1479351620276367360"  # #wti-pipeline

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
    """Load and validate Step 14 predicted content JSON."""
    content_file = CONTENT_DIR / f"predicted_{target_date}.json"
    
    if not content_file.exists():
        logger.error(f"Content file not found: {content_file}")
        return None
    
    try:
        with open(content_file, 'r') as f:
            content = json.load(f)
        
        status = content.get("status", "unknown")
        if status not in ("ready", "released"):
            logger.warning(f"Content status is '{status}' - not posting. Reasons: {content.get('held_reasons', [])}")
            return None
        
        logger.info(f"Loaded predicted content for {target_date} (status: {status})")
        return content
        
    except Exception as e:
        logger.error(f"Failed to load content JSON: {e}")
        return None


def update_remotion_data(content: dict) -> bool:
    """Update Remotion public/daily-wti-data.json with content data."""
    try:
        # Build Remotion-compatible data structure
        parks_data = []
        for park in _wdw_parks(content):
            # Get emoji and label for WTI score
            wti = park["wti"]
            
            # Simple WTI labeling (can be enhanced later)
            if wti <= 12:
                emoji, label = "❄️", "Very Low"
            elif wti <= 16:
                emoji, label = "💎", "Low"
            elif wti <= 20:
                emoji, label = "⚪", "Below Average"
            elif wti <= 25:
                emoji, label = "🌸", "Average"
            elif wti <= 30:
                emoji, label = "🔥", "Above Average"
            elif wti <= 35:
                emoji, label = "🔴", "High"
            else:
                emoji, label = "💀", "Extreme"
            
            # Park emojis
            park_emojis = {
                "MK": "🏰",
                "EP": "🌐", 
                "HS": "🎬",
                "AK": "🦁"
            }
            
            parks_data.append({
                "parkCode": park["park_code"],
                "parkName": park["park_name"],
                "parkEmoji": park_emojis.get(park["park_code"], "🎢"),
                "wtiScore": wti,
                "wtiEmoji": emoji,
                "wtiLabel": label,
                "headlinerRide": "Check the app!",  # Placeholder
                "headlinerWait": 0,
                "bestRide": "Check the app!",  # Legacy compat
                "bestRideWait": 0
            })
        
        # Format date nicely
        target_date = content["target_date"]
        date_obj = date.fromisoformat(target_date)
        formatted_date = date_obj.strftime("%B %-d, %Y")
        
        remotion_data = {
            "date": formatted_date,
            "dateISO": target_date,
            "mode": "predicted",
            "generatedAt": content["generated_at"],
            "contentSource": content["generated_by"],
            "qualityGateStatus": content["status"],
            "parks": parks_data
        }
        
        # Write to Remotion public directory
        output_file = REMOTION_DIR / "public" / "daily-wti-data.json"
        output_file.parent.mkdir(exist_ok=True)
        
        with open(output_file, 'w') as f:
            json.dump(remotion_data, f, indent=2)
        
        logger.info(f"Updated Remotion data: {output_file}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to update Remotion data: {e}")
        return False


def render_remotion_composition(target_date: str) -> Path | None:
    """Render Remotion composition for predicted tweet."""
    try:
        output_file = REMOTION_DIR / "out" / f"predicted-wti-{target_date}.mp4"
        output_file.parent.mkdir(exist_ok=True)
        
        # Change to Remotion directory
        original_cwd = os.getcwd()
        os.chdir(REMOTION_DIR)
        
        try:
            # Render DailyWTIAll composition
            cmd = [
                "npx", "remotion", "render",
                "src/index.ts", 
                "DailyWTIAll",
                str(output_file)
            ]
            
            logger.info(f"Rendering Remotion composition: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode != 0:
                logger.error(f"Remotion render failed: {result.stderr}")
                return None
            
            if not output_file.exists():
                logger.error(f"Rendered file not found: {output_file}")
                return None
            
            logger.info(f"Remotion render completed: {output_file}")
            return output_file
            
        finally:
            os.chdir(original_cwd)
            
    except subprocess.TimeoutExpired:
        logger.error("Remotion render timed out after 5 minutes")
        return None
    except Exception as e:
        logger.error(f"Remotion render failed: {e}")
        return None


def post_tweet(video_path: Path, content: dict) -> str | None:
    """Post tweet with video to Twitter."""
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
        
        # Create tweet text
        target_date = content["target_date"]
        date_obj = date.fromisoformat(target_date)
        formatted_date = date_obj.strftime("%A, %B %-d")
        
        tweet_text = f"🔮 Tomorrow's WTI Forecast — Walt Disney World\n{formatted_date}\n\n#DisneyWorld #WaitTimes #TPCR"
        
        # Post tweet
        logger.info("Posting tweet...")
        response = client.create_tweet(text=tweet_text, media_ids=[media.media_id])
        
        tweet_id = response.data["id"]
        logger.info(f"Tweet posted successfully: {tweet_id}")
        return tweet_id
        
    except Exception as e:
        logger.error(f"Failed to post tweet: {e}")
        return None


def save_tweet_state(tweet_id: str, target_date: str) -> bool:
    """Save tweet ID to tweet state file."""
    try:
        # Load existing state or create new
        if TWEET_STATE_FILE.exists():
            with open(TWEET_STATE_FILE, 'r') as f:
                state = json.load(f)
        else:
            state = {}
        
        # Update with new predicted tweet
        state["last_predicted"] = {
            "tweet_id": tweet_id,
            "target_date": target_date,
            "posted_at": date.today().isoformat() + "T16:00:00Z"  # Approximate
        }
        
        # Save state
        TWEET_STATE_FILE.parent.mkdir(exist_ok=True)
        with open(TWEET_STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
        
        logger.info(f"Saved tweet state: {TWEET_STATE_FILE}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to save tweet state: {e}")
        return False


def post_discord_confirmation(tweet_id: str, target_date: str, success: bool) -> None:
    """Post confirmation to Discord #wti-pipeline."""
    try:
        # Use Clawdbot message tool if available
        if success:
            message = f"✅ **Predicted WTI Tweet Posted**\nDate: {target_date}\nTweet ID: {tweet_id}\nhttps://twitter.com/ThemeParkCR/status/{tweet_id}"
        else:
            message = f"❌ **Predicted WTI Tweet Failed**\nDate: {target_date}\nCheck logs for details."
        
        # TODO: Implement Discord posting via Clawdbot message tool
        logger.info(f"Discord notification: {message}")
        
    except Exception as e:
        logger.error(f"Failed to post Discord confirmation: {e}")


def main():
    parser = argparse.ArgumentParser(description="Post predicted WTI tweet")
    parser.add_argument("--dry-run", action="store_true", help="Render but don't post")
    parser.add_argument("--date", type=str, help="Override target date (YYYY-MM-DD)")
    args = parser.parse_args()
    
    # Determine target date (tomorrow by default)
    if args.date:
        target_date = args.date
    else:
        tomorrow = date.today() + timedelta(days=1)
        target_date = tomorrow.strftime("%Y-%m-%d")
    
    logger.info(f"Starting predicted tweet workflow for {target_date}")
    
    # Step 1: Load and validate content JSON
    content = load_content_json(target_date)
    if not content:
        sys.exit(1)
    
    # Step 2: Update Remotion data
    if not update_remotion_data(content):
        logger.error("Failed to update Remotion data")
        sys.exit(1)
    
    # Step 3: Render Remotion composition
    video_path = render_remotion_composition(target_date)
    if not video_path:
        logger.error("Failed to render Remotion composition")
        sys.exit(1)
    
    if args.dry_run:
        logger.info(f"DRY RUN: Would post {video_path} to Twitter")
        logger.info("Dry run completed successfully")
        return
    
    # Step 4: Post tweet
    tweet_id = post_tweet(video_path, content)
    success = tweet_id is not None
    
    # Step 5: Save tweet state
    if success:
        save_tweet_state(tweet_id, target_date)
    
    # Step 6: Post Discord confirmation
    post_discord_confirmation(tweet_id, target_date, success)
    
    if success:
        logger.info(f"Predicted tweet workflow completed successfully: {tweet_id}")
    else:
        logger.error("Predicted tweet workflow failed")
        sys.exit(1)


if __name__ == "__main__":
    main()