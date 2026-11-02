// Fills the generated blocks of index.html: everything that depends on tokens.json or data/plans.json.
// A block is the text between <!--@build:name--> and <!--@/build:name-->.
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { contrast, formatRatio } from './lib/color.mjs';
import { path, writeText } from './lib/common.mjs';
import { loadPlans, planView } from './lib/plans.mjs';
import { lockupInline, markInline } from './lib/pins.mjs';
import { contrastRows, loadTokens } from './lib/tokens.mjs';

const esc = (value) =>
  String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/ /g, '&nbsp;');

const px = (rem) => `${Math.round(parseFloat(rem) * 16 * 100) / 100}`;
// Label colour for a swatch: the first of white, graphite and black that passes AA for small text.
const onColor = (hex) => ['#FFFFFF', '#16181C', '#000000'].find((fg) => contrast(hex, fg) >= 4.5) ?? '#000000';

const semanticGroups = [
  ['Фоны', ['bg-page', 'bg-surface', 'bg-sunken']],
  ['Границы', ['border', 'border-strong']],
  ['Текст', ['text-primary', 'text-secondary', 'text-muted', 'text-link', 'text-link-hover', 'text-on-accent']],
  ['Акцент и фокус', ['accent', 'accent-hover', 'accent-active', 'accent-subtle', 'accent-border', 'focus-ring']],
  [
    'Статусы',
    ['success', 'warning', 'danger', 'info'].flatMap((name) => ['fg', 'bg', 'border'].map((part) => `${name}-${part}`)),
  ],
  ['Блок кода', ['code-bg', 'code-fg', 'code-muted', 'code-accent', 'code-string', 'code-flag', 'code-border']],
  ['Недоступные элементы', ['disabled-bg', 'disabled-fg']],
];

