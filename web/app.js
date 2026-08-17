import { Frame, LedBoard, C, PALETTE, textWidth } from "./led.js";

const BOARD_COLS = 132; // 22 characters at 6px pitch
const BOARD_ROWS = 52;

const BADGE = { x: 0, y: 0, w: 26, h: 24 };
const TEXT_X = 30;
const TEXT_CLIP = { x: TEXT_X, w: BOARD_COLS - TEXT_X };
const LINE_Y = [1, 9, 17, 27, 35, 43];

const POLL_MS = 6000;
const ROUTE_POLL_MS = 25000;
const CYCLE_MS = 9000;
const SCROLL_PX_PER_S = 14;
const MAX_TOWN_LABELS = 22;
const LANDMARK_MOVE_NM = 3; // refetch the map only after moving this far

const CLASS_COLOR = {
  commercial: C.CYAN,
  general: C.GREEN,
  helicopter: C.VIOLET,
  military: C.AMBER,
  ground: C.DIM,
  drone: C.VIOLET,
  lighter: C.BLUE,
  unknown: C.WHITE,
};

const CLASS_LABEL = {
  commercial: "Airliner",
  general: "General aviation",
  helicopter: "Helicopter",
  military: "Military",
  ground: "Ground vehicle",
  drone: "Drone",
  lighter: "Lighter than air",
  unknown: "Unidentified",
};

const UNITS = {
  imperial: { alt: "ft", speed: "MPH", dist: "MI", speedK: 1.15078, distK: 1.15078, altK: 1 },
  aviation: { alt: "ft", speed: "KT", dist: "NM", speedK: 1, distK: 1, altK: 1 },
  metric: { alt: "m", speed: "KMH", dist: "KM", speedK: 1.852, distK: 1.852, altK: 0.3048 },
};

const DEFAULT_SETTINGS = {
  units: "imperial",
  radius: 60,
  filters: { commercial: true, general: true, helicopter: true, military: true, ground: false },
  focus: "cycle", // nearest | cycle | pinned
  keepAwake: true,
  minAlt: 0,
  headingUp: false,
  showMap: true,
};

const state = {
  settings: loadSettings(),
  position: loadLastPosition(),
  positionSource: null,
  aircraft: [],
  routes: new Map(),
  // What each aircraft says about itself, keyed by hex: the phase of flight,
  // the airport it is descending towards, and where it took off. Separate
  // from `routes` because it is an observation rather than a record, so it is
  // refreshed every poll instead of cached for the day.
  derived: new Map(),
  airlines: {},
  pinned: null,
  focusHex: null,
  cycleIndex: 0,
  lastCycle: 0,
  lastFetch: 0,
  lastRouteFetch: 0,
  routesInFlight: false,
  routeError: null,
  // A live-flight lookup costs a query against a small monthly allowance, so
  // it happens only when somebody asks for a particular aircraft: once on
  // open for whatever is nearest, and once more per tap. Cycling past an
  // aircraft is not asking. `liveAsked` stops a poll re-requesting one that
  // has already been paid for.
  liveWanted: null,
  liveAsked: new Set(),
  openedLive: false,
  fetching: false,
  error: null,
  source: null,
  allProvidersEmpty: false,
  landmarks: null,
  landmarkKey: null,
  landmarksInFlight: false,
  scroll: new Map(),
  wakeLock: null,
};

const el = (id) => document.getElementById(id);

// ------------------------------------------------------------------ settings

function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem("fw.settings") || "{}");
    return {
      ...DEFAULT_SETTINGS,
      ...saved,
      filters: { ...DEFAULT_SETTINGS.filters, ...(saved.filters || {}) },
    };
  } catch {
    return structuredClone(DEFAULT_SETTINGS);
  }
}

function saveSettings() {
  try {
    localStorage.setItem("fw.settings", JSON.stringify(state.settings));
  } catch {
    /* private browsing; not worth surfacing */
  }
}

function loadLastPosition() {
  try {
    const saved = JSON.parse(localStorage.getItem("fw.position") || "null");
    if (saved && Number.isFinite(saved.lat) && Number.isFinite(saved.lon)) return saved;
  } catch {
    /* ignore */
  }
  return null;
}

// ------------------------------------------------------------------ formatting

function unit() {
  return UNITS[state.settings.units] || UNITS.imperial;
}

function fmtAlt(ft) {
  if (ft === null || ft === undefined) return "--";
  const u = unit();
  if (u.altK !== 1) return `${Math.round(ft * u.altK)}M`;
  return ft >= 10000 ? `${(ft / 1000).toFixed(1)}KFT` : `${Math.round(ft)}FT`;
}

function fmtSpeed(kt) {
  if (kt === null || kt === undefined) return "--";
  const u = unit();
  return `${Math.round(kt * u.speedK)}${u.speed}`;
}

function fmtDist(nm) {
  if (nm === null || nm === undefined) return "--";
  const u = unit();
  const value = nm * u.distK;
  return `${value < 10 ? value.toFixed(1) : Math.round(value)}${u.dist}`;
}

function fmtVs(fpm) {
  if (!fpm) return "•LEVEL";
  return `${fpm > 0 ? "↑" : "↓"}${Math.abs(Math.round(fpm))}FPM`;
}

function compassPoint(deg) {
  const points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  return points[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16];
}

/** Callsigns are ICAO airline code plus a flight number: UAL2402. A tail
 *  number like N814GA is not an airline, so it must not match. */
function airlineOf(ac) {
  const flight = ac.flight || "";
  if (flight.length < 4) return null;
  const code = flight.slice(0, 3);
  if (!/^[A-Z]{3}$/.test(code) || !/^[0-9]/.test(flight[3])) return null;
  return { code, name: state.airlines[code] || code };
}

// Longest first, so "AIRBUS HELICOPTERS" wins over "AIRBUS".
const MANUFACTURERS = [
  "AIRBUS HELICOPTERS", "MCDONNELL DOUGLAS", "DE HAVILLAND CANADA", "DE HAVILLAND",
  "BOMBARDIER", "GULFSTREAM", "LOCKHEED MARTIN", "LOCKHEED", "AGUSTAWESTLAND",
  "AERO COMMANDER", "BRITTEN-NORMAN", "CANADAIR", "DASSAULT", "EMBRAER", "SIKORSKY",
  "AIRBUS", "BOEING", "CESSNA", "CIRRUS", "ROBINSON", "PIPER", "BEECH", "MOONEY",
  "PILATUS", "DIAMOND", "LEONARDO", "TEXTRON", "ANTONOV", "TUPOLEV", "SUKHOI",
  "EUROCOPTER", "HAWKER", "LEARJET", "GRUMMAN", "BELL", "ATR", "SAAB", "FOKKER",
];

