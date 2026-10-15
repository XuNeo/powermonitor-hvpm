#!/bin/sh
# Launch this checkout through uv when available, falling back to an existing .venv.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# Optional user-local X11 library extracted during Linux validation.
for runtime in "$ROOT"/.venv/xcb-runtime.*/usr/lib/x86_64-linux-gnu; do
    if [ -f "$runtime/libxcb-cursor.so.0" ]; then
        LD_LIBRARY_PATH="$runtime${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        export LD_LIBRARY_PATH
        break
    fi
done

UV=$(command -v uv 2>/dev/null || true)
if [ -z "$UV" ] && [ -x /opt/homebrew/bin/uv ]; then
    UV=/opt/homebrew/bin/uv
fi
if [ -n "$UV" ]; then
    exec "$UV" --directory "$ROOT" run --extra test python -m powermonitor "$@"
fi

if [ -x "$ROOT/.venv/bin/python" ]; then
    exec "$ROOT/.venv/bin/python" -m powermonitor "$@"
fi

printf '%s\n' 'Install uv or create .venv and install the project first; see README.md.' >&2
exit 1
