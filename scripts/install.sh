#!/usr/bin/env bash
# Install FlightWall as a launchd agent on the always-on Mac, so the board is
# up whenever the laptop is. Idempotent: safe to re-run after a git pull.
#
#   ./scripts/install.sh            install or update
#   ./scripts/install.sh --uninstall
#
# Every path derives from $HOME. Logs deliberately live outside the repo so
# they never end up in a commit.

set -euo pipefail

LABEL="com.cgriffin.flightwall"
PORT="${FLIGHTWALL_PORT:-8730}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
DOMAIN="gui/$(id -u)"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "this installer targets macOS launchd; on Linux use systemd or just run server/flightwall.py"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  say "removed $LABEL (state in ~/.flightwall was left alone)"
  exit 0
fi

PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || die "python3 not found. Install the Xcode command line tools: xcode-select --install"
"$PYTHON" - <<'EOF' || die "python3 is older than 3.9; install a newer one (brew install python3)"
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
EOF

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

say "writing $PLIST"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$REPO/server/flightwall.py</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_DIR/flightwall.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/flightwall.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$HOME</string>
  </dict>
</dict>
</plist>
PLIST_EOF

# bootout first so a re-run picks up an edited plist rather than silently
# keeping the old one loaded.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

say "waiting for the server to answer"
for _ in $(seq 1 25); do
  if curl -fsS --max-time 1 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 0.4
done
[[ "${ok:-}" == 1 ]] || die "server did not come up; check $LOG_DIR/flightwall.log"

TOKEN="$("$PYTHON" -c 'import json,os,pathlib;p=pathlib.Path(os.environ.get("FLIGHTWALL_HOME", pathlib.Path.home()/".flightwall"))/"config.json";print(json.loads(p.read_text())["token"])' 2>/dev/null || true)"

say "FlightWall is running"
echo
echo "  local     http://127.0.0.1:$PORT/?k=$TOKEN"
echo "  token     $TOKEN"
echo "  logs      $LOG_DIR/flightwall.log"
echo
echo "Your phone still needs an HTTPS address - browsers refuse to share location"
echo "over plain http. Run:  ./scripts/expose.sh"
