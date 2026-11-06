# Сайт plugbackup.site

Статический сайт: HTML, CSS и один небольшой JavaScript для переключателя темы и кнопок «Копировать». Без фреймворков, без внешних запросов, без аналитики и cookie. Сайт работает и с выключенным JavaScript.

Шрифты, логотип, favicon и картинка для ссылок берутся из [design/](../design/README.md) и копируются при сборке. Тарифы берутся из [design/data/plans.json](../design/data/plans.json), таблица сравнения из [src/data/compare.json](src/data/compare.json).

## Что где лежит

| Путь | Что это |
| --- | --- |
| `build.mjs` | Сборка: `src/` и `design/` → `build/site`. Без зависимостей, нужен Node.js 20 или новее |
| `src/pages/` | Страницы: `index.html` (главная), `docs.html` (`/docs/`), `terms.html` (`/terms/`), `compare.html` (`/compare/`), `404.html` |
| `src/partials/` | Шапка и подвал, общие для всех страниц |
| `src/data/compare.json` | Данные таблицы на `/compare/`: цифры, источники, даты проверки, метки «проверить» |
| `src/assets/css/site.css` | Вёрстка страниц. Токены и компоненты лежат в `design/` |
| `src/assets/js/` | `theme.js` (тема до первой отрисовки), `app.js` (переключатель и копирование) |
| `src/static/` | `robots.txt`, `sitemap.xml`, `.htaccess`: копируются в корень сборки как есть |
| `tools/publish-deploy.sh` | Публикация сборки в ветку `deploy` |
| `../.github/workflows/site-deploy.yml` | Запуск сборки и публикации при пуше в `main` |

## Посмотреть локально

Нужен Node.js 20 или новее.

```bash
node site/build.mjs
cd build/site
python -m http.server 8080
```

Затем откройте http://127.0.0.1:8080/. В Windows PowerShell команды те же, только переход в папку выглядит так: `cd build\site`.

Встроенный сервер Python не применяет `.htaccess`: не будет редиректа на HTTPS, заголовков безопасности и страницы 404 из корня. Для этого нужен сервер, который читает `.htaccess` или конфиг nginx.

## Тарифы и тексты

**Тарифы.** Меняйте только `design/data/plans.json` и пересоберите сайт. Карточки на главной и список «Что входит» получают данные оттуда. Цифры в тексты страниц не вписываются.

**Таблица сравнения.** Правьте `src/data/compare.json`, вёрстка не трогается:

- у каждой строки (сервиса) есть `checked`, дата проверки, и `sources`, ссылки на официальные страницы;
- ячейка с `"todo": true` выводит метку «проверить»: факт не подтверждён официальной страницей, его нужно перепроверить;
- после правки пересоберите сайт.

**Подстановки в страницах.** `{{bot.url}}`, `{{bot.handle}}`, `{{plans}}`, `{{features}}`, `{{payment.note}}`, `{{compare}}`, `{{site.origin}}`, `{{header}}`, `{{footer}}`. Неизвестная подстановка останавливает сборку.

## Проверки при сборке

Сборка завершается ошибкой, если в результате:

- есть внешний ресурс (картинка, скрипт, стиль, шрифт), то есть ссылка не на свой домен; обычные ссылки на бота, статус и GitHub допустимы;
- есть inline-стиль, блок `<style>` или inline-скрипт: их запрещает CSP;
- картинка без `alt`;
- внутренняя ссылка или ресурс ведёт на несуществующий файл;
- в `sitemap.xml` адрес страницы, которой нет;
- осталась непроставленная подстановка `{{…}}`.

Ошибка выводится со списком проблем, и файлы для публикации не создаются.

## Публикация: ветка deploy

При каждом пуше в `main`, если менялись `site/` или `design/`, GitHub Actions собирает сайт и публикует его в корень ветки `deploy` одним коммитом. Сообщение коммита: `deploy: <короткий sha main>`, по нему видно, из какой версии сделан сайт.

Хостинг забирает ветку `deploy` и кладёт её содержимое в папку сайта, ничего не собирая. Ветку `deploy` не правим руками: её пишет только workflow.

### Подключение на хостинге (один раз)

Названия пунктов зависят от панели. Ищите раздел «Git», «Репозиторий» или «Автодеплой».

1. Укажите репозиторий `https://github.com/Plugbackup/plugbackup.git`. Он публичный, для чтения доступ не нужен.
2. Укажите ветку `deploy`.
3. Укажите папку сайта `www/plugbackup.site`.
4. Сохраните и запустите обновление вручную, если панель это позволяет.
5. Проверьте, что в папке появились `index.html`, `.htaccess` и папка `assets/`.

Если панель обновляет репозиторий по расписанию, новая версия появится с задержкой. Для срочного обновления запустите workflow вручную (см. ниже) и обновите в панели.

Ветку `deploy` создаёт workflow при первом пуше в `main` с изменениями в `site/` или `design/`. Проверить можно на вкладке Actions репозитория или командой `git ls-remote --heads origin deploy`.

### Откат на предыдущую версию

Откат делается на `main`, а workflow сам публикует результат.

```bash
git checkout main
git pull
git revert <sha коммита, который нужно отменить>
git push origin main
```

Коммит с откатом меняет `site/` или `design/`, поэтому workflow запустится и опубликует предыдущее состояние. Номер нужного коммита ищите в `git log -- site design` или в сообщениях `deploy: <sha>` на ветке `deploy`.

Если нужно только перепубликовать текущий `main`, откройте Actions → site-deploy → Run workflow.

Если панель хостинга умеет выбирать конкретный коммит ветки, можно указать коммит `deploy`, у которого в сообщении нужный sha. Это выбор версии в панели, сама ветка при этом не меняется.

## Если на хостинге nginx

`.htaccess` работает только на Apache. На nginx те же правила задаются в конфиге сервера. Путь к корню и сертификаты берите из панели хостинга.

```nginx
server {
    listen 80;
    server_name plugbackup.site;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name plugbackup.site;
    root /путь/из/панели/www/plugbackup.site;
    index index.html;

    # ssl_certificate и ssl_certificate_key выдаёт панель хостинга.

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), payment=(), usb=()" always;
    add_header Cross-Origin-Opener-Policy "same-origin" always;
    add_header Content-Security-Policy "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;

    error_page 404 /404.html;

    # Кэш задан через expires, а не через add_header: add_header в location отменяет заголовки из server.
    location ~* \.woff2$ {
        expires 1y;
    }

    location ~* \.(css|js|svg|png|ico|txt|xml)$ {
        expires 1d;
    }

    location ~* \.html$ {
        expires -1;
    }

    location / {
        try_files $uri $uri/ =404;
    }
}
```

Папка `/docs` без слэша сама перенаправится на `/docs/` благодаря `try_files $uri/`.

## Проверка после публикации

```bash
curl -sI https://plugbackup.site/ | head -20
```

В ответе должны быть `HTTP/2 200`, `strict-transport-security`, `content-security-policy` и `x-content-type-options`. Адрес `http://plugbackup.site/` должен отвечать редиректом на HTTPS.
