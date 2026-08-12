#!/bin/bash
# Double-clickable (or `./run_gui.sh`) launcher for the EPUB Translator GUI on Linux/macOS.
set -o pipefail
cd "$(dirname "$(readlink -f "$0")")" || exit 1

python3 gui.py 2>&1 | tee gui_launch.log
status=$?

if [ $status -ne 0 ]; then
    echo
    echo "gui.py exited with an error (code $status). Details were saved to gui_launch.log."
    # Only pause for a keypress if there's an actual terminal attached to read from
    # (there isn't one when launched from a Terminal=false .desktop entry).
    if [ -t 0 ]; then
        read -p "Press Enter to close this window..." _
    fi
fi
exit $status
