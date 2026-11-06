#!/usr/bin/env bash
# Публикует готовую сборку сайта в ветку deploy одним коммитом.
#
#   bash site/tools/publish-deploy.sh build/site
#
# В ветке deploy лежит только содержимое сборки, в корне. История сохраняется:
# каждый коммит называется «deploy: <sha main>», по нему видно, из какой версии сделан сайт.
# Ветку deploy руками не правим. Откат делается на main, см. site/README.md.
#
# Переменные (необязательные):
#   DEPLOY_BRANCH  имя ветки, по умолчанию deploy
#   DEPLOY_REMOTE  имя remote, по умолчанию origin

set -euo pipefail

SRC="${1:?Укажите папку со сборкой, например build/site}"
BRANCH="${DEPLOY_BRANCH:-deploy}"
REMOTE="${DEPLOY_REMOTE:-origin}"

if [ ! -f "$SRC/index.html" ]; then
  echo "В $SRC нет index.html: сначала запустите node site/build.mjs" >&2
  exit 1
fi

SRC_ABS="$(cd "$SRC" && pwd)"
SOURCE_SHA="$(git rev-parse --short HEAD)"
SOURCE_FULL="$(git rev-parse HEAD)"
WT_PARENT="$(mktemp -d)"
WT="$WT_PARENT/deploy"

cleanup() {
  git worktree remove --force "$WT" >/dev/null 2>&1 || true
  git worktree prune >/dev/null 2>&1 || true
  rm -rf "$WT_PARENT"
}
trap cleanup EXIT

# Берём текущую ветку deploy с сервера, если она уже есть.
git fetch --no-tags "$REMOTE" "+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH" >/dev/null 2>&1 || true

if git rev-parse --verify -q "refs/remotes/$REMOTE/$BRANCH" >/dev/null; then
  echo "Ветка $BRANCH есть, добавляем коммит поверх неё."
  git worktree add --detach "$WT" "refs/remotes/$REMOTE/$BRANCH" >/dev/null
  git -C "$WT" checkout -q -B "$BRANCH"
else
  echo "Ветки $BRANCH ещё нет, создаём её с первым коммитом."
  git worktree add --detach "$WT" HEAD >/dev/null
  git -C "$WT" checkout -q --orphan "$BRANCH"
fi

# Убираем всё из рабочей копии ветки и кладём туда только сборку.
git -C "$WT" rm -r -q -f . >/dev/null 2>&1 || true
find "$WT" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -a "$SRC_ABS"/. "$WT"/

# autocrlf отключаем: в ветку должны попадать файлы как есть, с переводами строк LF.
git -C "$WT" -c core.autocrlf=false add -A
if git -C "$WT" diff --cached --quiet; then
  echo "Сборка не изменилась по сравнению с веткой $BRANCH, публиковать нечего."
  exit 0
fi

git -C "$WT" -c core.autocrlf=false \
  -c user.name="github-actions[bot]" \
  -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit -q -m "deploy: $SOURCE_SHA" -m "Сборка из main, коммит $SOURCE_FULL."

git -C "$WT" push "$REMOTE" "HEAD:refs/heads/$BRANCH"
echo "Опубликовано в $BRANCH: deploy: $SOURCE_SHA"
