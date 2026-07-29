#!/usr/bin/env bash
# Give the phone an HTTPS address for the local FlightWall server.
#
# This is not optional polish. Safari and Chrome only expose
# navigator.geolocation on a secure origin, so a plain http://192.168.x.x
# address will load the page and then never get a location fix.
#
#   ./scripts/expose.sh            pick the best available method
#   ./scripts/expose.sh tailscale  stable private hostname, real certificate
#   ./scripts/expose.sh cloudflare public URL, no account, changes each run
#   ./scripts/expose.sh selfsigned LAN only, needs a profile on the phone

set -euo pipefail

PORT="${FLIGHTWALL_PORT:-8730}"
METHOD="${1:-auto}"
STATE="${FLIGHTWALL_HOME:-$HOME/.flightwall}"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

token() {
  python3 -c "import json,pathlib;print(json.loads((pathlib.Path('$STATE')/'config.json').read_text())['token'])" 2>/dev/null || true
}

curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 \
  || die "nothing is listening on port $PORT. Run ./scripts/install.sh first."

TOKEN="$(token)"
SUFFIX=""
[[ -n "$TOKEN" ]] && SUFFIX="/?k=$TOKEN"

if [[ "$METHOD" == "auto" ]]; then
  if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
    METHOD=tailscale
  elif command -v cloudflared >/dev/null 2>&1; then
    METHOD=cloudflare
  else
    METHOD=none
  fi
fi

case "$METHOD" in
  tailscale)
    # Best option for an always-on laptop: a stable hostname with a real
    # Let's Encrypt certificate, reachable from cellular, private to your
    # own devices, and it survives reboots.
    say "publishing through Tailscale"
    tailscale serve --bg --https=443 "http://127.0.0.1:$PORT" >/dev/null
    HOST="$(tailscale status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["Self"]["DNSName"].rstrip("."))')"
    echo
    echo "  Open this on your phone (once), then Share -> Add to Home Screen:"
    echo
    echo "    https://$HOST$SUFFIX"
    echo
    echo "  Your phone must be signed in to the same tailnet. This URL is stable,"
    echo "  so the home-screen icon keeps working after reboots."
    echo "  To stop:  tailscale serve --https=443 off"
    ;;

  cloudflare)
    say "opening a Cloudflare quick tunnel (Ctrl-C to stop)"
    echo
    echo "  Watch for the https://<something>.trycloudflare.com line below, then"
    echo "  append  $SUFFIX  to it on your phone."
    echo
    echo "  Note: this URL is public and changes every time you run it, so the"
    echo "  home-screen icon will break on restart. Tailscale is the better"
    echo "  long-term answer for an always-on machine."
    echo
    exec cloudflared tunnel --url "http://127.0.0.1:$PORT"
    ;;

  selfsigned)
    say "restart the server with TLS: python3 server/flightwall.py --tls --port $PORT"
    IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<laptop-lan-ip>')"
    echo
    echo "  Then, on the phone (same Wi-Fi):"
    echo "    1. Visit https://$IP:$PORT$SUFFIX and accept the warning."
    echo "    2. iOS only: Settings > General > About > Certificate Trust Settings,"
    echo "       and switch the flightwall.local certificate on. Until you do,"
    echo "       iOS treats the origin as insecure and withholds location."
    echo
    echo "  LAN only - this will not work on cellular, and the address changes"
    echo "  if the laptop's DHCP lease does."
    ;;

  none)
    cat <<'EOF'
No HTTPS method is available yet. Pick one:

  Tailscale (recommended for an always-on laptop)
      brew install --cask tailscale
      # sign in on the laptop and the phone, then:
      ./scripts/expose.sh tailscale
    Stable private hostname, real certificate, works over cellular.

  Cloudflare quick tunnel (fastest to try, no account)
      brew install cloudflared
      ./scripts/expose.sh cloudflare
    Public URL that changes on every restart.

  Self-signed certificate (LAN only)
      ./scripts/expose.sh selfsigned
    Requires trusting a certificate profile on the phone.
EOF
    exit 1
    ;;

  *)
    die "unknown method '$METHOD' (expected tailscale, cloudflare or selfsigned)"
    ;;
esac
