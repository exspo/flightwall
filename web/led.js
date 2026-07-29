// A dot-matrix LED panel drawn on a canvas.
//
// The font is hand-coded rather than loaded, so the board renders identically
// offline and on a cold home-screen launch with no network. Each glyph is five
// columns of seven pixels, one byte per column, bit 0 = top row.

const FONT = {
  " ": [0x00, 0x00, 0x00, 0x00, 0x00],
  "!": [0x00, 0x00, 0x5f, 0x00, 0x00],
  '"': [0x00, 0x07, 0x00, 0x07, 0x00],
  "#": [0x14, 0x7f, 0x14, 0x7f, 0x14],
  "$": [0x24, 0x2a, 0x7f, 0x2a, 0x12],
  "%": [0x23, 0x13, 0x08, 0x64, 0x62],
  "&": [0x36, 0x49, 0x55, 0x22, 0x50],
  "'": [0x00, 0x05, 0x03, 0x00, 0x00],
  "(": [0x00, 0x1c, 0x22, 0x41, 0x00],
  ")": [0x00, 0x41, 0x22, 0x1c, 0x00],
  "*": [0x14, 0x08, 0x3e, 0x08, 0x14],
  "+": [0x08, 0x08, 0x3e, 0x08, 0x08],
  ",": [0x00, 0x50, 0x30, 0x00, 0x00],
  "-": [0x08, 0x08, 0x08, 0x08, 0x08],
  ".": [0x00, 0x60, 0x60, 0x00, 0x00],
  "/": [0x20, 0x10, 0x08, 0x04, 0x02],
  "0": [0x3e, 0x51, 0x49, 0x45, 0x3e],
  "1": [0x00, 0x42, 0x7f, 0x40, 0x00],
  "2": [0x42, 0x61, 0x51, 0x49, 0x46],
  "3": [0x21, 0x41, 0x45, 0x4b, 0x31],
  "4": [0x18, 0x14, 0x12, 0x7f, 0x10],
  "5": [0x27, 0x45, 0x45, 0x45, 0x39],
  "6": [0x3c, 0x4a, 0x49, 0x49, 0x30],
  "7": [0x01, 0x71, 0x09, 0x05, 0x03],
  "8": [0x36, 0x49, 0x49, 0x49, 0x36],
  "9": [0x06, 0x49, 0x49, 0x29, 0x1e],
  ":": [0x00, 0x36, 0x36, 0x00, 0x00],
  ";": [0x00, 0x56, 0x36, 0x00, 0x00],
  "<": [0x08, 0x14, 0x22, 0x41, 0x00],
  "=": [0x14, 0x14, 0x14, 0x14, 0x14],
  ">": [0x00, 0x41, 0x22, 0x14, 0x08],
  "?": [0x02, 0x01, 0x51, 0x09, 0x06],
  "@": [0x32, 0x49, 0x79, 0x41, 0x3e],
  A: [0x7e, 0x11, 0x11, 0x11, 0x7e],
  B: [0x7f, 0x49, 0x49, 0x49, 0x36],
  C: [0x3e, 0x41, 0x41, 0x41, 0x22],
  D: [0x7f, 0x41, 0x41, 0x22, 0x1c],
  E: [0x7f, 0x49, 0x49, 0x49, 0x41],
  F: [0x7f, 0x09, 0x09, 0x01, 0x01],
  G: [0x3e, 0x41, 0x49, 0x49, 0x7a],
  H: [0x7f, 0x08, 0x08, 0x08, 0x7f],
  I: [0x00, 0x41, 0x7f, 0x41, 0x00],
  J: [0x20, 0x40, 0x41, 0x3f, 0x01],
  K: [0x7f, 0x08, 0x14, 0x22, 0x41],
  L: [0x7f, 0x40, 0x40, 0x40, 0x40],
  M: [0x7f, 0x02, 0x04, 0x02, 0x7f],
  N: [0x7f, 0x04, 0x08, 0x10, 0x7f],
  O: [0x3e, 0x41, 0x41, 0x41, 0x3e],
  P: [0x7f, 0x09, 0x09, 0x09, 0x06],
  Q: [0x3e, 0x41, 0x51, 0x21, 0x5e],
  R: [0x7f, 0x09, 0x19, 0x29, 0x46],
  S: [0x46, 0x49, 0x49, 0x49, 0x31],
  T: [0x01, 0x01, 0x7f, 0x01, 0x01],
  U: [0x3f, 0x40, 0x40, 0x40, 0x3f],
  V: [0x1f, 0x20, 0x40, 0x20, 0x1f],
  W: [0x7f, 0x20, 0x18, 0x20, 0x7f],
  X: [0x63, 0x14, 0x08, 0x14, 0x63],
  Y: [0x03, 0x04, 0x78, 0x04, 0x03],
  Z: [0x61, 0x51, 0x49, 0x45, 0x43],
  "[": [0x00, 0x7f, 0x41, 0x41, 0x00],
  "\\": [0x02, 0x04, 0x08, 0x10, 0x20],
  "]": [0x00, 0x41, 0x41, 0x7f, 0x00],
  "^": [0x04, 0x02, 0x01, 0x02, 0x04],
  _: [0x40, 0x40, 0x40, 0x40, 0x40],
  "°": [0x00, 0x07, 0x05, 0x07, 0x00], // degree
  "↑": [0x04, 0x02, 0x7f, 0x02, 0x04], // climbing
  "↓": [0x10, 0x20, 0x7f, 0x20, 0x10], // descending
  "•": [0x00, 0x18, 0x18, 0x00, 0x00], // level
};

