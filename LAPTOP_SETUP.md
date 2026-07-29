# FlightWall laptop setup

Sets up the **always-on laptop** as the FlightWall host, reachable from the
phone over Tailscale HTTPS. Two forms — use **Form A** (paste into a Claude Code
session on that laptop; it self-recovers) unless you specifically want raw shell
(**Form B**).

The laptop is the engine; the phone is the display. FlightWall shows aircraft
around **the phone's** GPS position, not the laptop's — the laptop only fetches
and proxies the data.

---

## FORM A — paste this into Claude Code on the always-on laptop

```
You are setting up FlightWall on THIS machine (my always-on laptop). It serves a
web app to my phone, which reports its own GPS position. Read this entire brief,
then execute it step by step. Verify each step and STOP LOUDLY if a check fails —
do not limp forward.

## What we're achieving
A launchd agent serving FlightWall on port 8730, published to my tailnet over
HTTPS, so my phone can load it from anywhere and add it to the home screen.

## The constraint that drives everything
Browsers only expose navigator.geolocation on a SECURE ORIGIN. A plain
http://192.168.x.x address will load the page and then silently never get a
location fix — which looks like a broken app, not a config problem. HTTPS is
mandatory, and Tailscale is how we get it. Do not "simplify" by serving plain
HTTP on the LAN; that produces an app that cannot work.

## Steps (verify each before moving on)
1. Clone: `git clone -b claude/location-based-phone-app-skyx4u https://github.com/exspo/flightwall.git ~/flightwall`
   (if it exists: `git -C ~/flightwall pull --rebase --autostash`).
   CHECK: `~/flightwall/scripts/install.sh` exists.
2. Python: `python3 --version` must be 3.9+. There are NO other dependencies —
   no pip, no npm. If you find yourself installing packages, you have gone wrong.
   CHECK: version prints 3.9 or newer.
3. Self-test: `cd ~/flightwall && python3 -m unittest discover -s tests`.
   CHECK: 25 tests, OK. If any fail, STOP and show me the failure.
4. Install: `./scripts/install.sh`. This writes
   ~/Library/LaunchAgents/com.cgriffin.flightwall.plist, bootstraps it, and waits
   for the server to answer. It prints an access token — keep it.
   CHECK: `curl -fsS http://127.0.0.1:8730/api/health` returns JSON with ok:true.
   CHECK: `launchctl print gui/$(id -u)/com.cgriffin.flightwall` shows state = running.
5. Live-data check: with the token from step 4, run
   `curl -sS "http://127.0.0.1:8730/api/aircraft?lat=<my latitude>&lon=<my longitude>&radius=60&k=<token>"`
   using this machine's rough location. Report how many aircraft came back and
   which provider answered ("source"). A 502 here means all three ADS-B feeds
   were unreachable — report the error text, do not silently continue.
6. Tailscale CLI: confirm it exists. If `command -v tailscale` fails, check
   /Applications/Tailscale.app/Contents/MacOS/Tailscale — the Mac App Store build
   keeps the CLI inside the bundle. If it's the App Store build, open Tailscale
   and choose "Install CLI". If Tailscale isn't installed at all:
   `brew install --cask tailscale`, then sign in.
   CHECK: `tailscale status` prints this machine without error.
7. Tailnet HTTPS: `tailscale serve` needs MagicDNS AND HTTPS Certificates enabled
   for the tailnet. These are per-tailnet toggles in the admin console, NOT
   something you can set from the CLI. If step 8 fails on certificates, tell me to
   enable both at https://login.tailscale.com/admin/dns and wait for me.
8. Publish: `./scripts/expose.sh tailscale`. It prints the phone URL.
   CHECK: the printed URL is https://<machine>.<tailnet>.ts.net/?k=<token>
   CHECK: `curl -fsS https://<that host>/api/health` returns ok:true.
9. Report the final phone URL to me in full, on its own line, so I can open it.

