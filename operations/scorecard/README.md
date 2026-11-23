# TPCR accuracy scorecard (TPCR-ACCURACY-SCORECARD-001)

Read-only. Inputs: `$TPCR_ACCURACY_DIR/{wti_accuracy,slot_accuracy}.parquet`. Outputs only under `$TPCR_SCORECARD_DIR`.

- `scorecard.py --as-of YYYY-MM-DD [--out DIR] [--dry-run]`: exit 0 ok, 3 drift flagged, 75 inputs stale/missing, 1 error.
- `weekly.py --start YYYY-MM-DD --end YYYY-MM-DD`: `weekly_<start>_<end>.json` for the scorecard tweet template.
- `backtest.py [--start 2026-04-01] [--end 2026-10-12]`: first flag date per key.
- `drift_alert.py --status PATH [--dry-run] [--out DIR]`: writes `alerts/alert_<as_of>.eml.txt` only. There is no send path; every send needs Fred's explicit OK and is done by hand.
- `run_after_accuracy.sh <accuracy eval command...>`: runs the scorecard only if the eval command exits 0.

Transparency note: after `wti_predicted_tweet.py` posts the forecast (v2 card video and edge-copy text since TPCR-TWEET-V2, see `operations/tweet_v2/README.md`), it saves the note as a draft under `note_drafts[<reference_date>]` in `tweet_state.json` with `in_reply_to` set to the forecast tweet_id. It never posts the note. Lines are joined and whole lines dropped from the end until the text is 280 characters or fewer. `tweet_note.py approve YYYY-MM-DD` takes an exclusive lock, checks the draft's sha256 and in_reply_to, posts that one draft as a reply and marks it posted; a second run posts nothing. A draft left in `posting` (crash) or `unknown` (post returned nothing) is never retried: check X by hand. If no note line fits in 280 characters, no draft is saved and `note_too_long` is logged. Running approve needs Fred's OK.

Env defaults: TPCR_DRIFT_PARK_BIAS_MIN=5, TPCR_DRIFT_ENTITY_BIAS_MIN=15, TPCR_SCORECARD_MIN_N=20, TPCR_PARK_HIT_MAX_MIN=5, TPCR_PARK_MISS_MIN=10, TPCR_PARK_MAE_GOAL_MIN=8, TPCR_DRIFT_ALERT_TO=fred@hazeydata.ai, TPCR_TWEET_NOTE_VARIANT=standard.

Ride grading is off: `grade_ride` raises NotCalibrated until TPCR-RIDE-GRADE-CALIBRATE-001.

## Step for chief (not done here, needs an approval card)
Chain the nightly accuracy evaluation on the Mini through `run_after_accuracy.sh`, with TPCR_ACCURACY_DIR and TPCR_SCORECARD_DIR set, then `drift_alert.py --dry-run --status $TPCR_SCORECARD_DIR/status.json`.