const GLYPH_W = 5;
const GLYPH_H = 7;
const ADVANCE = 6; // glyph plus one column of gap

// Palette index 0 is the unlit dot. Everything else is a lit colour.
export const PALETTE = [
  "#141821", // 0 off
  "#f2f6ff", // 1 white
  "#ffb225", // 2 amber
  "#35e0d0", // 3 cyan
  "#5df08a", // 4 green
  "#ff4d5e", // 5 red
  "#7aa2ff", // 6 blue
  "#c98bff", // 7 violet
  "#5a6478", // 8 dim
];

export const C = {
  OFF: 0, WHITE: 1, AMBER: 2, CYAN: 3, GREEN: 4, RED: 5, BLUE: 6, VIOLET: 7, DIM: 8,
};

export function textWidth(str) {
  return str.length === 0 ? 0 : str.length * ADVANCE - 1;
}

/** A cols x rows grid of colour indices, with drawing helpers. */
export class Frame {
  constructor(cols, rows) {
    this.cols = cols;
    this.rows = rows;
    this.buf = new Uint8Array(cols * rows);
  }

  clear() {
    this.buf.fill(0);
  }

  set(x, y, color) {
    x |= 0;
    y |= 0;
    if (x < 0 || y < 0 || x >= this.cols || y >= this.rows) return;
    this.buf[y * this.cols + x] = color;
  }

  /**
   * Draw text with optional clipping to a window, which is what makes
   * horizontal scrolling possible for lines that overflow the panel.
   */
  text(str, x, y, color, clip) {
    const left = clip ? clip.x : 0;
    const right = clip ? clip.x + clip.w : this.cols;
    let cursor = x;
    for (const raw of String(str)) {
      const ch = FONT[raw] ? raw : raw.toUpperCase();
      const glyph = FONT[ch] || FONT["?"];
      // Skip glyphs entirely outside the window rather than per-pixel testing.
      if (cursor + GLYPH_W >= left && cursor < right) {
        for (let gx = 0; gx < GLYPH_W; gx++) {
          const px = cursor + gx;
          if (px < left || px >= right) continue;
          const bits = glyph[gx];
          for (let gy = 0; gy < GLYPH_H; gy++) {
            if (bits & (1 << gy)) this.set(px, y + gy, color);
          }
        }
      }
      cursor += ADVANCE;
    }
    return cursor - x;
  }

  hline(x0, x1, y, color) {
    for (let x = x0; x <= x1; x++) this.set(x, y, color);
  }

