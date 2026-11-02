// Tiny template engine for the SVG sources in graphics/src.
//
//   {{path.to.value}}         value from the context (text is XML-escaped)
//   {{@mark:on-dark}}         the P mark in its own units (23 x 40)
//   {{@wordmark:on-dark}}     "PlugBackup" outlines, 28 units font size, baseline at y = 0
//   {{@lockup:on-dark}}       mark + wordmark in a row (height 40)
//
// A <text> whose only content is {{key}} may hold several lines separated by "\n".
// Optional attributes of such <text>: data-lh (line height factor, default 1.15)
// and data-max (a soft limit of characters per line: longer lines print a warning).
import { escapeXml } from './common.mjs';
import { lockup, markGroup, wordmarkPath } from './pins.mjs';
import { logoVariants } from './variants.mjs';

const round = (value) => +value.toFixed(2);

export const lookup = (ctx, key) => key.split('.').reduce((value, part) => (value == null ? value : value[part]), ctx);

function need(ctx, key, name) {
  const value = lookup(ctx, key);
  if (value == null) throw new Error(`${name}: no value for {{${key}}}`);
  return String(value);
}

// Fills {{...}} inside the strings of a JSON value (used for data/graphics.json).
export function resolveStrings(value, ctx, name) {
  if (typeof value === 'string') return value.replace(/\{\{([\w.-]+)\}\}/g, (_, key) => need(ctx, key, name));
  if (Array.isArray(value)) return value.map((item) => resolveStrings(item, ctx, name));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, resolveStrings(v, ctx, name)]));
  }
  return value;
}

export async function fillTemplate(source, ctx, { name, warn = console.warn }) {
  const variants = await logoVariants();

  let out = source.replace(/<text([^>]*)>\{\{([\w.-]+)\}\}<\/text>/g, (_, attrs, key) => {
    const lines = need(ctx, key, name).split('\n');
    const x = attrs.match(/\bx="([^"]+)"/)?.[1];
    const size = Number(attrs.match(/font-size="([^"]+)"/)?.[1]);
    const factor = Number(attrs.match(/data-lh="([^"]+)"/)?.[1] ?? 1.15);
    const max = Number(attrs.match(/data-max="([^"]+)"/)?.[1] ?? 0);
    if (max) {
      for (const line of lines) {
        if (line.length > max) warn(`${name}: line "${line}" is longer than ${max} characters and may not fit`);
      }
    }
    const clean = attrs.replace(/\sdata-(lh|max)="[^"]*"/g, '');
    const tspans = lines
      .map((line, i) =>
        i === 0 ? escapeXml(line) : `<tspan x="${x}" dy="${round(size * factor)}">${escapeXml(line)}</tspan>`,
      )
      .join('');
    return `<text${clean}>${tspans}</text>`;
  });

  const logos = new Map();
  for (const [, kind, variant] of out.matchAll(/\{\{@(mark|wordmark|lockup):([\w-]+)\}\}/g)) {
    const key = `${kind}:${variant}`;
    if (logos.has(key)) continue;
    const options = variants[variant];
    if (!options) throw new Error(`${name}: unknown logo variant "${variant}"`);
    if (kind === 'mark') logos.set(key, markGroup(options));
    if (kind === 'wordmark') logos.set(key, `<path fill="${options.ink}" d="${(await wordmarkPath({ size: 28 })).d}"/>`);
    if (kind === 'lockup') logos.set(key, (await lockup(options)).body);
  }
  out = out.replace(/\{\{@(mark|wordmark|lockup):([\w-]+)\}\}/g, (_, kind, variant) => logos.get(`${kind}:${variant}`));

  return out.replace(/\{\{([\w.-]+)\}\}/g, (_, key) => escapeXml(need(ctx, key, name)));
}
