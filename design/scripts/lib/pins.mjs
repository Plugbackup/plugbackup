// Geometry of the PlugBackup mark: the letter P drawn with pins of a 3x5 pin header.
// One pin (row 2, column 2) is "connected" and gets the brand colour.
import { loadFont } from './fonts.mjs';

export const PIN = 6;
export const PITCH = 8.5;
export const RX = 1.5;
export const COLS = 3;
export const ROWS = 5;

export const MARK_W = (COLS - 1) * PITCH + PIN; // 23
export const MARK_H = (ROWS - 1) * PITCH + PIN; // 40

export const P_PINS = [
  [0, 0], [0, 1], [0, 2],
  [1, 0], [1, 2],
  [2, 0], [2, 1],
  [3, 0],
  [4, 0],
];
export const LIT_PIN = [2, 2];

const isTaken = (row, col) =>
  [...P_PINS, LIT_PIN].some(([r, c]) => r === row && c === col);

export const GHOST_PINS = Array.from({ length: ROWS * COLS }, (_, i) => [Math.floor(i / COLS), i % COLS]).filter(
  ([row, col]) => !isTaken(row, col),
);

const n = (value) => +value.toFixed(2);

export function pinRect([row, col], attrs = '', { x = 0, y = 0, size = PIN, pitch = PITCH, rx = RX } = {}) {
  return `<rect${attrs} x="${n(x + col * pitch)}" y="${n(y + row * pitch)}" width="${size}" height="${size}" rx="${rx}"/>`;
}

// A pin with a square hole: the "connected" pin in single-colour versions.
export function hollowPin([row, col]) {
  const x = col * PITCH;
  const y = row * PITCH;
  const r = RX;
  const s = PIN;
  const hole = 2.4;
  const o = (s - hole) / 2;
  const side = s - 2 * r;
  return (
    `<path fill-rule="evenodd" d="M${n(x + r)} ${n(y)}h${side}a${r} ${r} 0 0 1 ${r} ${r}v${side}` +
    `a${r} ${r} 0 0 1 -${r} ${r}h-${side}a${r} ${r} 0 0 1 -${r} -${r}v-${side}a${r} ${r} 0 0 1 ${r} -${r}Z` +
    `M${n(x + o)} ${n(y + o)}h${hole}v${hole}h-${hole}Z"/>`
  );
}

// The mark in its own units (23 x 40), without an outer <svg>.
export function markGroup({ ink, lit, ghost = 0, hollow = false, transform = '' }) {
  const ghosts = ghost ? `<g fill="${ink}" opacity="${ghost}">${GHOST_PINS.map((p) => pinRect(p)).join('')}</g>` : '';
  const pins = `<g fill="${ink}">${P_PINS.map((p) => pinRect(p)).join('')}</g>`;
  const litPin = hollow ? `<g fill="${ink}">${hollowPin(LIT_PIN)}</g>` : `<g fill="${lit}">${pinRect(LIT_PIN)}</g>`;
  const attr = transform ? ` transform="${transform}"` : '';
  return `<g${attr}>${ghosts}${pins}${litPin}</g>`;
}

// The mark for inline use in HTML: pins carry classes from animations.css and take their
// colours from CSS (currentColor), so it follows the theme.
export function markInline() {
  const sockets = GHOST_PINS.map((p) => pinRect(p, ' class="pb-logo__socket"')).join('');
  const pins = P_PINS.map((p) => pinRect(p, ' class="pb-logo__pin"')).join('');
  const lit = pinRect(LIT_PIN, ' class="pb-logo__lit"');
  const pulse = pinRect(LIT_PIN, ' class="pb-logo__pulse"');
  return `${sockets}<g class="pb-logo__plug">${pins}${lit}</g>${pulse}`;
}

// Wordmark ----------------------------------------------------------------

const WORD = [
  ['Plug', 600],
  ['Backup', 400],
];
const TRACKING = -0.03;

// Glyph outline scaled to `size` and moved to (x, baseline). The font's own getPath()
// is not used: opentype.js 2.0 returns NaN control points for some sizes.
function glyphOutline(font, char, size, x, baseline) {
  const scale = size / font.unitsPerEm;
  const px = (v) => x + v * scale;
  const py = (v) => baseline - v * scale;
  const points = [];
  const d = font
    .charToGlyph(char)
    .path.commands.map((c) => {
      if (c.type === 'Z') return 'Z';
      const coords = [];
      if (c.type === 'Q' || c.type === 'C') coords.push(px(c.x1), py(c.y1));
      if (c.type === 'C') coords.push(px(c.x2), py(c.y2));
      coords.push(px(c.x), py(c.y));
      for (let i = 0; i < coords.length; i += 2) points.push([coords[i], coords[i + 1]]);
      return c.type + coords.map(n).join(' ');
    })
    .join('');
  return { d, points };
}

// Wordmark as outlines in IBM Plex Mono: "Plug" is semibold, "Backup" is regular.
export async function wordmarkPath({ size, x = 0, baseline = 0 }) {
  let cursor = x;
  let d = '';
  let capHeight = 0;
  const box = { x1: Infinity, y1: Infinity, x2: -Infinity, y2: -Infinity };
  for (const [text, weight] of WORD) {
    const font = await loadFont('IBM Plex Mono', weight);
    capHeight = (font.tables.os2.sCapHeight / font.unitsPerEm) * size;
    for (const char of text) {
      const glyph = glyphOutline(font, char, size, cursor, baseline);
      d += glyph.d;
      for (const [px, py] of glyph.points) {
        box.x1 = Math.min(box.x1, px);
        box.y1 = Math.min(box.y1, py);
        box.x2 = Math.max(box.x2, px);
        box.y2 = Math.max(box.y2, py);
      }
      cursor += (font.charToGlyph(char).advanceWidth / font.unitsPerEm) * size + TRACKING * size;
    }
  }
  return { d, box, capHeight, width: cursor - x - TRACKING * size };
}

// Mark + wordmark in one row. Own units: height is MARK_H.
async function lockupParts() {
  const size = 28;
  const gap = 15;
  const { capHeight } = await wordmarkPath({ size });
  const baseline = MARK_H / 2 + capHeight / 2;
  const word = await wordmarkPath({ size, x: MARK_W + gap, baseline });
  return { width: n(MARK_W + gap + word.width), height: MARK_H, wordPath: word.d };
}

export async function lockup({ ink, lit, ghost = 0, hollow = false }) {
  const { width, height, wordPath } = await lockupParts();
  return {
    width,
    height,
    body: `${markGroup({ ink, lit, ghost, hollow })}<path fill="${ink}" d="${wordPath}"/>`,
  };
}

export async function lockupInline() {
  const { width, height, wordPath } = await lockupParts();
  return { width, height, body: `${markInline()}<path class="pb-logo__word" d="${wordPath}"/>` };
}
