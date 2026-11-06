#!/usr/bin/env node
// Сборка сайта plugbackup.site. Без внешних зависимостей, нужен Node.js 20 или новее.
//
//   node site/build.mjs                  # результат в build/site
//   node site/build.mjs --out <папка>    # результат в другой папке
//
// Вход: site/src (страницы, данные, стили и скрипты) и design/ (токены, шрифты, логотип, графика).
// Тарифы берутся из design/data/plans.json, таблица сравнения из site/src/data/compare.json.
// В конце сборка проверяет результат: внешние ресурсы, inline-код, ссылки и подстановки.
// Если проверка не проходит, сборка завершается с ошибкой, и workflow ничего не публикует.

import { cp, mkdir, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '..');
const src = join(here, 'src');
const design = join(repo, 'design');

const outFlag = process.argv.indexOf('--out');
const out = resolve(outFlag > 0 ? process.argv[outFlag + 1] : join(repo, 'build', 'site'));

// Страницы: исходный файл → путь в сборке. Пути с / на конце отдаются как папка с index.html.
const PAGES = [
  ['index.html', 'index.html'],
  ['docs.html', 'docs/index.html'],
  ['terms.html', 'terms/index.html'],
  ['compare.html', 'compare/index.html'],
  ['404.html', '404.html'],
];

// Файлы из design/: откуда → куда в сборке. Шрифты woff2 копируются все из design/fonts/.
const DESIGN_COPY = [
  ['tokens.css', 'assets/css/tokens.css'],
  ['components.css', 'assets/css/components.css'],
  ['animations.css', 'assets/css/animations.css'],
  ['fonts/fonts.css', 'assets/fonts/fonts.css'],
  ['fonts/OFL-IBM-Plex-Sans.txt', 'assets/fonts/OFL-IBM-Plex-Sans.txt'],
  ['fonts/OFL-IBM-Plex-Mono.txt', 'assets/fonts/OFL-IBM-Plex-Mono.txt'],
  ['logo/logo-on-light.svg', 'assets/img/logo-on-light.svg'],
  ['logo/logo-on-dark.svg', 'assets/img/logo-on-dark.svg'],
  ['graphics/svg/favicon.svg', 'favicon.svg'],
  ['graphics/png/apple-touch-icon.png', 'apple-touch-icon.png'],
  ['graphics/png/og-image.png', 'assets/img/og-image.png'],
];

const escapeHtml = (value) =>
  String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

// Число и единица не разрываются: «10 ГБ», «79 ₽».
const typo = (text) => text.replace(/(\d) (ГБ|ТБ|₽)/g, '$1\u00a0$2');

const fmtDate = (iso) => {
  const [year, month, day] = iso.split('-');
  return `${day}.${month}.${year}`;
};

const readJson = async (file) => JSON.parse(await readFile(file, 'utf8'));

function renderPlans(data) {
  const cards = data.plans.map((plan, index) => {
    const beta = plan.price_rub === 0;
    const label = beta ? 'Бета' : 'Платный';
    const price = beta ? 'Бесплатно' : `${plan.price_rub} ₽/мес`;
    const access = plan.access ? `Доступ ${plan.access}.` : `Оплата в ${data.payment}.`;
    const id = `plan-${plan.id}`;
    // Декоративные пины показывают уровень тарифа: бета один, 200 ГБ все четыре.
    const pins = data.plans
      .map((_, pin) => `<i class="pb-tier__pin${pin <= index ? ' is-on' : ''}"></i>`)
      .join('');
    const note = plan.note ? `\n      <p class="pb-plan__note">${escapeHtml(typo(plan.note))}</p>` : '';
    return `    <li>
      <article class="pb-card pb-plan" aria-labelledby="${id}">
        <div class="pb-plan__head">
          <p class="pb-plan__name">${label}</p>
          <span class="pb-tier" aria-hidden="true">${pins}</span>
        </div>
        <p class="pb-plan__size" id="${id}">${escapeHtml(typo(`${plan.gb} ГБ`))}</p>
        <p class="pb-plan__price">${escapeHtml(typo(price))}</p>
        <p class="pb-plan__access">${escapeHtml(typo(access))}</p>${note}
      </article>
    </li>`;
  });
  return `<ul class="pb-plans">\n${cards.join('\n')}\n  </ul>`;
}

function renderFeatures(data) {
  const items = data.features.map((item) => `<li>${escapeHtml(typo(item))}</li>`).join('');
  return `<ul class="feature-list">${items}</ul>`;
}

function renderCompare(data) {
  const columns = data.columns;
  const head = columns.map((column) => `<th scope="col">${escapeHtml(column.label)}</th>`).join('');
  const rows = data.services.map((service) => {
    const sources = service.sources
      .map((source) => `<a href="${escapeHtml(source.url)}" rel="noopener">${escapeHtml(source.title)}</a>`)
      .join('; ');
    const meta = `<span class="compare-meta">Источник: ${sources}. Проверено ${fmtDate(service.checked)}.</span>`;
    const cells = columns.map((column) => {
      const cell = service.cells[column.id];
      if (!cell) throw new Error(`compare.json: у «${service.id}» нет ячейки «${column.id}»`);
      const todo = cell.todo ? ' <span class="pb-badge pb-badge--warning compare-todo">проверить</span>' : '';
      return `<td>${escapeHtml(typo(cell.text))}${todo}</td>`;
    });
    const rowClass = service.own ? ' class="is-own"' : '';
    return `      <tr${rowClass}>
        <th scope="row"><span class="compare-name">${escapeHtml(service.name)}</span>${meta}</th>
        ${cells.join('\n        ')}
      </tr>`;
  });
  return `<div class="compare-scroll" role="region" aria-labelledby="compare-caption" tabindex="0">
    <table class="compare-table">
      <caption id="compare-caption" class="compare-caption">Сравнение по состоянию на ${fmtDate(data.updated)}</caption>
      <thead>
        <tr><th scope="col">Сервис</th>${head}</tr>
      </thead>
      <tbody>
${rows.join('\n')}
      </tbody>
    </table>
  </div>`;
}

