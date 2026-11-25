#!/bin/bash
# Run step2param on the example output flange (HD60-32, CC BY 4.0).
# Usage: bash examples/run_example.sh [freecadcmd]
#   freecadcmd defaults to "freecadcmd" on PATH.
set -e
cd "$(dirname "$0")/.."
FREECADCMD="${1:-freecadcmd}"
python -m step2param.step2param --step examples/output_flange.step \
    --out out/output_flange --ct --iso \
    --freecadcmd "$FREECADCMD"
echo "---"
python -c "import json; d=json.load(open('out/output_flange/features.json')); print('volume:', d['verify'][0]['detail']); print('refine_report:', d.get('refine_report',{}).get('status'))"