  circle(cx, cy, r, color) {
    // Midpoint circle, drawn as an outline.
    let x = r;
    let y = 0;
    let err = 1 - r;
    while (x >= y) {
      for (const [dx, dy] of [[x, y], [y, x], [-x, y], [-y, x], [-x, -y], [-y, -x], [x, -y], [y, -x]]) {
        this.set(cx + dx, cy + dy, color);
      }
      y++;
      if (err < 0) err += 2 * y + 1;
      else { x--; err += 2 * (y - x) + 1; }
    }
  }

  line(x0, y0, x1, y1, color) {
    let dx = Math.abs(x1 - x0);
    let dy = -Math.abs(y1 - y0);
    const sx = x0 < x1 ? 1 : -1;
    const sy = y0 < y1 ? 1 : -1;
    let err = dx + dy;
    for (;;) {
      this.set(x0, y0, color);
      if (x0 === x1 && y0 === y1) break;
      const e2 = 2 * err;
      if (e2 >= dy) { err += dy; x0 += sx; }
      if (e2 <= dx) { err += dx; y0 += sy; }
    }
  }
}

/**
 * Paints a Frame onto a canvas as physical-looking LEDs.
 *
 * The unlit grid never changes, so it is rendered once into an offscreen
 * canvas and blitted; only lit dots are drawn per frame. That keeps a
 * 128x44 panel cheap enough to animate on a phone.
 */
export class LedBoard {
  constructor(canvas, cols, rows) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: false });
    this.cols = cols;
    this.rows = rows;
    this.pitch = 0;
    this.bg = document.createElement("canvas");
    this.bgCtx = this.bg.getContext("2d");
    this.lastW = 0;
    this.lastH = 0;
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    const cssW = this.canvas.clientWidth;
    if (!cssW) return false;
    const pitch = Math.max(2, Math.floor((cssW * dpr) / this.cols));
    const w = pitch * this.cols;
    const h = pitch * this.rows;
    if (w === this.lastW && h === this.lastH) return false;

    this.pitch = pitch;
    this.lastW = w;
    this.lastH = h;
    this.canvas.width = w;
    this.canvas.height = h;
    this.canvas.style.height = `${h / dpr}px`;
    this.bg.width = w;
    this.bg.height = h;

    const r = Math.max(1, pitch * 0.38);
    this.dotRadius = r;
    this.bgCtx.fillStyle = "#05070c";
    this.bgCtx.fillRect(0, 0, w, h);
    this.bgCtx.fillStyle = PALETTE[C.OFF];
    for (let y = 0; y < this.rows; y++) {
      for (let x = 0; x < this.cols; x++) {
        this.bgCtx.beginPath();
        this.bgCtx.arc(x * pitch + pitch / 2, y * pitch + pitch / 2, r, 0, Math.PI * 2);
        this.bgCtx.fill();
      }
    }
    return true;
  }

  render(frame) {
    if (!this.pitch) this.resize();
    if (!this.pitch) return;
    const { ctx, pitch } = this;
    ctx.drawImage(this.bg, 0, 0);

    // Batch by colour so we set fillStyle a handful of times, not thousands.
    const byColor = new Map();
    for (let i = 0; i < frame.buf.length; i++) {
      const color = frame.buf[i];
      if (!color) continue;
      let list = byColor.get(color);
      if (!list) byColor.set(color, (list = []));
      list.push(i);
    }

    ctx.shadowBlur = pitch * 0.9;
    for (const [color, indices] of byColor) {
      const hex = PALETTE[color] || PALETTE[C.WHITE];
      ctx.fillStyle = hex;
      ctx.shadowColor = hex;
      ctx.beginPath();
      for (const i of indices) {
        const x = i % this.cols;
        const y = (i / this.cols) | 0;
        ctx.moveTo(x * pitch + pitch / 2 + this.dotRadius, y * pitch + pitch / 2);
        ctx.arc(x * pitch + pitch / 2, y * pitch + pitch / 2, this.dotRadius, 0, Math.PI * 2);
      }
      ctx.fill();
    }
    ctx.shadowBlur = 0;
  }
}
