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
    → terralogic_prepare_case      (сбор + аналитика)
    → terralogic_prepare_quickreport (QuickReport + PDF в CaseStore)
    → terralogic_get_report_pdf    (PDF base64 по MCP-каналу)
    → PDF документом пользователю
```

PDF передаётся через MCP (base64), поэтому **бот может работать на
удалённой машине** — общая файловая система с engine не требуется.

## Требования

- Python ≥ 3.11;
- сетевая доступность `terralogic-mcp` (порт 8004);
- доступ к `api.telegram.org` (или `TELEGRAM_PROXY`).

## Установка и запуск

```bash
cd terralogic-bot
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # вписать TELEGRAM_BOT_TOKEN от @BotFather

# запуск
set -a; . ./.env; set +a
nohup .venv/bin/terralogic-bot > /tmp/terralogic-bot.log 2>&1 &
```

Проверка: написать боту `/start`, затем кадастровый номер, например
`50:03:0070305:370` — через ~1–3 минуты (новый участок дольше) приходит
`TerraLogicX_50-03-0070305-370.pdf`.

## Удалённый режим (прокси)

Бот на отдельной машине, engine — на своём хосте:

```bash
# 1. Туннель до MCP engine (на машине бота)
ssh -L 8004:127.0.0.1:8004 user@terralogic-host

# 2. .env на машине бота
TELEGRAM_BOT_TOKEN=...
TERRALOGIC_MCP_URL=http://127.0.0.1:8004/mcp
TELEGRAM_PROXY=http://proxy-host:3128   # если api.telegram.org закрыт
```

Весь трафик бота: Telegram API (через прокси, если задан) и MCP engine
(через туннель/сеть). Других зависимостей от инфраструктуры engine нет.

## Конфигурация (env)

| Переменная | Default | Назначение |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — (обязательна) | токен @BotFather |
| `TERRALOGIC_MCP_URL` | `http://127.0.0.1:8004/mcp` | MCP endpoint engine |
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
