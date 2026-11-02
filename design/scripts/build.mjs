// Entry point: node scripts/build.mjs [all | tokens | logo | graphics | showcase | fonts | check]
import { readFile } from 'node:fs/promises';
import { buildGraphics } from './build-graphics.mjs';
import { buildLogo } from './build-logo.mjs';
import { buildShowcase } from './build-showcase.mjs';
import { buildTokens } from './build-tokens.mjs';
import { path } from './lib/common.mjs';
import { buildCss, contrastMarkdown } from './lib/tokens.mjs';

async function check() {
  const { tokens, report } = await buildTokens({ check: true });
  const stale = [];
  const expected = [
    ['tokens.css', buildCss(tokens)],
    ['contrast.md', contrastMarkdown(report)],
  ];
  for (const [file, content] of expected) {
    const current = await readFile(path(file), 'utf8').catch(() => '');
    if (current !== content.replace(/\s*$/, '\n')) stale.push(file);
  }
  if (stale.length) {
    throw new Error(`Out of date: ${stale.join(', ')}. Run "npm run build".`);
  }
  console.log('check: contrast passes AA, tokens.css and contrast.md match tokens.json');
}

const steps = {
  tokens: () => buildTokens(),
  logo: buildLogo,
  graphics: buildGraphics,
  showcase: buildShowcase,
  fonts: () => import('./sync-fonts.mjs'),
  check,
};

const target = process.argv[2] ?? 'all';
const run = target === 'all' ? ['tokens', 'logo', 'graphics', 'showcase'] : [target];

try {
  for (const name of run) {
    if (!steps[name]) throw new Error(`Unknown step "${name}". Available: all, ${Object.keys(steps).join(', ')}`);
    await steps[name]();
  }
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
