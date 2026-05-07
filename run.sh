#!/usr/bin/env bash
# ============================================================
# SrotaAI — One-command startup (Linux / macOS / WSL / Git Bash)
# Usage:  ./run.sh          start (or confirm running)
#         ./run.sh stop     stop the server
#         ./run.sh restart  restart
#
# Windows users without WSL/Git Bash: see README "Windows setup".
# ============================================================
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PORT="${PORT:-8001}"
HOST="0.0.0.0"
PIDFILE="$DIR/.server.pid"
LOGFILE="$DIR/server.log"
URL="http://localhost:$PORT"

_have() { command -v "$1" >/dev/null 2>&1; }

_python() {
    if _have python3; then echo python3
    elif _have python; then echo python
    else
        echo ""
    fi
}

_check_python() {
    PY=$(_python)
    if [[ -z "$PY" ]]; then
        echo "ERROR: Python not found. Install Python 3.11+ from https://python.org"
        exit 1
    fi
    local pyver
    pyver=$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    case "$pyver" in
        3.11|3.12|3.13) ;;
        *) echo "WARNING: Python $pyver detected. 3.11+ recommended." ;;
    esac
}

_is_running() {
    if [[ -f "$PIDFILE" ]]; then
        local pid
        pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    PY=$(_python)
    [[ -z "$PY" ]] && return 1
    $PY - <<EOF 2>/dev/null
import socket, sys
s = socket.socket()
try:
    s.bind(('127.0.0.1', $PORT))
    sys.exit(1)
except OSError:
    sys.exit(0)
finally:
    s.close()
EOF
}

_stop() {
    if [[ -f "$PIDFILE" ]]; then
        local pid
        pid=$(cat "$PIDFILE")
        kill "$pid" 2>/dev/null && echo "Stopped server (PID $pid)"
        rm -f "$PIDFILE"
        sleep 1
    fi
    _have fuser && fuser -k "$PORT/tcp" 2>/dev/null || true
    _have lsof  && lsof -ti tcp:"$PORT" 2>/dev/null | xargs -r kill 2>/dev/null || true
    sleep 1
}

_start() {
    _check_python

    if [[ ! -d .venv ]]; then
        echo "Creating virtual environment..."
        $PY -m venv .venv
    fi

    # shellcheck disable=SC1091
    if [[ -f .venv/bin/activate ]]; then
        source .venv/bin/activate
    else
        source .venv/Scripts/activate   # Git Bash on Windows
    fi

    if ! python -c "import fastapi" 2>/dev/null; then
        echo "Installing dependencies (one-time, ~30s)..."
        pip install --quiet --upgrade pip
        pip install --quiet -r requirements.txt
    fi

    if [[ ! -f srotaai.db ]]; then
        echo "Seeding signal database (one-time)..."
        python -m srotaai.signals pv-india-otc --db srotaai.db --min-n 2 >/dev/null 2>&1 || true
    fi

    nohup python -m uvicorn srotaai.web.app:app \
        --host "$HOST" --port "$PORT" \
        --timeout-keep-alive 30 \
        --log-level info > "$LOGFILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    disown "$pid" 2>/dev/null || true

    for _ in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        if curl -s -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then
            echo ""
            echo "============================================================"
            echo "  SrotaAI is running"
            echo "============================================================"
            echo ""
            echo "    Open in your browser:  $URL"
            echo ""
            echo "    stop:    ./run.sh stop"
            echo "    restart: ./run.sh restart"
            echo "    logs:    tail -f server.log"
            echo "============================================================"
            return 0
        fi
    done
    echo "ERROR: Server failed to start. Last 20 lines of log:"
    tail -20 "$LOGFILE"
    return 1
}

case "${1:-start}" in
    stop)
        _stop
        echo "Server stopped."
        ;;
    restart)
        _stop
        _start
        ;;
    start|"")
        if _is_running; then
            echo "Server already running at $URL"
            echo "  restart: ./run.sh restart"
            echo "  stop:    ./run.sh stop"
        else
            _start
        fi
        ;;
    *)
        echo "Usage: ./run.sh [start|stop|restart]"
        exit 1
        ;;
esac
