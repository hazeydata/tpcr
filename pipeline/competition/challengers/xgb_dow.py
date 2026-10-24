"""Challenger: xgb-dow

Hypothesis: Day-of-week as a 6th feature captures weekend/weekday crowd patterns
that the baseline misses. Weekends at WDW are consistently different from weekdays.

Changes from baseline:
- Add day_of_week (0=Mon, 6=Sun) as feature #6
- Everything else identical: same hyperparams, same geo-decay, same weighting
"""

NAME = "xgb-dow"
DESCRIPTION = "Add day-of-week as 6th feature"
DATE_REGISTERED = "2026-04-06"
STATUS = "retired"
NOTES = "Retired: day-of-week is already in production via #470; never forecast or scored."

HYPERPARAMS = {}  # Use baseline hyperparams

FEATURES = ["mins_since_6am", "mins_since_open", "date_group_id_encoded", "season_encoded", "season_year_encoded", "day_of_week"]

GEO_DECAY_HALFLIFE = 730
