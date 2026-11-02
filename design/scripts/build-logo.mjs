import { pathToFileURL } from 'node:url';
import { path, writeText } from './lib/common.mjs';
import { MARK_H, MARK_W, lockup, markGroup, wordmarkPath } from './lib/pins.mjs';
import { loadTokens } from './lib/tokens.mjs';
import { logoVariants } from './lib/variants.mjs';

const svg = ({ width, height, viewBox, body }) =>
  `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="${viewBox}" role="img" aria-label="PlugBackup">
  <title>PlugBackup</title>
  ${body}
</svg>`;

export async function buildLogo() {
  const { color } = await loadTokens();
  const variants = await logoVariants();
  const files = [];
  const scale = 4;

  for (const [name, options] of Object.entries(variants)) {
    files.push([
      `mark-${name}.svg`,
      svg({
        width: MARK_W * scale,
        height: MARK_H * scale,
        viewBox: `0 0 ${MARK_W} ${MARK_H}`,
        body: markGroup(options),
      }),
    ]);

    const word = await wordmarkPath({ size: 100 });
    const { x1, y1, x2, y2 } = word.box;
    files.push([
      `wordmark-${name}.svg`,
      svg({
        width: Math.round(x2 - x1),
        height: Math.round(y2 - y1),
        viewBox: `${x1.toFixed(2)} ${y1.toFixed(2)} ${(x2 - x1).toFixed(2)} ${(y2 - y1).toFixed(2)}`,
        body: `<path fill="${options.ink}" d="${word.d}"/>`,
      }),
    ]);

    const row = await lockup(options);
    files.push([
      `logo-${name}.svg`,
      svg({
        width: Math.round(row.width * scale),
        height: row.height * scale,
        viewBox: `0 0 ${row.width} ${row.height}`,
        body: row.body,
      }),
    ]);
  }

  const circles = {
    graphite: { bg: color.palette.neutral[900], ink: color.palette.neutral[100], ghost: 0.16 },
    chalk: { bg: color.palette.neutral[100], ink: color.palette.neutral[900], ghost: 0.14 },
  };
  for (const [name, { bg, ink, ghost }] of Object.entries(circles)) {
    const offset = `translate(${(64 - MARK_W) / 2} ${(64 - MARK_H) / 2})`;
    files.push([
      `mark-circle-${name}.svg`,
      svg({
        width: 256,
        height: 256,
        viewBox: '0 0 64 64',
        body: `<circle cx="32" cy="32" r="32" fill="${bg}"/>\n  ${markGroup({ ink, lit: color.brand, ghost, transform: offset })}`,
      }),
    ]);
  }

  let changed = 0;
  for (const [file, content] of files) {
    if (await writeText(path('logo', file), content)) changed++;
  }
  console.log(`logo: ${files.length} files in logo/ (${changed} changed)`);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  buildLogo().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
