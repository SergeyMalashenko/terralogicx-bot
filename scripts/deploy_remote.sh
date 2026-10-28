#!/usr/bin/env bash
# Деплой terralogicx-bot на удалённую машину по USB-C линку.
#
# Топология:
#   удалённая машина (бот)  192.168.55.1
#   хост TerraLogicX        192.168.55.100 (terralogic-mcp на 0.0.0.0:8004)
#
# Использование:
#   scripts/deploy_remote.sh [user@]192.168.55.1
#
# Требования на удалённой машине: python3 ≥ 3.11, доступ в интернет
# (pip install) либо заранее подготовленный wheelhouse.
set -euo pipefail

REMOTE="${1:-192.168.55.1}"
REMOTE_DIR="${REMOTE_DIR:-My_projects/terralogicx-bot}"
ENGINE_URL="${ENGINE_URL:-http://192.168.55.100:8004/mcp}"

cd "$(dirname "$0")/.."

echo "== rsync проекта на $REMOTE:$REMOTE_DIR"
rsync -a --delete \
    --exclude .venv --exclude .git --exclude .env \
    --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
    ./ "$REMOTE:$REMOTE_DIR/"

echo "== установка на $REMOTE"
ssh "$REMOTE" bash -s <<EOF
set -euo pipefail
cd "$REMOTE_DIR"
python3 -m venv .venv
.venv/bin/pip install -q -e .
if [ ! -f .env ]; then
    cp .env.example .env
    sed -i "s|^TERRALOGIC_MCP_URL=.*|TERRALOGIC_MCP_URL=$ENGINE_URL|" .env
    echo "Создан .env — впишите TELEGRAM_BOT_TOKEN и TELEGRAM_PROXY"
fi
EOF

echo "== готово. Запуск на $REMOTE:"
echo "  ssh $REMOTE 'cd $REMOTE_DIR && set -a; . ./.env; set +a; \\"
echo "    nohup .venv/bin/terralogicx-bot > /tmp/terralogicx-bot.log 2>&1 &'"
