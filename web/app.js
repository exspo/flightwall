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
};

const state = {
  settings: loadSettings(),
  position: loadLastPosition(),
  positionSource: null,
  aircraft: [],
  routes: new Map(),
  airlines: {},
  pinned: null,
  focusHex: null,
  cycleIndex: 0,
  lastCycle: 0,
  lastFetch: 0,
  lastRouteFetch: 0,
  fetching: false,
  error: null,
  source: null,
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

function subtitleOf(ac) {
  const route = state.routes.get(ac.flight);
  if (route && route.route) return route.route.toUpperCase();
  if (ac.flight) return ac.flight;
  return ac.reg || "NO CALLSIGN";
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
    state.error = null;
    state.lastFetch = Date.now();
    hideBanner();
    fetchRoutes();
    renderList();
  } catch (err) {
    state.error = err.message;
    showBanner(`Could not reach the aircraft feed: ${err.message}`, true);
  } finally {
    state.fetching = false;
  }
}

async function fetchRoutes() {
  if (Date.now() - state.lastRouteFetch < ROUTE_POLL_MS) return;
  const wanted = state.aircraft
    .filter((ac) => airlineOf(ac) && !state.routes.has(ac.flight))
    .slice(0, 100)
    .map((ac) => ({ callsign: ac.flight, lat: ac.lat, lng: ac.lon }));
  if (!wanted.length) return;
  state.lastRouteFetch = Date.now();
  try {
    const res = await fetch("api/routes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ planes: wanted }),
    });
    if (!res.ok) return;
    const { routes } = await res.json();
    for (const [callsign, route] of Object.entries(routes || {})) {
      state.routes.set(callsign, route);
    }
    renderList();
  } catch {
    /* routes are decoration; never let them break the board */
  }
}

// Classes the server can emit that have no checkbox of their own ride along
// with the closest one that does.
const FILTER_ALIAS = { drone: "general", lighter: "general", unknown: "general" };

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

  const route = state.routes.get(ac.flight);
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

  return [
    { text: titleOf(ac), color: C.WHITE, x: TEXT_X, clip: TEXT_CLIP },
    { text: subtitleOf(ac), color: airline && route ? C.AMBER : C.DIM, x: TEXT_X, clip: TEXT_CLIP },
    { text: typeLabel(ac), color, x: TEXT_X, clip: TEXT_CLIP },
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

  // How far the whole scope is turned. Every bearing below is drawn relative
  // to this, so north-up and heading-up share one code path.
  const up = headingUp();

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
    const scale = Math.min(1, Math.sqrt(Math.min(ac.dst, maxNm) / maxNm));
    const rad = ((ac.dir - up - 90) * Math.PI) / 180;
    const x = cx + Math.cos(rad) * maxR * scale;
    const y = cy + Math.sin(rad) * maxR * scale;
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
    container.innerHTML = `<p class="empty">Nothing in range right now. Widen the radius or loosen the filters.</p>`;
    return;
  }

  container.innerHTML = list
    .slice(0, 60)
    .map((ac) => {
      const route = state.routes.get(ac.flight);
      const color = PALETTE[colorOf(ac)];
      const title = titleOf(ac);
      const ident = route && route.route ? route.route : ac.flight || ac.reg;
      // For a private aircraft the title is already the tail number, so
      // repeating it as the identifier just wastes the line.
      const sub = [ident === title ? null : ident, typeLabel(ac)].filter(Boolean).join(" · ");
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
    // The highlight follows from drawBoard on the next frame.
  });

  el("radar").addEventListener("click", (event) => {
    // Pick whichever blip is nearest the tap, in screen space.
    const rect = el("radar").getBoundingClientRect();
    const tapX = event.clientX - rect.left;
    const tapY = event.clientY - rect.top;
    const cx = rect.width / 2;
    const maxR = rect.width / 2 - 14;
    const maxNm = Math.max(1, state.settings.radius);
    let best = null;
    let bestDist = 28;
    const up = headingUp(); // must match drawRadar, or taps land on the wrong blip
    for (const ac of visibleAircraft()) {
      const scale = Math.min(1, Math.sqrt(Math.min(ac.dst, maxNm) / maxNm));
      const rad = ((ac.dir - up - 90) * Math.PI) / 180;
      const x = cx + Math.cos(rad) * maxR * scale;
      const y = cx + Math.sin(rad) * maxR * scale;
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
