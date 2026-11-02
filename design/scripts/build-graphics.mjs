// Renders graphics/src/*.svg (templates) into graphics/svg/*.svg and graphics/png/*.png.
// Texts come from data/graphics.json and data/plans.json, colours from tokens.json.
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { Resvg } from '@resvg/resvg-js';
import { path, readJson, writeBinary, writeText } from './lib/common.mjs';
import { fontFiles } from './lib/fonts.mjs';
import { loadPlans, planContext } from './lib/plans.mjs';
import { fillTemplate, resolveStrings } from './lib/template.mjs';
import { loadTokens } from './lib/tokens.mjs';

// name: source file in graphics/src. png: files to export and their width in px.
export const jobs = [
  { name: 'avatar', png: [{ file: 'avatar', width: 640 }] },
  { name: 'bot-description', png: [{ file: 'bot-description', width: 640 }] },
  { name: 'banner-announce', png: [{ file: 'banner-announce', width: 1280 }] },
  { name: 'banner-update', png: [{ file: 'banner-update', width: 1280 }] },
  { name: 'banner-plan', png: [{ file: 'banner-plan', width: 1280 }] },
  { name: 'og-image', png: [{ file: 'og-image', width: 1200 }] },
  { name: 'favicon', png: [{ file: 'favicon-32', width: 32 }] },
  { name: 'apple-touch-icon', png: [{ file: 'apple-touch-icon', width: 180 }] },
];

export async function buildGraphics() {
  const tokens = await loadTokens();
  const texts = await readJson('data', 'graphics.json');
  const base = {
    ...planContext(await loadPlans()),
    color: { brand: tokens.color.brand, ...tokens.color.palette },
  };
  const fonts = await fontFiles();
  let svgCount = 0;
  let pngCount = 0;

  for (const job of jobs) {
    const { plan: planId, ...raw } = texts[job.name] ?? {};
    const scope = { ...base, plan: planId ? base.plans[planId] : undefined };
    if (planId && !scope.plan) throw new Error(`${job.name}: unknown plan "${planId}" in data/graphics.json`);
    const ctx = { ...scope, ...resolveStrings(raw, scope, job.name) };

    const source = await readFile(path('graphics', 'src', `${job.name}.svg`), 'utf8');
    const svg = (await fillTemplate(source, ctx, { name: job.name })).replace(/\s*$/, '\n');
    await writeText(path('graphics', 'svg', `${job.name}.svg`), svg);
    svgCount++;

    for (const { file, width } of job.png) {
      const resvg = new Resvg(svg, {
        fitTo: { mode: 'width', value: width },
        font: { fontFiles: fonts, loadSystemFonts: false, defaultFontFamily: 'IBM Plex Sans' },
      });
      await writeBinary(path('graphics', 'png', `${file}.png`), resvg.render().asPng());
      pngCount++;
    }
  }
  console.log(`graphics: ${svgCount} svg in graphics/svg, ${pngCount} png in graphics/png`);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  buildGraphics().catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
}
