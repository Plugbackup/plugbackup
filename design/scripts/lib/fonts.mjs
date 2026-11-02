// Loads IBM Plex from node_modules for the build scripts.
// The packages ship WOFF, while resvg needs TTF/OTF: WOFF is just zlib-compressed
// sfnt tables, so it is unpacked here and cached under node_modules/.cache.
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { inflateSync } from 'node:zlib';
import opentype from 'opentype.js';
import { path } from './common.mjs';

const packages = {
  'IBM Plex Sans': { pkg: '@ibm/plex-sans', prefix: 'IBMPlexSans' },
  'IBM Plex Mono': { pkg: '@ibm/plex-mono', prefix: 'IBMPlexMono' },
};

export const styles = { 400: 'Regular', 500: 'Medium', 600: 'SemiBold' };

export function woffToSfnt(woff) {
  if (woff.toString('latin1', 0, 4) !== 'wOFF') throw new Error('Not a WOFF file');
  const flavor = woff.readUInt32BE(4);
  const count = woff.readUInt16BE(12);
  const tables = [];
  for (let i = 0; i < count; i++) {
    const at = 44 + i * 20;
    const tag = woff.toString('latin1', at, at + 4);
    const offset = woff.readUInt32BE(at + 4);
    const compLength = woff.readUInt32BE(at + 8);
    const origLength = woff.readUInt32BE(at + 12);
    const checksum = woff.readUInt32BE(at + 16);
    const raw = woff.subarray(offset, offset + compLength);
    const data = compLength < origLength ? inflateSync(raw) : raw;
    if (data.length !== origLength) throw new Error(`Bad table length for ${tag}`);
    tables.push({ tag, checksum, data });
  }

  const pad4 = (n) => (n + 3) & ~3;
  const headerSize = 12 + count * 16;
  const total = tables.reduce((sum, t) => sum + pad4(t.data.length), headerSize);
  const out = Buffer.alloc(total);
  const entrySelector = Math.floor(Math.log2(count));
  const searchRange = 2 ** entrySelector * 16;
  out.writeUInt32BE(flavor, 0);
  out.writeUInt16BE(count, 4);
  out.writeUInt16BE(searchRange, 6);
  out.writeUInt16BE(entrySelector, 8);
  out.writeUInt16BE(count * 16 - searchRange, 10);

  let offset = headerSize;
  tables.forEach((table, i) => {
    const at = 12 + i * 16;
    out.write(table.tag, at, 'latin1');
    out.writeUInt32BE(table.checksum, at + 4);
    out.writeUInt32BE(offset, at + 8);
    out.writeUInt32BE(table.data.length, at + 12);
    table.data.copy(out, offset);
    offset += pad4(table.data.length);
  });
  return out;
}

async function sfntFile(family, weight) {
  const { pkg, prefix } = packages[family];
  const name = `${prefix}-${styles[weight]}`;
  const cached = path('node_modules', '.cache', 'plugbackup-design', 'fonts', `${name}.ttf`);
  if (!existsSync(cached)) {
    const source = path('node_modules', pkg, 'fonts', 'complete', 'woff', `${name}.woff`);
    if (!existsSync(source)) {
      throw new Error(`Font ${name} not found. Run "npm install" in design/ first.`);
    }
    await mkdir(dirname(cached), { recursive: true });
    await writeFile(cached, woffToSfnt(await readFile(source)));
  }
  return cached;
}

// Files for resvg: every weight of both families.
export async function fontFiles() {
  const files = [];
  for (const family of Object.keys(packages)) {
    for (const weight of Object.keys(styles)) files.push(await sfntFile(family, Number(weight)));
  }
  return files;
}

const loaded = new Map();

export async function loadFont(family, weight) {
  const key = `${family}:${weight}`;
  if (!loaded.has(key)) {
    const buffer = await readFile(await sfntFile(family, weight));
    const arrayBuffer = buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
    loaded.set(key, opentype.parse(arrayBuffer));
  }
  return loaded.get(key);
}