const blocks = {
  async 'logo-lockup'() {
    const { width, height, body } = await lockupInline();
    return `<svg class="pb-logo sc-lockup" viewBox="0 0 ${width} ${height}" role="img" aria-label="PlugBackup">${body}</svg>`;
  },

  async 'logo-hero'() {
    return `<svg id="hero-logo" class="pb-logo pb-logo--animate sc-hero__mark" viewBox="-4 -4 31 48" role="img" aria-label="Знак PlugBackup: буква P из пинов">${markInline()}</svg>`;
  },

  async 'logo-motion'() {
    return `<svg id="motion-logo" class="pb-logo pb-logo--animate sc-motion-logo" viewBox="-4 -4 31 48" role="img" aria-label="Знак PlugBackup: анимация появления">${markInline()}</svg>`;
  },

  async palette({ tokens }) {
    const names = { neutral: 'Нейтральные', accent: 'Акцент', green: 'Успех', amber: 'Предупреждение', red: 'Ошибка' };
    const brand = tokens.color.brand;
    const brandRow = `<div class="sc-palette"><h4 class="pb-h4">Цвет знака</h4><div class="sc-chips"><div class="sc-chip" style="--chip:${brand};--chip-fg:${onColor(brand)}"><span>brand</span><code>${brand}</code></div></div><p class="pb-small">Только для графики (включённый пин, акценты на графике). Для текста и кнопок есть отдельные токены ниже: этот цвет не проходит AA для мелкого текста.</p></div>`;
    const rows = Object.entries(tokens.color.palette).map(([hue, scale]) => {
      const chips = Object.entries(scale)
        .map(
          ([step, hex]) =>
            `<div class="sc-chip" style="--chip:${hex};--chip-fg:${onColor(hex)}"><span>${hue}-${step}</span><code>${hex}</code></div>`,
        )
        .join('');
      return `<div class="sc-palette"><h4 class="pb-h4">${names[hue] ?? hue}</h4><div class="sc-chips">${chips}</div></div>`;
    });
    return [brandRow, ...rows].join('\n');
  },

  async semantic({ tokens }) {
    const { light, dark } = tokens.color;
    const seen = new Set();
    const groups = semanticGroups.map(([title, keys]) => {
      const rows = keys.map((key) => {
        if (!(key in light)) throw new Error(`semantic block: unknown token ${key}`);
        seen.add(key);
        return `<div class="sc-token"><span class="sc-token__pair"><i style="background:${light[key]}" title="Светлая тема"></i><i style="background:${dark[key]}" title="Тёмная тема"></i></span><code>--color-${key}</code><span class="sc-token__vals">${light[key]} · ${dark[key]}</span></div>`;
      });
      return `<section class="sc-token-group"><h4 class="pb-label">${title}</h4>${rows.join('')}</section>`;
    });
    const missing = Object.keys(light).filter((key) => !seen.has(key));
    if (missing.length) throw new Error(`semantic block: tokens without a group: ${missing.join(', ')}`);
    return groups.join('\n');
  },

  async contrast({ tokens }) {
    const { rows, fixed } = contrastRows(tokens);
    const cell = (c) =>
      `<td><span class="sc-aa" style="color:${c.fg};background:${c.bg}">Aa</span> <span class="sc-ratio">${formatRatio(c.ratio)} ${c.pass ? '✓' : '✗'}</span></td>`;
    const body = rows
      .map(
        (row) =>
          `<tr><td><code>${row.label.replace(' / ', '</code> на <code>')}</code></td>${cell(row.light)}${cell(row.dark)}<td class="num">${row.min}:1</td></tr>`,
      )
      .join('\n');
    const fixedBody = fixed
      .map(
        (row) =>
          `<tr><td>${esc(row.label)}</td><td colspan="2"><span class="sc-aa" style="color:${row.fg};background:${row.bg}">Aa</span> <span class="sc-ratio">${formatRatio(row.ratio)} ${row.pass ? '✓' : '✗'}</span> <code>${row.fg}</code> на <code>${row.bg}</code></td><td class="num">${row.min}:1</td></tr>`,
      )
      .join('\n');
    const total = rows.length * 2 + fixed.length;
    return `<details class="sc-details"><summary>Таблица контрастов: ${total} проверок, все проходят AA</summary><div class="sc-scroll"><table class="sc-table"><thead><tr><th scope="col">Пара</th><th scope="col">Светлая тема</th><th scope="col">Тёмная тема</th><th scope="col">Норма</th></tr></thead><tbody>${body}${fixedBody}</tbody></table></div><p class="pb-small">Та же таблица лежит в <a class="pb-link" href="contrast.md">contrast.md</a>.</p></details>`;
  },

  async 'type-scale'({ tokens }) {
    return Object.entries(tokens.font.size)
      .map(
        ([name, rem]) =>
          `<div class="sc-type"><div class="sc-type__meta"><code>--font-size-${name}</code><span>${rem} · ${px(rem)} px</span></div><div class="sc-type__sample" style="font-size:var(--font-size-${name})">Резервные копии</div></div>`,
      )
      .join('\n');
  },

  async space({ tokens }) {
    return Object.entries(tokens.space)
      .filter(([name]) => name !== '0')
      .map(
        ([name, value]) =>
          `<div class="sc-space"><code>--space-${name}</code><span class="sc-space__bar" style="width:var(--space-${name})"></span><span class="sc-space__val">${value === '1px' ? '1 px' : `${px(value)} px`}</span></div>`,
      )
      .join('\n');
  },

  async radius({ tokens }) {
    return Object.entries(tokens.radius)
      .map(
        ([name, value]) =>
          `<figure class="sc-radius"><span style="border-radius:var(--radius-${name})"></span><figcaption><code>--radius-${name}</code><br>${value === '9999px' ? 'полный' : value}</figcaption></figure>`,
      )
      .join('\n');
  },

  async shadow({ tokens }) {
    const items = Object.keys(tokens.shadow.light).map(
      (name) =>
        `<figure class="sc-shadow"><span style="box-shadow:var(--shadow-${name})"></span><figcaption><code>--shadow-${name}</code></figcaption></figure>`,
    );
    items.push(
      `<figure class="sc-shadow"><span class="sc-shadow__focus" style="box-shadow:var(--shadow-focus)"></span><figcaption><code>--shadow-focus</code></figcaption></figure>`,
    );
    return items.join('\n');
  },

  async motion({ tokens }) {
    const row = (label, value, d, e) =>
      `<div class="sc-motion" style="--d:${d};--e:${e}"><code>${label}</code><span class="sc-motion__val">${value}</span><span class="sc-motion__track"><i></i></span></div>`;
    const durations = Object.entries(tokens.duration).map(([name, value]) =>
      row(`--duration-${name}`, value, `var(--duration-${name})`, 'var(--ease-standard)'),
    );
    const easings = Object.entries(tokens.easing).map(([name, value]) =>
      row(`--ease-${name}`, value, '900ms', `var(--ease-${name})`),
    );
    return `<div><h4 class="pb-h4">Длительности</h4>${durations.join('')}</div><div><h4 class="pb-h4">Кривые</h4>${easings.join('')}</div>`;
  },

  async plans({ plans }) {
    const features = plans.features.map((text) => `<li>${esc(text)}</li>`).join('');
    const cards = plans.plans.map((plan, index) => {
      const view = planView(plan);
      const featured = view.free;
      const label = view.name.replace(/\s/g, '') === view.size.replace(/\s/g, '') ? 'Тариф' : view.name;
      const tier = [0, 1, 2, 3]
        .map((i) => `<i class="pb-tier__pin${i <= index ? ' is-on' : ''}" style="--i:${i}"></i>`)
        .join('');
      const price = view.free
        ? `<p class="pb-plan__price">${esc(view.price)}</p>`
        : `<p class="pb-plan__price">${esc(view.priceValue)} <span class="pb-plan__period">${esc(view.period)}</span></p>`;
      const lines = [
        `<article class="pb-card pb-card--interactive pb-plan${featured ? ' pb-plan--featured' : ''}">`,
        `  <header class="pb-plan__head"><h4 class="pb-plan__name">${esc(label)}</h4><span class="pb-tier" aria-hidden="true">${tier}</span></header>`,
        `  <p class="pb-plan__size">${esc(view.size)}</p>`,
        `  ${price}`,
        view.access && `  <p class="pb-plan__access">${esc(view.access[0].toUpperCase() + view.access.slice(1))}</p>`,
        view.note && `  <p class="pb-plan__note">${esc(view.note)}</p>`,
        `  <ul class="pb-plan__features">${features}</ul>`,
        `  <a class="pb-button pb-button--block pb-plan__cta${featured ? ' pb-button--primary' : ''}" href="${esc(plans.bot.url)}">Открыть ${esc(plans.bot.handle)}</a>`,
        `</article>`,
      ];
      return lines.filter(Boolean).join('\n');
    });
    return `<div class="pb-plans">${cards.join('\n')}</div>\n<p class="pb-caption sc-footnote">${esc(plans.payment_note)}</p>`;
  },
};

export async function buildShowcase() {
  const [tokens, plans] = await Promise.all([loadTokens(), loadPlans()]);
  const file = path('index.html');
  const html = await readFile(file, 'utf8');
  const found = new Set();

  const pattern = /(<!--@build:([\w-]+)-->)([\s\S]*?)(<!--@\/build:\2-->)/g;
  const replacements = [];
  for (const match of html.matchAll(pattern)) {
    const name = match[2];
    const render = blocks[name];
    if (!render) throw new Error(`index.html: unknown block "${name}"`);
    found.add(name);
    replacements.push([match[0], `${match[1]}\n${await render({ tokens, plans })}\n${match[4]}`]);
  }
  const missing = Object.keys(blocks).filter((name) => !found.has(name));
  if (missing.length) throw new Error(`index.html: blocks not used: ${missing.join(', ')}`);

  let out = html;
  for (const [from, to] of replacements) out = out.replace(from, () => to);
  const changed = await writeText(file, out);
  console.log(`showcase: ${found.size} blocks in index.html${changed ? '' : ' (no changes)'}`);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  buildShowcase().catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
}
