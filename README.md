# FlightWall

Live aircraft around you, on a dot-matrix board, on your phone.

Point it at wherever you are and it shows what is actually overhead — airline,
route, type, altitude, speed, track, climb rate, and how far away it is — using
your phone's GPS. It runs on your own always-on laptop; nothing is hosted for
you and no account is involved.

```
 ╭──────╮  UNITED
 │  ·   │  ORD-LAX
 ╰──────╯  A-321NEO
 ALT:4100FT,SPD:250MPH
 TRK:327°,↓1088FPM
 DST:9.4MI SE,N37502
```

Three views: the board (one aircraft at a time, cycling or pinned), a radar
scope centred on you, and a distance-sorted list of everything in range. The
scope can rotate with the phone, so pointing it at an aircraft brings that
aircraft to the top of the display, and it draws towns, county and state lines
and major water underneath the traffic so there is something to orient by.

## The one thing that will bite you

**It must be served over HTTPS.** Safari and Chrome only hand out
`navigator.geolocation` on a secure origin. A plain `http://192.168.1.x:8730`
address will load the page perfectly and then never get a location fix, which
looks like a broken app rather than a setup problem. `scripts/expose.sh` exists
to solve exactly this.

## Install on the always-on Mac

```bash
git clone https://github.com/exspo/flightwall.git ~/flightwall
cd ~/flightwall
./scripts/install.sh
```

That registers a launchd agent (`com.cgriffin.flightwall`) that starts at login
and restarts if it dies. It needs only `python3` — no `pip install`, no
`node_modules`, nothing to re-provision later. Logs go to
`~/Library/Logs/flightwall.log`, deliberately outside the repo.

Then give it an HTTPS address:

```bash
./scripts/expose.sh
```

It picks the best method available:

| Method | Address | Reachable from | Survives reboot |
|---|---|---|---|
| **Tailscale** (recommended) | `https://<machine>.<tailnet>.ts.net` | anywhere, incl. cellular | yes, stable URL |
| Cloudflare quick tunnel | random `*.trycloudflare.com` | anywhere, public | no, new URL each run |
| Self-signed cert | `https://<lan-ip>:8730` | same Wi-Fi only | yes, if the DHCP lease holds |

Tailscale is the right answer for a machine that is always on: a stable private
hostname with a real certificate, so the home-screen icon keeps working.

Sign in to Tailscale on both the laptop and the phone, then:

```bash
brew install --cask tailscale
./scripts/expose.sh tailscale
```

Commands in this README carry no inline `#` comments, because macOS zsh does not
treat `#` as a comment at an interactive prompt — pasting one can leave you at a
`quote>` prompt or, worse, run a backtick or `>` inside it.

## Put it on the phone

1. Open the HTTPS URL in Safari, including the `?k=…` token the script prints.
2. Allow location when asked.
3. **Share → Add to Home Screen.**

It launches fullscreen with no browser chrome. The token is exchanged for a
long-lived cookie on first load and stripped from the URL, so it does not sit
in your bookmark.

## Try it without any of that

```bash
python3 server/flightwall.py --demo
```

Synthetic traffic around whatever location you give it, no network required.
Useful for checking the board renders before worrying about tunnels. Open the
`http://127.0.0.1:8730/?k=…` line it prints — `localhost` counts as a secure
origin, so geolocation works there.

## Options

Tap the gear:

- **Board shows** — cycle through nearby traffic, lock to the closest, or pin
  whatever you tap on the list or radar.
- **Units** — mph/miles, knots/nm, or km/h/km.
- **Radius** — 5 to 250 nm.
- **Filters** — airliners, general aviation, helicopters, military, ground
  vehicles (off by default; these are pushback tugs and snowploughs).
- **Show towns and boundaries** — the ground under the traffic.
- **Keep the screen awake** — for leaving it propped up on a desk.

Under the scope there is a **North up / Heading up** button. Heading up needs
one tap to grant compass access, because iOS only releases orientation data
after an explicit user gesture.

