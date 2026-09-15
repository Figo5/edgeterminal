#!/usr/bin/env bash
# EdgeTerminal process control: start | stop | restart | status
#                              install-service | uninstall-service
#
# Credentials are never passed as arguments or echoed: the app reads .env itself
# (edgeterminal/config.py:_load_dotenv). Process identification is by PID file and
# a verified command line -- never a broad pkill, which would hit CivicFolio (8787)
# or any other uvicorn on this machine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${EDGETERMINAL_PORT:-8799}"
PYTHON="$ROOT/.venv/bin/python"
PIDFILE="$ROOT/data/edgeterminal.pid"
LOGFILE="$ROOT/data/edgeterminal.log"
LABEL="com.edgeterminal.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
MARKER="edgeterminal.api:app"

die() { echo "error: $*" >&2; exit 1; }

# True only if $1 is alive AND is actually this app -- a recycled PID belonging to
# some other process must never be reported as running, let alone killed.
is_ours() {
  local pid="${1:-}"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
  ps -o command= -p "$pid" 2>/dev/null | grep -q "$MARKER"
}

running_pid() {
  [ -f "$PIDFILE" ] || return 1
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  is_ours "$pid" || { rm -f "$PIDFILE"; return 1; }
  echo "$pid"
}

# Any EdgeTerminal already holding the port, even one this script did not start
# (a stray foreground run, or a process left behind by a tool session).
port_holder() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | while read -r p; do
    is_ours "$p" && { echo "$p"; return 0; }
  done
}

cmd_start() {
  [ -x "$PYTHON" ] || die "virtualenv missing: $PYTHON (python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)"
  if pid="$(running_pid)"; then echo "already running (pid $pid) on port $PORT"; return 0; fi
  # Without this the new process loses the bind, exits, and the health check below
  # silently passes against the *old* one -- which is how a process left over from
  # an earlier session kept serving stale code.
  if foreign="$(port_holder)" && [ -n "$foreign" ]; then
    echo "$PORT is already served by EdgeTerminal pid $foreign, which this script did not start."
    echo "adopting it; run '$0 restart' to replace it with the current code."
    echo "$foreign" > "$PIDFILE"
    return 0
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $PORT is in use by another program (not EdgeTerminal); refusing to start"
  fi
  mkdir -p "$ROOT/data"
  cd "$ROOT"
  PORT="$PORT" nohup "$PYTHON" -m uvicorn edgeterminal.api:app \
      --host 127.0.0.1 --port "$PORT" >>"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      echo "started (pid $(cat "$PIDFILE")) — http://127.0.0.1:$PORT"; return 0
    fi
    sleep 0.25
  done
  echo "started (pid $(cat "$PIDFILE")) but /api/health did not answer in 10s; see $LOGFILE" >&2
  return 1
}

cmd_stop() {
  if ! pid="$(running_pid)"; then echo "not running"; return 0; fi
  kill "$pid"
  for _ in $(seq 1 40); do is_ours "$pid" || break; sleep 0.25; done
  if is_ours "$pid"; then kill -9 "$pid" 2>/dev/null || true; sleep 0.5; fi
  rm -f "$PIDFILE"
  echo "stopped (pid $pid)"
}

cmd_status() {
  if pid="$(running_pid)"; then
    echo "running: pid $pid, port $PORT"
    curl -fsS "http://127.0.0.1:$PORT/api/health" 2>/dev/null || echo "health endpoint not answering"
    echo
  else
    echo "stopped (port $PORT)"
    return 1
  fi
}

cmd_install_service() {
  # Opt-in only. Never installed as a side effect of start.
  [ -f "$PLIST" ] && die "already installed: $PLIST (uninstall-service first)"
  mkdir -p "$(dirname "$PLIST")" "$ROOT/data"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string><string>-m</string><string>uvicorn</string>
    <string>edgeterminal.api:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict><key>PORT</key><string>$PORT</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGFILE</string>
  <key>StandardErrorPath</key><string>$LOGFILE</string>
</dict></plist>
PLISTEOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "installed and loaded $LABEL"
  echo "this survives terminal close and restarts at login; remove with: $0 uninstall-service"
}

cmd_uninstall_service() {
  [ -f "$PLIST" ] || { echo "no service installed"; return 0; }
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled $LABEL (removed $PLIST)"
}

case "${1:-}" in
  start)             cmd_start ;;
  stop)              cmd_stop ;;
  restart)           cmd_stop
                     if foreign="$(port_holder)" && [ -n "$foreign" ]; then
                       echo "replacing EdgeTerminal pid $foreign on port $PORT"
                       kill "$foreign" 2>/dev/null || true
                       for _ in $(seq 1 40); do is_ours "$foreign" || break; sleep 0.25; done
                       is_ours "$foreign" && kill -9 "$foreign" 2>/dev/null || true
                     fi
                     cmd_start ;;
  status)            cmd_status ;;
  install-service)   cmd_install_service ;;
  uninstall-service) cmd_uninstall_service ;;
  *) echo "usage: $0 {start|stop|restart|status|install-service|uninstall-service}" >&2; exit 2 ;;
esac