/** "AIRBUS A-321neo" reads better on a narrow board as "A-321NEO". Only strip a
 *  prefix we recognise - blindly dropping the first word turns
 *  "AIRBUS HELICOPTERS H135" into "HELICOPTERS H135". */
function typeLabel(ac) {
  if (!ac.desc) return ac.type || "UNKNOWN TYPE";
  const desc = ac.desc.toUpperCase().trim();
  for (const maker of MANUFACTURERS) {
    if (desc.startsWith(`${maker} `)) {
      const rest = desc.slice(maker.length).trim();
      if (rest) return rest;
    }
  }
  return desc;
}

function titleOf(ac) {
  const airline = airlineOf(ac);
  if (airline) return airline.name.toUpperCase();
  if (ac.owner) return ac.owner.toUpperCase();
  if (ac.mil) return "MILITARY";
  return ac.reg || ac.flight || ac.hex.toUpperCase();
}

/**
 * The route for an aircraft, but only when its own position supports it.
 *
 * Route databases are keyed on flight number and go stale; the server checks
 * each record against where the aircraft actually is and flags the ones that
 * do not hold up. A confidently wrong route is worse than none, so a flagged
 * record never reaches the board or the list.
 */
function usableRoute(ac) {
  const route = state.routes.get(ac.flight);
  return route && !route.suspect ? route : null;
}

function colorOf(ac) {
  if (ac.emergency) return C.RED;
  return CLASS_COLOR[ac.class] ?? C.WHITE;
}

// ------------------------------------------------------------------- compass

const compass = {
  enabled: false,     // user wants heading-up
  live: false,        // listeners attached and events arriving
  heading: null,      // smoothed, degrees clockwise from true north
  accuracy: null,     // iOS only; negative means uncalibrated
  warned: false,
};

/** Shortest-path angular smoothing. Averaging 359 and 1 linearly gives 180,
 *  which would swing the whole scope around every time you face north. */
function smoothAngle(previous, next, factor) {
  if (previous === null) return next;
  const delta = ((next - previous + 540) % 360) - 180;
  return (previous + factor * delta + 360) % 360;
}

function onOrientation(event) {
  let heading = null;
  if (typeof event.webkitCompassHeading === "number" && !Number.isNaN(event.webkitCompassHeading)) {
    // iOS: already degrees clockwise from north.
    heading = event.webkitCompassHeading;
    if (typeof event.webkitCompassAccuracy === "number") compass.accuracy = event.webkitCompassAccuracy;
  } else if (event.absolute && typeof event.alpha === "number") {
    // Spec alpha runs anticlockwise from north, so it has to be inverted.
    heading = (360 - event.alpha) % 360;
  }
  if (heading === null || Number.isNaN(heading)) return;

  if (!compass.live) {
    compass.live = true;
    syncCompassButton(); // drops the "waiting" label the moment data arrives
  }
  compass.heading = smoothAngle(compass.heading, heading, 0.18);

  // A negative accuracy means iOS does not trust the reading yet.
  if (!compass.warned && compass.accuracy !== null && compass.accuracy < 0) {
    compass.warned = true;
    showBanner("Compass needs calibrating — wave the phone in a figure eight.");
  }
}

function attachOrientation() {
  window.addEventListener("deviceorientationabsolute", onOrientation, true);
  window.addEventListener("deviceorientation", onOrientation, true);
}

/**
 * iOS 13+ only delivers orientation events after an explicit grant, and the
 * request must come from a user gesture — so this is always called from a tap.
 */
async function enableCompass() {
  const DOE = window.DeviceOrientationEvent;
  if (!DOE) {
    showBanner("This device does not report a compass heading.", true);
    return false;
  }
  if (typeof DOE.requestPermission === "function") {
    let outcome;
    try {
      outcome = await DOE.requestPermission();
    } catch {
      // Thrown when there is no user activation, which is recoverable: the
      // next tap on the button will have it.
      showBanner("Tap the orientation button again to allow compass access.", true);
      return false;
    }
    if (outcome !== "granted") {
      showBanner(
        "Compass access was denied. Allow Motion & Orientation for this site in " +
          "Settings → Apps → Safari, then try again.",
        true
      );
      return false;
    }
  }
  attachOrientation();
  return true;
}

/** Degrees the scope is rotated by. Zero means north-up. */
function headingUp() {
  return compass.enabled && compass.live && compass.heading !== null ? compass.heading : 0;
}

async function setCompassEnabled(on) {
  if (on && !compass.live) {
    if (!(await enableCompass())) return;
  }
  compass.enabled = on;
  state.settings.headingUp = on;
  saveSettings();
  syncCompassButton();
}

function syncCompassButton() {
  const button = el("orientBtn");
  const active = compass.enabled;
  button.setAttribute("aria-pressed", String(active));
  button.classList.toggle("active", active);
  button.textContent = !active
    ? "North up"
    : compass.live
    ? "Heading up"
    : "Waiting for compass…";
}

// ------------------------------------------------------------------ location

function setPosition(lat, lon, accuracy, source) {
  state.position = { lat, lon, accuracy, ts: Date.now() };
  state.positionSource = source;
  try {
    localStorage.setItem("fw.position", JSON.stringify(state.position));
  } catch {
    /* ignore */
  }
  el("locStatus").textContent =
    source === "gps"
      ? `GPS ±${Math.round(accuracy || 0)}m`
      : source === "manual"
      ? "Manual location"
      : "Last known location";
  el("locStatus").className = `chip ${source === "gps" ? "ok" : "warn"}`;
  refresh(true);
}

function startLocation() {
  if (!window.isSecureContext) {
    // Safari silently withholds geolocation on an insecure origin, which looks
    // like a broken app rather than a configuration problem. Say so plainly.
    showBanner(
      "This page is not on HTTPS, so your phone will not share its location. " +
        "Serve FlightWall over HTTPS (see scripts/expose.sh) or set a location manually.",
      true
    );
  }
  if (!navigator.geolocation) {
    showBanner("This browser has no geolocation support. Set a location manually.", true);
    return;
  }
  navigator.geolocation.watchPosition(
    (pos) => {
      hideBanner();
      setPosition(pos.coords.latitude, pos.coords.longitude, pos.coords.accuracy, "gps");
    },
    (err) => {
      const hint =
        err.code === err.PERMISSION_DENIED
          ? "Location permission was denied. Allow it in Settings, or set a location manually."
          : `Could not get a location fix (${err.message}).`;
      showBanner(hint, true);
      if (state.position) {
        el("locStatus").textContent = "Last known location";
        el("locStatus").className = "chip warn";
        refresh(true);
      }
    },
    { enableHighAccuracy: true, maximumAge: 15000, timeout: 20000 }
  );
}

