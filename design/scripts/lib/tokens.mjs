import { contrast, formatRatio } from './color.mjs';
import { readJson } from './common.mjs';

export const loadTokens = () => readJson('tokens.json');

const decl = (name, value) => `  --${name}: ${value};`;

function flat(prefix, group) {
  return Object.entries(group).map(([key, value]) => decl(`${prefix}-${key}`, value));
}

function themeBlock(tokens, theme) {
  const colors = flat('color', tokens.color[theme]);
  const shadows = flat('shadow', tokens.shadow[theme]);
  return [`  color-scheme: ${theme};`, ...colors, ...shadows].join('\n');
}

export function buildCss(tokens) {
  const { color, font, space, radius, size, shadow, duration, easing } = tokens;
  const base = [
    decl('color-brand', color.brand),
    ...Object.entries(color.palette).flatMap(([hue, scale]) => flat(`color-${hue}`, scale)),
    '',
    ...flat('font-family', font.family),
    ...flat('font-weight', font.weight),
    ...flat('font-size', font.size),
    ...flat('line-height', font['line-height']),
    ...flat('letter-spacing', font['letter-spacing']),
    '',
    ...flat('space', space),
    ...flat('radius', radius),
    ...flat('size', size),
    '',
    ...flat('shadow', shadow.common),
    ...flat('duration', duration),
    ...flat('ease', easing),
  ].join('\n');

  return `/*
 * PlugBackup design tokens.
 * Generated from tokens.json by scripts/build-tokens.mjs: edit the JSON, not this file.
 *
 * Themes: light by default; dark follows prefers-color-scheme.
 * The data-theme="light" | "dark" attribute on <html> overrides the system setting.
 */

:root {
${base}

  /* Theme: light */
${themeBlock(tokens, 'light')}
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme='light']) {
${themeBlock(tokens, 'dark').replace(/^/gm, '  ')}
  }
}

:root[data-theme='dark'] {
${themeBlock(tokens, 'dark')}
}
`;
}

// Contrast ---------------------------------------------------------------

export function contrastRows(tokens) {
  const themes = ['light', 'dark'];
  const rows = [];
  const kinds = [
    ['text', 4.5],
    ['ui', 3],
  ];
  for (const [kind, min] of kinds) {
    for (const [fg, bg] of tokens.contrast[kind]) {
      const cells = Object.fromEntries(
        themes.map((theme) => {
          const fgHex = tokens.color[theme][fg];
          const bgHex = tokens.color[theme][bg];
          if (!fgHex || !bgHex) throw new Error(`Unknown token in pair ${fg} / ${bg}`);
          const ratio = contrast(fgHex, bgHex);
          return [theme, { fg: fgHex, bg: bgHex, ratio, pass: ratio >= min }];
        }),
      );
      rows.push({ kind, label: `${fg} / ${bg}`, min, ...cells });
    }
  }
  const fixed = tokens.contrast.fixed.map(({ label, fg, bg, min }) => {
    const ratio = contrast(fg, bg);
    return { kind: 'fixed', label, fg, bg, min, ratio, pass: ratio >= min };
  });
  return { rows, fixed };
}

export function contrastFailures({ rows, fixed }) {
  const bad = [];
  for (const row of rows) {
    for (const theme of ['light', 'dark']) {
      if (!row[theme].pass) bad.push(`${theme}: ${row.label} ${formatRatio(row[theme].ratio)} < ${row.min}`);
    }
  }
  for (const row of fixed) {
    if (!row.pass) bad.push(`fixed: ${row.label} ${formatRatio(row.ratio)} < ${row.min}`);
  }
  return bad;
}

const mark = (cell) => `${formatRatio(cell.ratio)} ${cell.pass ? '✓' : '✗'}`;

export function contrastMarkdown({ rows, fixed }) {
  const total = rows.length * 2 + fixed.length;
  const section = (kind, title, note) => {
    const list = rows.filter((row) => row.kind === kind);
    return `### ${title}

${note}

| Пара (текст / фон) | Светлая тема | Тёмная тема | Норма |
| --- | --- | --- | --- |
${list
  .map(
    (row) =>
      `| \`${row.label}\` | ${mark(row.light)} <br> \`${row.light.fg}\` на \`${row.light.bg}\` | ${mark(row.dark)} <br> \`${row.dark.fg}\` на \`${row.dark.bg}\` | ${row.min}:1 |`,
  )
  .join('\n')}
`;
  };

  return `# Контрасты

Таблица создаётся скриптом \`npm run build\` из \`tokens.json\`, вручную её не правьте. Все ${total} проверок проходят WCAG 2.2 AA. Если проверка не проходит, сборка завершается с ошибкой.

Способ расчёта: относительная яркость по WCAG, отношение \`(L1 + 0,05) / (L2 + 0,05)\`. Значения округляются вниз, чтобы 4,497 не превратилось в 4,50.

Нормы AA: 4,5:1 для обычного текста, 3:1 для границ полей, кнопок, рамок фокуса и графики.

Не проверяются неактивные элементы (\`disabled-fg\` на \`disabled-bg\`): WCAG их не требует. Декоративные границы (\`border\`) тоже не проверяются: смысла они не несут.

${section('text', 'Текст', 'Все пары для текста обычного размера, нужно 4,5:1.')}
${section('ui', 'Границы, рамки и заливки интерактивных элементов', 'Нужно 3:1 к фону, на котором элемент лежит.')}
### Графика и логотип

Эти цвета не зависят от темы.

| Что проверяем | Значение | Норма |
| --- | --- | --- |
${fixed
  .map((row) => `| ${row.label} | ${mark(row)} <br> \`${row.fg}\` на \`${row.bg}\` | ${row.min}:1 |`)
  .join('\n')}
`;
}