function fill(html, vars, page) {
  return html.replace(/\{\{([a-zA-Z.-]+)\}\}/g, (match, key) => {
    if (!(key in vars)) throw new Error(`${page}: неизвестная подстановка ${match}`);
    return vars[key];
  });
}

async function walk(dir) {
  const files = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) files.push(...(await walk(path)));
    else files.push(path);
  }
  return files;
}

async function exists(path) {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}

// Проверка результата: то, что нельзя ломать. Возвращает список проблем.
async function verify(dir) {
  const problems = [];
  const files = await walk(dir);
  const pages = files.filter((file) => file.endsWith('.html'));

  for (const file of pages) {
    const html = await readFile(file, 'utf8');
    const rel = relative(dir, file).split('\\').join('/');
    const add = (text) => problems.push(`${rel}: ${text}`);

    if (/\{\{/.test(html)) add('осталась подстановка {{…}}');
    if (/<style[\s>]/i.test(html)) add('блок <style> запрещён CSP');
    if (/\sstyle\s*=/i.test(html)) add('атрибут style запрещён CSP');
    if (/<script(?![^>]*\ssrc=)/i.test(html)) add('inline-скрипт запрещён CSP');
    if (/<img\b(?![^>]*\balt=)[^>]*>/i.test(html)) add('картинка без alt');

    // Ресурсы (не ссылки на страницы) должны быть свои: относительные или от корня.
    for (const tag of html.match(/<(img|script|link|source)\b[^>]*>/gi) ?? []) {
      // rel="canonical" не загружается браузером, это просто адрес страницы для поисковиков.
      if (/rel="canonical"/i.test(tag)) continue;
      for (const [, attr, value] of tag.matchAll(/\b(src|href|srcset)="([^"]*)"/gi)) {
        if (/^(https?:)?\/\//i.test(value) || /^(data|blob):/i.test(value)) {
          add(`внешний ресурс ${attr}="${value}"`);
        }
      }
    }

    // Внутренние ссылки и ресурсы должны существовать в сборке.
    for (const [, value] of html.matchAll(/\b(?:href|src)="(\/[^"#?]*)(?:[#?][^"]*)?"/g)) {
      const path = decodeURIComponent(value);
      const target = join(dir, path);
      const ok = path.endsWith('/') ? await exists(join(target, 'index.html')) : await exists(target);
      if (!ok) add(`битая внутренняя ссылка ${value}`);
    }
  }

  const sitemap = join(dir, 'sitemap.xml');
  if (await exists(sitemap)) {
    const xml = await readFile(sitemap, 'utf8');
    for (const [, loc] of xml.matchAll(/<loc>https:\/\/[^/]+(\/[^<]*)<\/loc>/g)) {
      const target = join(dir, decodeURIComponent(loc));
      const ok = loc.endsWith('/') ? await exists(join(target, 'index.html')) : await exists(target);
      if (!ok) problems.push(`sitemap.xml: нет страницы ${loc}`);
    }
  } else {
    problems.push('sitemap.xml: файл не найден');
  }

  return problems;
}

async function main() {
  const plansData = await readJson(join(design, 'data', 'plans.json'));
  const compare = await readJson(join(src, 'data', 'compare.json'));

  const vars = {
    'bot.url': plansData.bot.url,
    'bot.handle': escapeHtml(plansData.bot.handle),
    'site.origin': `https://${plansData.site}`,
    plans: renderPlans(plansData),
    features: renderFeatures(plansData),
    'payment.note': escapeHtml(typo(plansData.payment_note)),
    compare: renderCompare(compare),
    'compare.updated': fmtDate(compare.updated),
  };

  const header = await readFile(join(src, 'partials', 'header.html'), 'utf8');
  const footer = await readFile(join(src, 'partials', 'footer.html'), 'utf8');

  // Чистим содержимое, а не саму папку: на Windows папку, открытую локальным сервером, не удалить.
  await mkdir(out, { recursive: true });
  for (const entry of await readdir(out)) {
    await rm(join(out, entry), { recursive: true, force: true });
  }

  await cp(join(src, 'static'), out, { recursive: true });
  await cp(join(src, 'assets'), join(out, 'assets'), { recursive: true });

  for (const [from, to] of DESIGN_COPY) {
    const target = join(out, to);
    await mkdir(dirname(target), { recursive: true });
    await cp(join(design, from), target);
  }
  const fontsDir = join(design, 'fonts');
  for (const name of await readdir(fontsDir)) {
    if (!name.endsWith('.woff2')) continue;
    await cp(join(fontsDir, name), join(out, 'assets', 'fonts', name));
  }

  for (const [from, to] of PAGES) {
    const page = from;
    const raw = await readFile(join(src, 'pages', from), 'utf8');
    const html = fill(raw.replace('{{header}}', () => header).replace('{{footer}}', () => footer), vars, page);
    const target = join(out, to);
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, html, 'utf8');
  }

  const problems = await verify(out);
  if (problems.length) {
    console.error('Проверка сборки не пройдена:');
    for (const problem of problems) console.error(`  - ${problem}`);
    process.exit(1);
  }

  const count = (await walk(out)).length;
  console.log(`Сборка готова: ${count} файлов в ${relative(process.cwd(), out) || out}`);
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
