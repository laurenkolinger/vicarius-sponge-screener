#!/usr/bin/env bash
# launch_sponge_screener.sh: start Sponge Screener for the VICARIUS desktop on the port in argv[1].
#
# VICARIUS (the module card on port 5090) runs this from the scripts folder with
# the port as the only argument, then opens http://127.0.0.1:<port>/ with the run
# folder and the signed-in initials in the address. The app uses only the Python
# standard library, so the system Python runs it; screener.py --vicarius reads
# config/vicarius.ini for the work folder that holds the video cache.
# SPONGE_SCREENER_DRY_RUN=1 prints the command instead of running it.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="/usr/bin/python3"
PORT="${1:-}"
MAX_PORT=65535

if [[ ! "${PORT}" =~ ^[0-9]{1,5}$ ]] || (( 10#${PORT} < 1 || 10#${PORT} > MAX_PORT )); then
  echo "launch_sponge_screener.sh: the port is the first argument, a number from 1 to ${MAX_PORT}, for example 5098; got '${PORT}'" >&2
  exit 2
fi
PORT="$((10#${PORT}))"

if [ "${SPONGE_SCREENER_DRY_RUN:-0}" = "1" ]; then
  echo "${PYTHON} ${REPO}/screener.py --vicarius --port ${PORT}"
  exit 0
fi

cd "${REPO}"
exec "${PYTHON}" "${REPO}/screener.py" --vicarius --port "${PORT}"