// ------------------------------------------------------------------ data

async function refresh(force = false) {
  if (!state.position || state.fetching) return;
  if (!force && Date.now() - state.lastFetch < POLL_MS - 500) return;
  state.fetching = true;
  const { lat, lon } = state.position;
  try {
    const url = `api/aircraft?lat=${lat.toFixed(5)}&lon=${lon.toFixed(5)}&radius=${state.settings.radius}`;
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
    const data = await res.json();
    state.aircraft = data.aircraft || [];
    state.source = data.source;
    state.allProvidersEmpty = Boolean(data.allProvidersEmpty);
    state.error = null;
    state.lastFetch = Date.now();
    hideBanner();
    fetchLandmarks();
    fetchRoutes();
    renderList();

    // One live lookup per app open, for whatever is overhead. After this the
    // board spends nothing until somebody taps an aircraft.
    if (!state.openedLive && state.aircraft.length) {
      state.openedLive = true;
      const nearest = visibleAircraft().find((ac) => ac.flight);
      if (nearest) requestLiveRoute(nearest);
    }
  } catch (err) {
    state.error = err.message;
    showBanner(`Could not reach the aircraft feed: ${err.message}`, true);
  } finally {
    state.fetching = false;
  }
}

/**
 * @param force skip the poll interval. Used when the board switches to an
 *        aircraft whose route is not known yet, so selecting something does
 *        not sit on "Looking up…" for most of a poll cycle.
 */
/** Everything the server needs to work out where an aircraft is going without
 *  asking a route table: where it is, which way it is pointing, how fast, and
 *  whether it is on its way down. */
function telemetryFor(ac) {
  return {
    callsign: ac.flight || "",
    hex: ac.hex,
    lat: ac.lat,
    lng: ac.lon,
    // The server speaks the readsb dialect on the way in; the board renames
    // these on the way out, so they are translated back here.
    track: ac.trk,
    gs: ac.gs,
    alt_baro: ac.gnd ? "ground" : ac.alt,
    baro_rate: ac.vs,
    nav_altitude_mcp: ac.sel,
  };
}

/**
 * Ask for the live, paid answer on one aircraft. Only ever called because a
 * person wanted this particular flight - a tap, or the nearest aircraft when
 * the app opens. Repeats are free: once a callsign has been asked for, the
 * server has it cached and there is nothing to buy.
 */
function requestLiveRoute(ac) {
  if (!ac || !ac.flight) return;
  const callsign = ac.flight;
  if (state.liveAsked.has(callsign)) return;
  state.liveAsked.add(callsign);
  state.liveWanted = callsign;
  fetchRoutes(true);
}

