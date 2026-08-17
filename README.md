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
are going. Three sources answer, in descending order of how much they can be
trusted.

**FlightAware**, if you have set up a key. This is the only source that knows
what today's date is, so its answer is used as given and never second-guessed.

It is also the only source that costs money, and the board is exactly the wrong
shape for that: in cycle mode it changes aircraft every nine seconds, and
296 new callsigns an hour cross a 60nm circle (measured). Querying whatever
happened to be on screen would run to hundreds of dollars a month against a $5
allowance. So a query is only ever spent because a person asked for a
particular aircraft:

- one when the app opens, for whatever is nearest
- one more each time you tap an aircraft
- nothing at all for cycling, and nothing for tapping the same aircraft twice

Answers are cached for six hours, and a hard monthly cap (`AEROAPI_MONTHLY_CAP`,
800 queries ≈ $4) stops spending dead if something ever loops. Without a key
this tier is skipped entirely and nothing else changes.

**The community route tables** — adsbdb.com and the VRS standing data behind
adsb.lol — which are free and cost nothing to ask. They are also a flight
number mapped to whatever that number meant on the day the table was built,
with no date attached, so a reassigned number keeps its old airports
indefinitely. Sampling 70 aircraft overhead in August 2026: adsbdb answered for
38 of them and 27 of those 38 were contradicted by where the aircraft actually
was. The same callsign frequently resolves to three different routes depending
which table you ask.

So table records are held to what the aircraft can verify. Every record is
checked against the broadcast position, and the one on the board gets two
stronger tests: its origin must match where the aircraft actually took off,
and if the aircraft is measurably descending toward some other field, the
record's destination loses. A record that fails is dropped rather than
patched — a reused number flies several legs a day, and departing the
recorded destination proves the record is the wrong leg, not the same leg
backwards. What survives on screen is the observed departure and, once the
descent starts, the observed arrival.

**The aircraft itself**, which cannot go stale because it is an observation
rather than a record. Its 24 hours of track history is rewound to the last time
it was on the ground, which gives the airport it actually departed from. Its
descent rate, groundspeed and track are projected forward to a patch of ground,
which usually contains exactly one airport — that is where it is going. The
autopilot's selected altitude distinguishes an aircraft levelled off mid-descent
from one that is genuinely cruising.

The second answer is only offered while an aircraft is low enough for it to
mean something. At cruise an aeroplane looks identical whether it is stopping at
the next field or carrying on for another two thousand miles, so nothing is
said. Origin lookups pull a few hundred KB of track history and so are made
only for the aircraft on screen.

When none of the three can name an airport, the panel says what the aircraft is
doing instead — `Descending through 6,000 ft, 6.2MI SSE` — rather than
explaining which record it distrusted.

### FlightAware key (optional)

[AeroAPI's Personal tier](https://www.flightaware.com/commercial/aeroapi/) is
free up to $5/month of usage, $10/month if you feed ADS-B. Because only the
focused aircraft is ever looked up and answers are cached, that allowance goes
a long way. Put the key in either place:

```bash
export FLIGHTWALL_AEROAPI_KEY=...
```

or add `"aeroapi_key": "..."` to `~/.flightwall/config.json`, which is already
chmod 600.

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
- **Filed routes for private flights.** No table carries a tail number, so
  there is nothing to look up. The departure airport still resolves from the
  aircraft's track history, and the arrival from its descent.
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
