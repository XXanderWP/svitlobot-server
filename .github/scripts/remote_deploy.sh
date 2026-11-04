#!/usr/bin/env bash
# Виконується на сервері під час деплою.
#   $1 — тимчасова тека з розпакованим пакетом (server/, deploy.env, compose.env)
#   $2 — тека проєкту на сервері (можна з ~, напр. ~/Svitlobot)
# Скрипт не виводить вміст .env і не показує логи контейнера, щоб у вивід GitHub Action
# не потрапили секрети.
set -euo pipefail

SRC="$1"
DIR="$2"
case "$DIR" in
  "~") DIR="$HOME" ;;
  "~/"*) DIR="$HOME/${DIR#"~/"}" ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "::error::На сервері не встановлено Docker"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "::error::На сервері немає Docker Compose v2 (команда «docker compose»)"
  exit 1
fi

mkdir -p "$DIR/data"
chmod 700 "$DIR/data"

# Оновлюємо код. Теку data/ (стан і .env.override) не чіпаємо.
rm -rf "$DIR/app"
cp -R "$SRC/server/." "$DIR/"
install -m 600 "$SRC/deploy.env" "$DIR/.env"
install -m 600 "$SRC/compose.env" "$DIR/compose.env"

cd "$DIR"
echo "• Збирання образу та перезапуск контейнера..."
# --force-recreate: контейнер перечитає новий .env навіть без змін у коді.
# --env-file compose.env: Compose не читає .env із секретами (див. make_env.py).
# Старий контейнер міг бути без порту стуків — --force-recreate застосує нову конфігурацію.
docker compose --env-file compose.env up -d --build --force-recreate --remove-orphans --quiet-pull

echo "• Очікування перевірки стану контейнера..."
for i in $(seq 1 45); do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' svitlobot-monitor 2>/dev/null || echo "відсутній")"
  if [ "$status" = "healthy" ]; then
    echo "✔ Контейнер працює (стан: healthy)"
    docker image prune -f >/dev/null 2>&1 || true
    exit 0
  fi
  if [ "$status" = "unhealthy" ] || [ "$status" = "exited" ] || [ "$status" = "відсутній" ]; then
    break
  fi
  sleep 2
done

echo "::error::Контейнер не запустився (стан: $status). Перегляньте логи на сервері: cd $2 && docker compose --env-file compose.env logs --tail 50"
exit 1
