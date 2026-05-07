#!/usr/bin/env bash
# ============================================================
# SrotaAI — One-command startup
# Usage:  ./run.sh          (starts or confirms server running)
#         ./run.sh stop     (stops the server)
#         ./run.sh restart  (restart)
# ============================================================

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PORT=8001
HOST="0.0.0.0"
PIDFILE="$DIR/.server.pid"
LOGFILE="$DIR/server.log"

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[[ -z "$IP" ]] && IP=$(hostname)
URL="http://$IP:$PORT"

_is_running() {
    if [[ -f "$PIDFILE" ]]; then
        local pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    # Also check if port is in use by someone else
    if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
        return 0
    fi
    return 1
}

_stop() {
    if [[ -f "$PIDFILE" ]]; then
        local pid=$(cat "$PIDFILE")
        kill "$pid" 2>/dev/null && echo "Stopped server (PID $pid)"
        rm -f "$PIDFILE"
        sleep 1
    fi
    fuser -k "$PORT/tcp" 2>/dev/null || true
    sleep 1
}

_start() {
    # Activate venv
    if [[ ! -d .venv ]]; then
        echo "⏳ Creating virtual environment..."
        python3 -m venv .venv
        .venv/bin/pip install --quiet -r requirements.txt
    fi
    source .venv/bin/activate
    python -c "import fastapi" 2>/dev/null || {
        echo "⏳ Installing dependencies..."
        pip install --quiet -r requirements.txt
    }

    # Start as background daemon
    nohup python -m uvicorn srotaai.web.app:app \
        --host "$HOST" --port "$PORT" \
        --timeout-keep-alive 30 \
        --log-level info > "$LOGFILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    disown "$pid"

    # Wait for it to be ready (up to 5 seconds)
    for i in 1 2 3 4 5; do
        sleep 1
        if curl -s -o /dev/null -w "" "http://localhost:$PORT/" 2>/dev/null; then
            echo ""
            echo "╔══════════════════════════════════════════════════════╗"
            echo "║          SrotaAI — PV Monitoring Platform           ║"
            echo "╠══════════════════════════════════════════════════════╣"
            echo "║                                                      ║"
            echo "║  ✓ Server running (PID $pid)                     ║"
            echo "║                                                      ║"
            echo "║  Open in browser:                                    ║"
            echo "║    → $URL                                            ║"
            echo "║                                                      ║"
            echo "║  To stop:  ./run.sh stop                             ║"
            echo "║  Logs:     tail -f server.log                        ║"
            echo "╚══════════════════════════════════════════════════════╝"
            echo ""
            return 0
        fi
    done
    echo "ERROR: Server failed to start. Check server.log"
    cat "$LOGFILE" | tail -20
    return 1
}

# ---- Main ----
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
            echo ""
            echo "✓ Server already running at $URL"
            echo "  To restart: ./run.sh restart"
            echo "  To stop:    ./run.sh stop"
            echo ""
        else
            _start
        fi
        ;;
    *)
        echo "Usage: ./run.sh [start|stop|restart]"
        ;;
esac
