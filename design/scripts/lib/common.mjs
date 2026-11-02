import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

export const path = (...parts) => join(root, ...parts);

export async function readJson(...parts) {
  return JSON.parse(await readFile(path(...parts), 'utf8'));
}

// Writes text without trailing spaces and with exactly one trailing newline.
// Returns true if the file changed.
export async function writeText(file, text) {
  const next = text.replace(/[ \t]+$/gm, '').replace(/\s*$/, '\n');
  let prev = null;
  try {
    prev = await readFile(file, 'utf8');
  } catch {
    // new file
  }
  if (prev === next) return false;
  await mkdir(dirname(file), { recursive: true });
  await writeFile(file, next);
  return true;
}

export async function writeBinary(file, data) {
  await mkdir(dirname(file), { recursive: true });
  await writeFile(file, data);
}

export function escapeXml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Russian typography: keeps a number glued to its unit and short words to the next one.
export const NBSP = ' ';
