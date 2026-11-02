import { pathToFileURL } from 'node:url';
import { path, writeText } from './lib/common.mjs';
import { buildCss, contrastFailures, contrastMarkdown, contrastRows, loadTokens } from './lib/tokens.mjs';

export async function buildTokens({ check = false } = {}) {
  const tokens = await loadTokens();
  const report = contrastRows(tokens);
  const failures = contrastFailures(report);
  if (failures.length) {
    throw new Error(`Contrast below WCAG AA:\n  ${failures.join('\n  ')}`);
  }
  if (check) {
    return { tokens, report };
  }
  await writeText(path('tokens.css'), buildCss(tokens));
  await writeText(path('contrast.md'), contrastMarkdown(report));
  const total = report.rows.length * 2 + report.fixed.length;
  console.log(`tokens: tokens.css, contrast.md (${total} pairs, all AA)`);
  return { tokens, report };
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  buildTokens().catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
}