async function fetchRoutes(force = false) {
  if (!force && Date.now() - state.lastRouteFetch < ROUTE_POLL_MS) return;
  const wanted = state.aircraft
    .filter((ac) => airlineOf(ac) && !state.routes.has(ac.flight))
    .slice(0, 100)
    .map(telemetryFor);

  // The focused aircraft rides along on every poll even when its route is
  // already cached: it is the one on screen, its descent changes minute by
  // minute, and it is the only aircraft worth spending an origin lookup on.
  const focus = focusedAircraft();
  if (focus && !wanted.some((p) => p.hex === focus.hex)) {
    wanted.unshift(telemetryFor(focus));
  }

  const live = state.liveWanted;
  if ((!wanted.length && !live) || state.routesInFlight) return;
  state.lastRouteFetch = Date.now();
  state.routesInFlight = true;
  // Cleared before the request rather than after, so a slow reply cannot let
  // the next poll bill the same aircraft twice.
  state.liveWanted = null;

  // A live lookup is keyed on the callsign, so its aircraft has to be in the
  // payload even when its route is already cached.
  if (live && !wanted.some((p) => p.callsign === live)) {
    const ac = state.aircraft.find((a) => a.flight === live);
    if (ac) wanted.unshift(telemetryFor(ac));
  }

  try {
    const res = await fetch("api/routes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        planes: wanted,
        focus: focus ? focus.flight : null,
        live,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { routes, derived, errors } = await res.json();
    for (const [callsign, route] of Object.entries(routes || {})) {
      state.routes.set(callsign, route);
    }
    for (const [hex, info] of Object.entries(derived || {})) {
      // Keep the last known origin: it is fetched only for the focused
      // aircraft, and dropping it when focus moves on would make the panel
      // flicker on the way back.
      const previous = state.derived.get(hex);
      state.derived.set(hex, {
        ...info,
        origin: info.origin || (previous && previous.origin) || null,
      });
    }
    // An upstream failure returns no entry for the callsign, so without this
    // the UI would claim it is still looking, forever.
    state.routeError = errors && errors.length ? errors[0] : null;
    renderList();
    renderRoute(); // the focused aircraft's route may have just arrived
  } catch (err) {
    state.routeError = err.message;
    renderRoute();
  } finally {
    state.routesInFlight = false;
  }
}

// Classes the server can emit that have no checkbox of their own ride along
// with the closest one that does.
const FILTER_ALIAS = { drone: "general", lighter: "general", unknown: "general" };

/**
 * Ground features for the current view. Refetched only when you have actually
 * moved or changed range - the ground does not move, and redrawing uses the
 * cached copy every frame regardless.
 */
async function fetchLandmarks() {
  if (!state.position || !state.settings.showMap || state.landmarksInFlight) return;
  const { lat, lon } = state.position;
  const radius = state.settings.radius;

  if (state.landmarks && state.landmarkKey) {
    const moved = Math.hypot(
      (lat - state.landmarkKey.lat) * 60,
      (lon - state.landmarkKey.lon) * 60 * Math.cos((lat * Math.PI) / 180)
    );
    if (moved < LANDMARK_MOVE_NM && state.landmarkKey.radius === radius) return;
  }

  state.landmarksInFlight = true;
  try {
    const res = await fetch(
      `api/landmarks?lat=${lat.toFixed(4)}&lon=${lon.toFixed(4)}&radius=${radius}`,
      { credentials: "same-origin" }
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.landmarks = await res.json();
    state.landmarkKey = { lat, lon, radius };
  } catch {
    // The map is orientation, not function. Keep whatever we last had and
    // never let it interfere with the aircraft display.
  } finally {
    state.landmarksInFlight = false;
  }
}

function visibleAircraft() {
  const { filters, minAlt } = state.settings;
  return state.aircraft.filter((ac) => {
    if (!filters[FILTER_ALIAS[ac.class] || ac.class]) return false;
    if (minAlt && (ac.alt ?? 0) < minAlt) return false;
    return true;
  });
}

function focusedAircraft() {
  const list = visibleAircraft();
  if (!list.length) return null;
  if (state.settings.focus === "pinned" && state.pinned) {
    const pinned = list.find((ac) => ac.hex === state.pinned) || state.aircraft.find((ac) => ac.hex === state.pinned);
    if (pinned) return pinned;
  }
  if (state.settings.focus === "cycle") {
    const now = Date.now();
    if (now - state.lastCycle > CYCLE_MS) {
      state.lastCycle = now;
      state.cycleIndex += 1;
    }
    return list[state.cycleIndex % list.length];
  }
  return list[0];
}

// ------------------------------------------------------------------ the board

const board = { instance: null, frame: new Frame(BOARD_COLS, BOARD_ROWS) };

/**
 * Horizontal offset for a line that is wider than its window: hold at the
 * left, slide to the end, hold, slide back. Keyed by line index and reset
 * whenever that line's text changes, so the map stays six entries deep no
 * matter how many aircraft pass through the board.
 */
function scrollOffset(lineIndex, text, width, windowWidth, now) {
  if (width <= windowWidth) return 0;
  const previous = state.scroll.get(lineIndex);
  if (!previous || previous.text !== text) {
    state.scroll.set(lineIndex, { text, start: now });
    return 0;
  }

  const travel = width - windowWidth;
  const pause = 1.6;
  const duration = travel / SCROLL_PX_PER_S;
  let t = ((now - previous.start) / 1000) % (duration * 2 + pause * 2);

  if (t < pause) return 0;
  t -= pause;
  if (t < duration) return -t * SCROLL_PX_PER_S;
  t -= duration;
  if (t < pause) return -travel;
  return -(travel - (t - pause) * SCROLL_PX_PER_S);
}

function drawBadge(frame, ac) {
  const cx = BADGE.x + 12;
  const cy = BADGE.y + 11;
  const r = 11;
  const up = headingUp(); // the badge turns with the scope, or they disagree
  frame.circle(cx, cy, r, C.DIM);

  // Cardinal ticks, north picked out so the rose is readable at a glance.
  for (const [degrees, color] of [[0, C.WHITE], [90, C.DIM], [180, C.DIM], [270, C.DIM]]) {
    const rad = ((degrees - up - 90) * Math.PI) / 180;
    frame.set(Math.round(cx + Math.cos(rad) * r), Math.round(cy + Math.sin(rad) * r), color);
  }
  frame.set(cx, cy, C.DIM);
  if (!ac) return;

  const maxNm = Math.max(1, state.settings.radius);
  const scale = Math.min(1, Math.sqrt(Math.min(ac.dst, maxNm) / maxNm));
  const rad = ((ac.dir - up - 90) * Math.PI) / 180;
  const px = cx + Math.cos(rad) * (r - 2) * scale;
  const py = cy + Math.sin(rad) * (r - 2) * scale;
  frame.line(cx, cy, Math.round(px), Math.round(py), C.DIM);
  const color = colorOf(ac);
  frame.set(Math.round(px), Math.round(py), color);
  frame.set(Math.round(px) + 1, Math.round(py), color);
  frame.set(Math.round(px), Math.round(py) + 1, color);
  frame.set(Math.round(px) + 1, Math.round(py) + 1, color);
}

function boardLines(ac) {
  if (!ac) {
    return [
      { text: "NO AIRCRAFT", color: C.AMBER, x: TEXT_X, clip: TEXT_CLIP },
      { text: "IN RANGE", color: C.DIM, x: TEXT_X, clip: TEXT_CLIP },
      { text: "", color: C.DIM, x: TEXT_X, clip: TEXT_CLIP },
      { text: state.position ? `SCANNING ${state.settings.radius}NM` : "WAITING FOR LOCATION", color: C.DIM, x: 0 },
      { text: state.error ? "FEED ERROR" : "", color: C.RED, x: 0 },
      { text: "", color: C.DIM, x: 0 },
    ];
  }

  const route = usableRoute(ac);
  const airline = airlineOf(ac);
  const color = colorOf(ac);

  const line3 =
    ac.emergency
      ? `! ${String(ac.emergency).toUpperCase()} !`
      : ac.gnd
      ? "ON GROUND"
      : `ALT:${fmtAlt(ac.alt)},SPD:${fmtSpeed(ac.gs)}`;

  const line4 = ac.gnd
    ? `DST:${fmtDist(ac.dst)}`
    : `TRK:${ac.trk === null || ac.trk === undefined ? "---" : String(Math.round(ac.trk)).padStart(3, "0")}°,${fmtVs(ac.vs)}`;

  let line5 = `DST:${fmtDist(ac.dst)} ${compassPoint(ac.dir)}`;
  if (ac.reg) line5 += `,${ac.reg}`;
  if (route && route.airports && route.airports.length >= 2) {
    const from = route.airports[0];
    const to = route.airports[route.airports.length - 1];
    if (from.location && to.location) line5 += `,${from.location} > ${to.location}`.toUpperCase();
  }

  // The flight number always gets its own line. It used to share a line with
  // the route, which meant it vanished the moment a route resolved - exactly
  // when you most want to look the flight up.
  const identity = ac.flight || ac.reg || ac.hex.toUpperCase();
  const routeText = route && route.route ? route.route.toUpperCase() : airline ? "NO ROUTE" : typeLabel(ac);

  return [
    { text: titleOf(ac), color: C.WHITE, x: TEXT_X, clip: TEXT_CLIP },
    { text: identity, color: C.AMBER, x: TEXT_X, clip: TEXT_CLIP },
    { text: routeText, color: route && route.route ? color : C.DIM, x: TEXT_X, clip: TEXT_CLIP },
    { text: line3, color: ac.emergency ? C.RED : C.GREEN, x: 0 },
    { text: line4, color: C.GREEN, x: 0 },
    { text: line5, color: C.BLUE, x: 0 },
  ];
}

function drawBoard(now) {
  const ac = focusedAircraft();
  const nextHex = ac ? ac.hex : null;
  if (nextHex !== state.focusHex) {
    // The board is the source of truth for what is selected, and it is
    // resolved here per frame. Push the change into the list rather than
    // letting renderList paint a highlight one frame behind.
    state.focusHex = nextHex;
    syncListHighlight();
    renderRoute();
    if (ac && airlineOf(ac) && !state.routes.has(ac.flight)) fetchRoutes(true);
  }
  const frame = board.frame;
  frame.clear();
  drawBadge(frame, ac);

  boardLines(ac).forEach((line, i) => {
    if (!line.text) return;
    const clip = line.clip || { x: 0, w: BOARD_COLS };
    const width = textWidth(line.text);
    const offset = scrollOffset(i, line.text, width, clip.w, now);
    frame.text(line.text, line.x + offset, LINE_Y[i], line.color, clip);
  });

  board.instance.render(frame);
}

// ------------------------------------------------------------------ the scope

/**
 * Where something at a given distance and bearing lands on the scope.
 *
 * The radial scale is deliberately compressed with a square root so distant
 * traffic stays visible instead of bunching at the rim. Ground features use
 * exactly the same mapping, so a town and an aircraft at the same distance
 * sit at the same radius: bearings are true, distances are squashed.
 *
 * Everything that needs scope coordinates goes through here - drawing, the
 * map, and the tap hit-test - because any second copy of this would
 * eventually disagree with the first.
 */
function scopeXY(geometry, dst, dir) {
  const { cx, cy, maxR, maxNm, up } = geometry;
  const scale = Math.min(1, Math.sqrt(Math.min(dst, maxNm) / maxNm));
  const rad = ((dir - up - 90) * Math.PI) / 180;
  return [cx + Math.cos(rad) * maxR * scale, cy + Math.sin(rad) * maxR * scale];
}

const MAP_STYLE = {
  counties: { color: "rgba(120,140,180,0.30)", width: 1 },
  states: { color: "rgba(150,175,225,0.60)", width: 1.4 },
  lakes: { color: "rgba(70,150,220,0.65)", width: 1.2 },
};

function drawMap(ctx, geometry, reserved) {
  const map = state.landmarks;
  if (!map || !state.settings.showMap) return;

  for (const [layer, style] of Object.entries(MAP_STYLE)) {
    const runs = map.lines?.[layer];
    if (!runs || !runs.length) continue;
    ctx.strokeStyle = style.color;
    ctx.lineWidth = style.width;
    ctx.beginPath();
    for (const run of runs) {
      for (let i = 0; i < run.length; i += 2) {
        const [x, y] = scopeXY(geometry, run[i], run[i + 1]);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
    }
    ctx.stroke();
  }

  drawTownLabels(ctx, geometry, map.cities || [], reserved);
}

/**
 * Town names, nearest first, skipping any whose label would collide with one
 * already placed. The server sorts by distance, so when the view is crowded
 * the towns closest to you are the ones that survive.
 */
function drawTownLabels(ctx, geometry, cities, reserved) {
  ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";

  // Seeded with the scope's own labels - range rings and cardinal points -
  // which are drawn afterwards and would otherwise land on top of a town.
  const placed = [...(reserved || [])];
  let drawn = 0;
  for (const [name, , dst, dir] of cities) {
    if (drawn >= MAX_TOWN_LABELS) break;
    const [x, y] = scopeXY(geometry, dst, dir);
    // Leave the middle clear; that is where you are.
    if (Math.hypot(x - geometry.cx, y - geometry.cy) < 14) continue;

    const width = ctx.measureText(name).width;
    const box = { x: x + 4, y: y - 6, w: width + 4, h: 12 };
    if (box.x + box.w > geometry.cx + geometry.maxR) box.x = x - width - 8;
    if (placed.some((p) => box.x < p.x + p.w && box.x + box.w > p.x && box.y < p.y + p.h && box.y + box.h > p.y)) {
      continue;
    }
    placed.push(box);
    drawn += 1;

    ctx.fillStyle = "rgba(150,175,225,0.85)";
    ctx.beginPath();
    ctx.arc(x, y, 1.6, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(190,205,235,0.75)";
    ctx.fillText(name, box.x, y);
  }
  ctx.textBaseline = "alphabetic";
}

function drawRadar() {
  const canvas = el("radar");
  const ctx = canvas.getContext("2d");
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  const size = canvas.clientWidth;
  if (!size) return;
  if (canvas.width !== size * dpr) {
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    canvas.style.height = `${size}px`;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, size, size);

  const cx = size / 2;
  const cy = size / 2;
  const maxR = size / 2 - 14;
  const maxNm = Math.max(1, state.settings.radius);
  const u = unit();
  const geometry = { cx, cy, maxR, maxNm, up: headingUp() };

  // How far the whole scope is turned. Every bearing below is drawn relative
  // to this, so north-up and heading-up share one code path.
  const up = geometry.up;

  // The scope's own labels are drawn further down but their positions are
  // known now, so towns can be kept clear of them.
  ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.textAlign = "center";
  const reserved = [];
  const reserve = (text, x, y) => {
    const w = ctx.measureText(text).width + 6;
    reserved.push({ x: x - w / 2, y: y - 8, w, h: 15 });
  };
  for (const fraction of [0.33, 0.66, 1]) {
    reserve(`${Math.round(maxNm * fraction * u.distK)}${u.dist}`, cx + 26, cy - maxR * fraction + 12);
  }
  for (const [label, degrees] of [["N", 0], ["E", 90], ["S", 180], ["W", 270]]) {
    const rad = ((degrees - geometry.up - 90) * Math.PI) / 180;
    reserve(label, cx + Math.cos(rad) * (maxR + 9), cy + Math.sin(rad) * (maxR + 9) + 3.5);
  }

  // Ground first: it belongs under the traffic, not over it.
  drawMap(ctx, geometry, reserved);

  ctx.strokeStyle = "rgba(122,162,255,0.22)";
  ctx.fillStyle = "rgba(122,162,255,0.55)";
  ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.textAlign = "center";
  for (const fraction of [0.33, 0.66, 1]) {
    const r = maxR * fraction;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.stroke();
    // Range labels stay put: they annotate the display, not the ground.
    ctx.fillText(`${Math.round(maxNm * fraction * u.distK)}${u.dist}`, cx + 26, cy - r + 12);
  }

  // The cardinal axes turn with the compass.
  ctx.beginPath();
  for (const degrees of [0, 90]) {
    const rad = ((degrees - up - 90) * Math.PI) / 180;
    ctx.moveTo(cx - Math.cos(rad) * maxR, cy - Math.sin(rad) * maxR);
    ctx.lineTo(cx + Math.cos(rad) * maxR, cy + Math.sin(rad) * maxR);
  }
  ctx.stroke();

  ctx.fillStyle = "rgba(242,246,255,0.75)";
  for (const [label, degrees] of [["N", 0], ["E", 90], ["S", 180], ["W", 270]]) {
    const rad = ((degrees - up - 90) * Math.PI) / 180;
    ctx.fillText(label, cx + Math.cos(rad) * (maxR + 9), cy + Math.sin(rad) * (maxR + 9) + 3.5);
  }

  // When the scope is turning, mark the direction the phone is pointing so
  // "straight ahead" is unambiguous.
  if (up) {
    // Inside the rim: outside it would land on whichever cardinal letter has
    // rotated to the top.
    ctx.fillStyle = "rgba(53,224,208,0.9)";
    ctx.beginPath();
    ctx.moveTo(cx, cy - maxR + 2);
    ctx.lineTo(cx - 5, cy - maxR + 12);
    ctx.lineTo(cx + 5, cy - maxR + 12);
    ctx.closePath();
    ctx.fill();
  }

  // You, at the middle.
  ctx.fillStyle = "#f2f6ff";
  ctx.beginPath();
  ctx.arc(cx, cy, 3, 0, Math.PI * 2);
  ctx.fill();

  for (const ac of visibleAircraft()) {
    // Compress the radial scale so distant traffic stays on the scope while
    // nearby traffic still separates out near the middle.
    const [x, y] = scopeXY(geometry, ac.dst, ac.dir);
    const focused = ac.hex === state.focusHex;
    const color = PALETTE[colorOf(ac)];

    ctx.save();
    ctx.translate(x, y);
    // The aircraft's own track rotates with the scope too, so its nose keeps
    // pointing where it is actually going.
    ctx.rotate((((ac.trk ?? 0) - up - 90) * Math.PI) / 180);
    ctx.fillStyle = color;
    ctx.globalAlpha = focused ? 1 : 0.8;
    ctx.beginPath();
    ctx.moveTo(6, 0);
    ctx.lineTo(-4, 4);
    ctx.lineTo(-2, 0);
    ctx.lineTo(-4, -4);
    ctx.closePath();
    ctx.fill();
    ctx.restore();

    if (focused) {
      ctx.strokeStyle = color;
      ctx.globalAlpha = 1;
      ctx.beginPath();
      ctx.arc(x, y, 10, 0, Math.PI * 2);
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
}

// ------------------------------------------------------------------ the list

function renderList() {
  const list = visibleAircraft();
  el("count").textContent = String(list.length);
  el("sourceChip").textContent = state.source ? `via ${state.source}` : "…";

  const container = el("list");
  if (!list.length) {
    // "Nothing nearby" has several very different causes and they need
    // telling apart: the wrong ones are silently wrong, and you cannot tell
    // which you are looking at from an empty list.
    let why;
    if (state.aircraft.length) {
      const hidden = state.aircraft.length;
      why = `<b>${hidden} aircraft nearby</b>, all hidden by your filters. Check the "Show" boxes in settings.`;
    } else if (!state.position) {
      why = "Waiting for a location fix. Nothing can be looked up until the phone reports where it is.";
    } else if (state.error) {
      why = `Could not reach the aircraft feed: ${escapeHtml(state.error)}`;
    } else {
      const where = `${state.position.lat.toFixed(3)}, ${state.position.lon.toFixed(3)}`;
      const src = state.positionSource === "gps" ? "GPS" : "a saved location";
      why =
        `No aircraft within ${state.settings.radius} NM of ${where}, searched using ${src}.` +
        (state.allProvidersEmpty
          ? " All three feeds agree, so this is a real coverage gap rather than one feed misbehaving."
          : "") +
        " If you can see aircraft, check that location looks right.";
    }
    container.innerHTML = `<p class="empty">${why}</p>`;
    return;
  }

  container.innerHTML = list
    .slice(0, 60)
    .map((ac) => {
      const route = usableRoute(ac);
      const color = PALETTE[colorOf(ac)];
      const title = titleOf(ac);
      // Flight number first. The route used to replace it, which hid the one
      // identifier you need to look a flight up.
      const ident = ac.flight || ac.reg;
      const sub = [
        ident === title ? null : ident,
        route && route.route ? route.route : null,
        typeLabel(ac),
      ]
        .filter(Boolean)
        .join(" · ");
      return `
        <button class="row${ac.hex === state.focusHex ? " active" : ""}" data-hex="${ac.hex}">
          <span class="dot" style="background:${color};box-shadow:0 0 8px ${color}"></span>
          <span class="row-main">
            <span class="row-title">${escapeHtml(title)}</span>
            <span class="row-sub">${escapeHtml(sub || "unknown")}</span>
          </span>
          <span class="row-metrics">
            <span>${escapeHtml(fmtDist(ac.dst))} ${escapeHtml(compassPoint(ac.dir))}</span>
            <span class="muted">${ac.gnd ? "on ground" : escapeHtml(fmtAlt(ac.alt))}</span>
          </span>
        </button>`;
    })
    .join("");
}

/** ISO 3166-1 alpha-2 to a flag emoji, by offsetting into the regional
 *  indicator block. Cheap way to mark an international leg. */
function flagOf(iso2) {
  if (!iso2 || iso2.length !== 2 || !/^[A-Z]{2}$/.test(iso2)) return "";
  return String.fromCodePoint(...[...iso2].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}

/**
 * Where to send someone who wants the full picture on a flight.
 *
 * FlightAware's /live/flight/ path accepts an airline callsign and a tail
 * number alike, so one URL shape covers both. An aircraft broadcasting
 * neither still has its ICAO address, which the tracker that feeds this app
 * can look up.
 */
function trackerLink(ac) {
  const ident = (ac.flight || ac.reg || "").trim();
  if (ident) {
    return {
      url: `https://flightaware.com/live/flight/${encodeURIComponent(ident)}`,
      label: "FlightAware",
    };
  }
  if (ac.hex) {
    return { url: `https://adsb.lol/?icao=${encodeURIComponent(ac.hex)}`, label: "adsb.lol" };
  }
  return null;
}

const EXTERNAL_ICON =
  '<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">' +
  '<path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" ' +
  'd="M14 4h6v6M20 4l-8.5 8.5M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>';

function routeLeg(role, airport) {
  const code = airport.iata || airport.icao || "??";
  const flag = flagOf((airport.countryiso2 || "").toUpperCase());
  const city = [airport.location, flag].filter(Boolean).join(" ");
  return `
    <div class="route-leg ${role === "From" ? "from" : "to"}">
      <span class="route-role">${role}</span>
      <span class="route-code">${escapeHtml(code)}</span>
      <span class="route-place">
        <span class="route-airport">${escapeHtml(airport.name || airport.location || "Unknown airport")}</span>
        ${city ? `<span class="route-city">${escapeHtml(city)}</span>` : ""}
      </span>
    </div>`;
}

/**
 * Where the focused aircraft came from and where it is going. The board only
 * has room for the ORD-LAX code pair, so the readable version lives here.
 */
function renderRoute() {
  const container = el("route");
  const ac = state.aircraft.find((a) => a.hex === state.focusHex);
  if (!ac) {
    container.innerHTML = `<p class="route-none">No aircraft selected.</p>`;
    return;
  }

  // The flight number heads the panel in every case, including when no route
  // resolves - it is the thing you need to look the flight up elsewhere.
  const airlineName = airlineOf(ac);
  const ident = ac.flight || ac.reg || ac.hex.toUpperCase();
  const tracker = trackerLink(ac);
  // rel=noopener because target=_blank otherwise hands the opened page a
  // reference back to this one.
  const callsign = tracker
    ? `<a class="route-callsign link" href="${tracker.url}" target="_blank" rel="noopener noreferrer"
         title="Open ${escapeHtml(ident)} on ${tracker.label}">${escapeHtml(ident)}${EXTERNAL_ICON}</a>`
    : `<span class="route-callsign">${escapeHtml(ident)}</span>`;

  const header = `
    <div class="route-flight">
      ${callsign}
      ${airlineName ? `<span class="route-airline">${escapeHtml(airlineName.name)}</span>` : ""}
      ${
        ac.reg && ac.reg !== ac.flight
          ? `<a class="route-reg link" href="https://flightaware.com/live/flight/${encodeURIComponent(ac.reg)}"
               target="_blank" rel="noopener noreferrer"
               title="Aircraft history for ${escapeHtml(ac.reg)}">${escapeHtml(ac.reg)}</a>`
          : ""
      }
    </div>`;

  const route = usableRoute(ac);
  if (route && route.airports && route.airports.length >= 2) {
    const from = route.airports[0];
    const to = route.airports[route.airports.length - 1];
    // Three or more airports means an intermediate stop the API knows about.
    const via = route.airports.slice(1, -1);
    container.innerHTML =
      header +
      routeLeg("From", from) +
      `<div class="route-rule"></div>` +
      (via.length
        ? `<p class="route-none">via ${via.map((a) => escapeHtml(a.iata || a.icao || "")).join(", ")}</p>`
        : "") +
      routeLeg("To", to) +
      sourceNote(route);
    return;
  }

  // No route table would answer for this aircraft, so fall back to what it is
  // broadcasting about itself. It flew out of somewhere real a few hours ago
  // and, if it is on its way down, it is pointing at somewhere real now.
  const info = state.derived.get(ac.hex) || {};
  if (info.origin || info.destination) {
    // Light aircraft fly circuits, and a training flight that took off and is
    // landing back at the same field is a round trip rather than a broken
    // lookup. Saying it once reads as an answer; saying it twice reads as a
    // bug.
    const sameField =
      info.origin && info.destination &&
      (info.origin.icao || info.origin.iata) === (info.destination.icao || info.destination.iata);

    container.innerHTML =
      header +
      (sameField
        ? routeLeg("Local", info.origin)
        : (info.origin
            ? routeLeg("From", info.origin)
            : `<p class="route-none">Departure airport not identified.</p>`) +
          `<div class="route-rule"></div>` +
          (info.destination
            ? routeLeg("To", info.destination)
            : `<p class="route-none">${escapeHtml(phaseSentence(ac, info))}</p>`)) +
      observedNote(info, sameField);
    return;
  }

  container.innerHTML =
    header + `<p class="route-none">${escapeHtml(phaseSentence(ac, info))}</p>` + lookupNote();
}

/** A broken route backend should still be findable, but it is a note at the
 *  bottom rather than the headline: the panel above it is already answering
 *  the question from the aircraft's own telemetry. */
function lookupNote() {
  if (!state.routeError) return "";
  return `<p class="route-source">Route lookup unavailable — ${escapeHtml(state.routeError)}</p>`;
}

/** Where a resolved route came from. Only worth saying when it is the live
 *  one, since that is the difference between today's flight and a table. */
function sourceNote(route) {
  if (route.source !== "aeroapi") return "";
  const progress = Number.isFinite(route.progress) ? ` · ${Math.round(route.progress)}% flown` : "";
  return `<p class="route-source">Live flight status${escapeHtml(progress)}</p>`;
}

function observedNote(info, sameField = false) {
  if (sameField) {
    return `<p class="route-source">Read from the aircraft: took off here and is descending back to it</p>`;
  }
  const parts = [];
  if (info.origin) parts.push("departure from its track history");
  if (info.destination) {
    parts.push(info.destination.confidence === "high" ? "arrival from its descent" : "likely arrival from its descent");
  }
  return parts.length ? `<p class="route-source">Read from the aircraft: ${parts.join(", ")}</p>` : "";
}

/**
 * What the aircraft is doing, said plainly. This is the last thing the panel
 * has to offer, and it is still a real answer: altitude, trend, and bearing
 * are all being broadcast continuously, so there is no reason to show the
 * reader an apology instead.
 */
function phaseSentence(ac, info) {
  const where = `${fmtDist(ac.dst)} ${compassPoint(ac.dir)}`;
  if (ac.gnd) return `On the ground, ${where}.`;

  const alt = Number.isFinite(ac.alt) ? `${Math.round(ac.alt).toLocaleString()} ft` : null;
  const phase = (info && info.phase) || null;
  const verb =
    phase === "descent" ? "Descending through" :
    phase === "climb" ? "Climbing through" :
    "Level at";

  if (!alt) return `Overhead, ${where}.`;
  return `${verb} ${alt}, ${where}.`;
}

/** Move the highlight without rebuilding the list markup - this runs whenever
 *  the board changes aircraft, including every cycle tick. */
function syncListHighlight() {
  document.querySelectorAll("#list .row").forEach((row) => {
    row.classList.toggle("active", row.dataset.hex === state.focusHex);
  });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
  );
}

// ------------------------------------------------------------------ chrome

function showBanner(message, isError = false) {
  const banner = el("banner");
  banner.textContent = message;
  banner.className = `banner show${isError ? " error" : ""}`;
}

function hideBanner() {
  el("banner").className = "banner";
}

async function requestWakeLock() {
  if (!state.settings.keepAwake || !("wakeLock" in navigator)) return;
  try {
    state.wakeLock = await navigator.wakeLock.request("screen");
    state.wakeLock.addEventListener("release", () => (state.wakeLock = null));
  } catch {
    /* denied or unsupported; the board still works, the screen just sleeps */
  }
}

function bindControls() {
  el("list").addEventListener("click", (event) => {
    const row = event.target.closest("[data-hex]");
    if (!row) return;
    state.pinned = row.dataset.hex;
    state.settings.focus = "pinned";
    saveSettings();
    syncControls();
    // Picking an aircraft out of the list is the request for the real answer.
    requestLiveRoute(state.aircraft.find((ac) => ac.hex === state.pinned));
    // The highlight follows from drawBoard on the next frame.
  });

  el("radar").addEventListener("click", (event) => {
    // Pick whichever blip is nearest the tap, in screen space.
    const rect = el("radar").getBoundingClientRect();
    const tapX = event.clientX - rect.left;
    const tapY = event.clientY - rect.top;
    // Same projection the drawing uses, so taps cannot land on the wrong blip.
    const geometry = {
      cx: rect.width / 2,
      cy: rect.width / 2,
      maxR: rect.width / 2 - 14,
      maxNm: Math.max(1, state.settings.radius),
      up: headingUp(),
    };
    let best = null;
    let bestDist = 28;
    for (const ac of visibleAircraft()) {
      const [x, y] = scopeXY(geometry, ac.dst, ac.dir);
      const d = Math.hypot(x - tapX, y - tapY);
      if (d < bestDist) {
        bestDist = d;
        best = ac;
      }
    }
    if (best) {
      state.pinned = best.hex;
      state.settings.focus = "pinned";
      saveSettings();
      syncControls();
      requestLiveRoute(best);
    }
  });

  el("focusMode").addEventListener("change", (event) => {
    state.settings.focus = event.target.value;
    if (state.settings.focus !== "pinned") state.pinned = null;
    saveSettings();
  });

  el("units").addEventListener("change", (event) => {
    state.settings.units = event.target.value;
    saveSettings();
    renderList();
    drawRadar();
  });

  el("radius").addEventListener("change", (event) => {
    state.settings.radius = Number(event.target.value);
    el("radiusValue").textContent = `${state.settings.radius} NM`;
    saveSettings();
    fetchLandmarks();
    refresh(true);
  });
  el("radius").addEventListener("input", (event) => {
    el("radiusValue").textContent = `${event.target.value} NM`;
  });

  document.querySelectorAll("[data-filter]").forEach((input) => {
    input.addEventListener("change", () => {
      state.settings.filters[input.dataset.filter] = input.checked;
      saveSettings();
      renderList();
    });
  });

  el("showMap").addEventListener("change", (event) => {
    state.settings.showMap = event.target.checked;
    saveSettings();
    if (state.settings.showMap) fetchLandmarks();
  });

  el("keepAwake").addEventListener("change", (event) => {
    state.settings.keepAwake = event.target.checked;
    saveSettings();
    if (state.settings.keepAwake) requestWakeLock();
    else if (state.wakeLock) state.wakeLock.release();
  });

  el("manualBtn").addEventListener("click", () => {
    const raw = prompt("Enter a location as 'latitude, longitude'", state.position ? `${state.position.lat.toFixed(4)}, ${state.position.lon.toFixed(4)}` : "");
    if (!raw) return;
    const [lat, lon] = raw.split(",").map((part) => Number(part.trim()));
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      alert("That does not look like a valid latitude and longitude.");
      return;
    }
    setPosition(lat, lon, null, "manual");
  });

  // This tap is what supplies the user activation iOS requires before it will
  // hand over compass events.
  el("orientBtn").addEventListener("click", () => {
    // Preference on but no events yet means a grant that needs re-requesting;
    // that tap should retry rather than switch the preference off.
    setCompassEnabled(compass.enabled && !compass.live ? true : !compass.enabled);
  });

  el("settingsToggle").addEventListener("click", () => {
    el("settings").classList.toggle("open");
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      requestWakeLock();
      refresh(true);
    }
  });
}

function syncControls() {
  el("focusMode").value = state.settings.focus;
  el("units").value = state.settings.units;
  el("radius").value = String(state.settings.radius);
  el("radiusValue").textContent = `${state.settings.radius} NM`;
  el("keepAwake").checked = state.settings.keepAwake;
  el("showMap").checked = state.settings.showMap;
  document.querySelectorAll("[data-filter]").forEach((input) => {
    input.checked = Boolean(state.settings.filters[input.dataset.filter]);
  });
}

// ------------------------------------------------------------------ start up

/**
 * Re-arm heading-up on launch. On iOS the grant is remembered, but the
 * request can still reject without user activation — in that case keep the
 * preference on and let the button, which does have activation, retry.
 */
async function restoreCompass() {
  if (!state.settings.headingUp || !window.DeviceOrientationEvent) {
    syncCompassButton();
    return;
  }
  compass.enabled = true;
  if (typeof window.DeviceOrientationEvent.requestPermission !== "function") {
    attachOrientation(); // no permission gate on this platform
  } else {
    try {
      if ((await window.DeviceOrientationEvent.requestPermission()) === "granted") {
        attachOrientation();
      }
    } catch {
      /* needs a tap; the button now reads "Waiting for compass…" */
    }
  }
  syncCompassButton();
}

async function loadAirlines() {
  try {
    const res = await fetch("data/airlines.json", { credentials: "same-origin" });
    state.airlines = await res.json();
  } catch {
    state.airlines = {};
  }
}

function tick(now) {
  drawBoard(now);
  drawRadar();
  requestAnimationFrame(tick);
}

async function main() {
  // The token arrives once in the query string; the server hands back a cookie,
  // so strip it from the URL before it ends up in a home-screen bookmark.
  if (new URLSearchParams(location.search).has("k")) {
    history.replaceState(null, "", location.pathname);
  }

  board.instance = new LedBoard(el("board"), BOARD_COLS, BOARD_ROWS);
  board.instance.resize();
  window.addEventListener("resize", () => board.instance.resize());

  syncControls();
  bindControls();
  restoreCompass();
  await loadAirlines();

  if (state.position) {
    state.positionSource = "saved";
    el("locStatus").textContent = "Last known location";
    el("locStatus").className = "chip warn";
    refresh(true);
  }
  startLocation();
  requestWakeLock();

  setInterval(() => refresh(), POLL_MS);
  requestAnimationFrame(tick);

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
}

main();
