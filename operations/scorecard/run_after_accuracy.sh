#!/bin/bash
set -u
"$@" || exit $?
cd "$(dirname "$0")" && exec python3 scorecard.py --as-of "${TPCR_AS_OF:-$(date +%F)}"