Everything persists in `localStorage`, per device.

## How it works

```
phone (GPS, PWA)  ──HTTPS──▶  laptop (flightwall.py)  ──▶  ADS-B feeds
```

The laptop proxies the feeds rather than the phone calling them directly. That
sidesteps CORS, collapses repeated polls into one upstream call, caches route
lookups, and means the phone only ever talks to your own machine.

Positions come from volunteer ADS-B receivers, tried in order until one answers:
[adsb.lol](https://adsb.lol), [adsb.fi](https://adsb.fi),
[airplanes.live](https://airplanes.live). All three speak the same
readsb/tar1090 format, so any one of them can be down without you noticing.

Routes (`ORD-LAX`) are a separate lookup — aircraft do not broadcast where they
are going. Those come from adsb.lol's route database, falling back to
adsbdb.com, cached for a day.

Those databases are keyed on flight number and go stale, and for regional
carriers they are wrong often enough to matter: a retired or reused number
keeps its old airports and reads as fact. So every record is checked against
the aircraft's own broadcast position — if the aircraft is nowhere near the
claimed path, the route is withheld and the panel says which record it
distrusted. Multi-leg routes are checked leg by leg, and the tolerance is
generous, since real flights hold and get vectored around weather.

This makes wrong routes stop appearing; it does not make routes correct. For
genuinely reliable routes and times you would need a commercial flight-data
API with an account and a key — a different shape of project from a
self-hosted board with no accounts in it.

Map features are clipped to the current view on the laptop and sent as
distances and bearings, so a view costs a few kilobytes rather than the 2.4 MB
the full dataset occupies, and the phone needs no map library. The scope
compresses distance with a square root so distant traffic stays on screen;
the map uses the same compression, which means directions are true and
distances are squashed toward the rim. Deliberately absent: roads, whose
Natural Earth layer alone is 50 MB, and raster map tiles, which would add a
network dependency and break offline use.

| Path | What it is |
|---|---|
| `server/flightwall.py` | The whole server. Stdlib only. |
| `server/demo_feed.py` | Synthetic traffic for `--demo`. |
| `web/led.js` | Dot-matrix renderer and a hand-coded 5×7 font. |
| `web/app.js` | Board, radar, list, settings, geolocation. |
| `scripts/install.sh` | launchd agent. `--uninstall` to remove. |
| `scripts/expose.sh` | HTTPS via Tailscale, Cloudflare, or self-signed. |
| `server/landmarks.py` | Ground features, clipped to the current view. |
| `scripts/build_landmarks.py` | Rebuilds `server/data/` from public sources. |
| `tests/test_server.py` | `python3 -m unittest discover -s tests` |

State lives in `~/.flightwall/` — the access token and the route cache. It is
never written into the repo.

The flight number in the route panel links out to FlightAware for the full
picture — schedules, history, the things a raw ADS-B feed cannot tell you. It
is the only part of the app that reaches anywhere other than your own laptop,
and only when you tap it.

## What it cannot show you

- **Aircraft without ADS-B.** Most airliners transmit; many older light
  aircraft do not, and will simply be absent.
- **Low-altitude traffic far from a receiver.** Coverage comes from volunteers
  with antennas. It is excellent above a few thousand feet near cities and
  patchy at low level in rural areas.
- **Routes for private flights.** There is no route to look up.
- **Anything, if the laptop is asleep.** The phone is a window onto that
  machine; if it is off, the board is dark.

Aircraft positions are public broadcast data. Some operators use anonymised
addresses, and those show up without a registration.

## Security

The server binds all interfaces and requires a token, generated once into
`~/.flightwall/config.json` and accepted as `?k=…` or a cookie. That matters
most behind a public Cloudflare tunnel, where the URL is the only thing between
your board and the internet. `--no-auth` turns the check off; only do that on a
LAN you trust.

## Uninstall

```bash
./scripts/install.sh --uninstall
rm -rf ~/.flightwall
```
