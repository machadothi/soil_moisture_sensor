#!/usr/bin/env bash
# Compile the soil sensor firmware and copy it to the board, then restart it.
#
#   tools/deploy.sh                 # code only; the board keeps its config.json
#   tools/deploy.sh --config        # also upload firmware/config.json
#   PORT=/dev/ttyUSB1 tools/deploy.sh
#
# Needs mpremote and mpy-cross matching the board's MicroPython (1.25.x):
#   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# main.py stays as source (tiny); the modules are compiled to .mpy.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-/dev/ttyUSB0}"
BIN="${VENV:-$ROOT/.venv}/bin"
MPREMOTE="$BIN/mpremote"
MPY_CROSS="$BIN/mpy-cross"
MODULES=(app ble bthome convert power protocol sensors)
# Sources would be imported instead of the .mpy files.
STALE=(app.py ble.py bthome.py convert.py power.py protocol.py sensors.py)

[ -x "$MPREMOTE" ] && [ -x "$MPY_CROSS" ] || { echo "Missing mpremote/mpy-cross in $BIN, see the top of $0"; exit 1; }

BUILD="$ROOT/build"
rm -rf "${BUILD:?}" && mkdir -p "$BUILD"
for module in "${MODULES[@]}"; do
    "$MPY_CROSS" -O3 -o "$BUILD/$module.mpy" "$ROOT/firmware/$module.py"
done
echo "Compiled: ${MODULES[*]}"

remote() { "$MPREMOTE" connect "$PORT" "$@"; }

# Stop the running program first. A deep-sleeping board can't hear Ctrl+C:
# reset it through the USB-serial chip (RTS drives EN), then it stays awake.
"$BIN/python" -I -c "
import serial, sys, time
s = serial.Serial(); s.port = sys.argv[1]; s.dtr = s.rts = False; s.open()
s.rts = True; time.sleep(0.1); s.rts = False" "$PORT"
sleep 2
for attempt in $(seq 1 30); do
    remote exec "print('ready')" >/dev/null 2>&1 && break
    [ "$attempt" = 30 ] && { echo "The board doesn't answer on $PORT (press its EN button and try again)"; exit 1; }
    sleep 1
done

for file in "${STALE[@]}"; do
    remote rm ":$file" >/dev/null 2>&1 || true
done
args=()
for module in "${MODULES[@]}"; do
    args+=(cp "$BUILD/$module.mpy" ":$module.mpy" +)
done
args+=(cp "$ROOT/firmware/main.py" :main.py)
if [ "${1:-}" = "--config" ]; then
    [ -f "$ROOT/firmware/config.json" ] || { echo "No firmware/config.json: copy config.example.json and edit it"; exit 1; }
    args+=(+ cp "$ROOT/firmware/config.json" :config.json)
fi
remote "${args[@]}"
remote reset
echo "Deployed. Log: $MPREMOTE connect $PORT repl   (Ctrl+C stops the program, Ctrl+] quits)"
