#!/bin/bash
# Launch the touchscreen app on the Pi's display.
#   ./run.sh            -> real Arduino on /dev/ttyACM0
#   ./run.sh sim        -> built-in simulator, no hardware
# Extra options come from BSS_* env vars (see config.py / README.md), e.g.
#   BSS_CAMERA=1 ./run.sh        BSS_UPLOAD=1 BSS_UPLOAD_URL=... ./run.sh
cd "$(dirname "$0")"
export DISPLAY="${DISPLAY:-:0}"

if [ "$1" = "sim" ]; then
    export BSS_SERIAL_PORT=sim
fi

python3 app.py
