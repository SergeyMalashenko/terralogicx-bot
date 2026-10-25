# terralogic-bot

Telegram-интерфейс доставки экспресс-оценок TerraLogicX. Пользователь
отправляет кадастровый номер — бот вызывает существующий use case
`terralogic-mcp` и возвращает готовый QuickReport PDF документом.

> Бот — только delivery/interface layer: никакой аналитики, скоринга,
> рендера и обращений к источникам (NSPD/OSM/2GIS/RGIS). Всё это остаётся
> в `terralogic-engine`.

## Поток

```
Telegram user → КН текстом → terralogic-bot → terralogic-mcp
    → terralogic_prepare_case (сбор + аналитика)
    → terralogic_prepare_quickreport (QuickReport + PDF)
    → PDF документом пользователю
```

## Требования

- Python ≥ 3.11;
- запущенный контур TerraLogicX: `pynspd-mcp :8001`, `pyosm-mcp :8002`,
  `py2gis-mcp :8003`, `terralogic-mcp :8004` (плюс `pyrgis-mcp :8005`,
  `geodocs-mcp :8006` для региона 50);
- PDF читается с общей файловой системы (single-host): боту нужен путь
  к CaseStore engine;
- доступ к `api.telegram.org` (или `TELEGRAM_PROXY`).

## Установка и запуск

```bash
cd terralogic-bot
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # вписать TELEGRAM_BOT_TOKEN от @BotFather

# запуск (по образцу остальных сервисов workspace)
set -a; . ./.env; set +a
nohup .venv/bin/terralogic-bot > /tmp/terralogic-bot.log 2>&1 &
```

Проверка: написать боту `/start`, затем кадастровый номер, например
`50:03:0070305:370` — через ~1–3 минуты (новый участок дольше) приходит
`TerraLogicX_50-03-0070305-370.pdf`.

## Конфигурация (env)

| Переменная | Default | Назначение |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — (обязательна) | токен @BotFather |
| `TERRALOGIC_MCP_URL` | `http://127.0.0.1:8004/mcp` | MCP endpoint engine |
| `TERRALOGIC_CASE_STORE_PATH` | `./terralogic-engine/case-store` | корень CaseStore (чтение PDF) |
| `TERRALOGIC_REQUEST_TIMEOUT_S` | `900` | таймаут MCP-вызова (сбор идёт минуты) |
| `MAX_CONCURRENT_REPORTS` | `2` | лимит одновременных формирований |
| `TELEGRAM_PROXY` | — | прокси для api.telegram.org |
| `LOG_LEVEL` | `INFO` | уровень логирования |

## Поведение

- `/start`, `/help` — статическая справка;
- текст = кадастровый номер (`50:21:0030210:123`); формат проверяется
  детерминированным парсером, невалидный ввод не вызывает backend;
- один активный запрос на пару (пользователь, КН): повтор — «уже
  формируется»; общий лимит параллелизма — `MAX_CONCURRENT_REPORTS`;
- `refresh_policy` — `if_stale` (дефолт engine): повторный запрос по
  свежему кейсу не пересобирает источники;
- ошибки пользователю — дружелюбные тексты без traceback; запросы
  логируются с `request_id`, user_id, КН, case_id, run_id, длительностью.

## Тесты

```bash
.venv/bin/pip install pytest pytest-asyncio ruff
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check src tests
```

Тесты не требуют Telegram API и живого backend (fake client/transport).