## Known wrinkles & recovery (fix within these rules; don't stall)
- Everything derives from $HOME. A hardcoded /Users/<name> is a bug.
- Port 8730 already in use -> something else is bound; find it with
  `lsof -iTCP:8730 -sTCP:LISTEN`. Do NOT just pick another port silently; if you
  must change it, set FLIGHTWALL_PORT for BOTH install.sh and expose.sh and tell me.
- launchd bootstrap fails "service already loaded" -> `launchctl bootout
  gui/$(id -u)/com.cgriffin.flightwall` then re-run install.sh.
- Agent flaps / server not answering -> read ~/Library/Logs/flightwall.log. That
  is the only log; it is deliberately outside the repo.
- `tailscale serve` errors about certificates -> step 7, tailnet toggles. This is
  the single most common failure and it is NOT fixable from this machine.
- ADS-B feeds all 502 -> the app is fine, the upstreams or the network are not.
  `--demo` mode proves the install independently: `python3 server/flightwall.py
  --demo --port 8731` then load http://127.0.0.1:8731 with the printed token.
- Token lost -> it is in ~/.flightwall/config.json.

## Your mandate
Work autonomously through the steps, fixing wrinkles with the playbook above.
Only stop and ask me if: (a) you need a tailnet admin-console change (step 7),
(b) a test fails, or (c) you hit something the playbook doesn't cover. Finish
with a short report: tests passing? agent running? which provider answered and
how many aircraft? and the phone URL.
```

---

## FORM B — raw shell fallback

Paste this block as-is. It carries no inline comments on purpose: macOS zsh has
`interactive_comments` **off** by default, so a `#` pasted at the prompt is not a
comment. An apostrophe after one opens a quote and drops you at `quote>`, and a
backtick or `>` would be run as substitution or redirection. Prose stays out of
the block for that reason.

```bash
set -e
git clone -b claude/location-based-phone-app-skyx4u https://github.com/exspo/flightwall.git ~/flightwall 2>/dev/null \
  || git -C ~/flightwall pull --rebase --autostash
cd ~/flightwall
python3 --version
python3 -m unittest discover -s tests
./scripts/install.sh
curl -fsS http://127.0.0.1:8730/api/health >/dev/null && echo "server ok"
tailscale status >/dev/null || tailscale up
./scripts/expose.sh tailscale
```

Step by step, and what to expect:

| Line | Expect |
|---|---|
| `python3 --version` | 3.9 or newer |
| `python3 -m unittest …` | `Ran 25 tests … OK` |
| `./scripts/install.sh` | prints your access token and the local URL |
| `curl … /api/health` | `server ok` |
| `tailscale status` | this machine listed; `tailscale up` runs only if not signed in |
| `./scripts/expose.sh tailscale` | prints the `https://….ts.net/?k=…` phone URL |

If `tailscale` is reported as not found but the app **is** installed, you have
the Mac App Store build, which keeps its CLI inside the app bundle. Open
Tailscale and choose "Install CLI", then re-run the last two lines.
`expose.sh` also looks inside the bundle itself, so it will usually find it
regardless.

`expose.sh tailscale` needs MagicDNS **and** HTTPS Certificates enabled for the
tailnet, at <https://login.tailscale.com/admin/dns>. Those are admin-console
toggles that cannot be set from this machine; the script detects that specific
failure and says so.

## On the phone

1. Install Tailscale, sign in to the same tailnet, and connect.
2. Open the `https://<machine>.<tailnet>.ts.net/?k=<token>` URL in Safari.
3. Allow location when prompted.
4. **Share → Add to Home Screen.**

The token is exchanged for a long-lived cookie and stripped from the URL, so it
does not end up in the bookmark.

## Success criteria

- `launchctl print gui/$(id -u)/com.cgriffin.flightwall` shows state = running
- `curl https://<host>.ts.net/api/health` returns `ok:true` from the phone's network
- The board shows aircraft and the location chip reads `GPS ±<n>m`, not
  `Last known location`
- It survives a laptop reboot without re-running anything

## If the phone shows "Last known location"

That is the secure-origin problem, every time. Confirm the address in Safari's
bar starts with `https://` and ends in `.ts.net` — not an IP address, and not
`http://`. An IP address cannot work no matter what certificate is behind it.
