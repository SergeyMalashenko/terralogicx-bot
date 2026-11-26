# terralogicx-bot

Telegram-интерфейс доставки экспресс-оценок TerraLogicX. Пользователь
отправляет кадастровый номер — бот вызывает существующий use case
`terralogic-mcp` и возвращает готовый QuickReport PDF документом.

> Бот — только delivery/interface layer: никакой аналитики, скоринга,
> рендера и обращений к источникам (NSPD/OSM/2GIS/RGIS). Всё это остаётся
> в `terralogic-engine`.

## Поток

```
Telegram user → КН текстом → terralogicx-bot → terralogic-mcp
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

### Вариант A: pixi (рекомендуется для Jetson / aarch64)

```bash
cd terralogicx-bot
pixi install          # создаст окружение и pixi.lock при первом запуске
cp .env.example .env  # вписать TELEGRAM_BOT_TOKEN от @BotFather
pixi run start        # .env подхватывается автоматически
pixi run test         # юнит-тесты
```

### Вариант B: venv + pip

```bash
cd terralogicx-bot
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # вписать TELEGRAM_BOT_TOKEN от @BotFather

# запуск (.env подхватывается автоматически)
nohup .venv/bin/terralogicx-bot > /tmp/terralogicx-bot.log 2>&1 &
```

Проверка: написать боту `/start`, затем кадастровый номер, например
`50:03:0070305:370` — через ~1–3 минуты (новый участок дольше) приходит
`TerraLogicX_50-03-0070305-370.pdf`.

## Удалённый режим (USB-C линк + прокси)

Топология боевого развёртывания:

```
[удалённая машина 192.168.55.1]          [хост TerraLogicX 192.168.55.100]
   terralogicx-bot ──── MCP ────▶ terralogic-mcp 0.0.0.0:8004
        │                              └─ NSPD/OSM/2GIS/RGIS/geodocs (127.0.0.1)
        └─ api.telegram.org через TELEGRAM_PROXY
```

Engine слушает все интерфейсы (`--host 0.0.0.0`), поэтому туннель не
нужен: бот ходит напрямую на `http://192.168.55.100:8004/mcp`.

Деплой одной командой с хоста engine:

```bash
cd terralogicx-bot
scripts/deploy_remote.sh user@192.168.55.1
```

Скрипт синхронизирует проект (rsync), создаёт venv, ставит пакет и пишет
`.env` с `TERRALOGIC_MCP_URL=http://192.168.55.100:8004/mcp`. Остаётся
вписать `TELEGRAM_BOT_TOKEN` и `TELEGRAM_PROXY` и запустить:

```bash
ssh user@192.168.55.1
cd terralogicx-bot
set -a; . ./.env; set +a
nohup .venv/bin/terralogicx-bot > /tmp/terralogicx-bot.log 2>&1 &
```

Весь трафик бота: Telegram API (через прокси, если задан) и MCP engine
(по USB-C линку). Других зависимостей от инфраструктуры engine нет.

### Автозапуск на Jetson (systemd --user)

Юнит `~/.config/systemd/user/terralogicx-bot.service` уже настроен на
боевой машине; включён linger — бот стартует при загрузке без входа
пользователя. Управление:

```bash
systemctl --user status terralogicx-bot     # состояние
systemctl --user restart terralogicx-bot    # перезапуск (после git pull)
journalctl --user -u terralogicx-bot -f     # логи
```

## Конфигурация (env)

| Переменная | Default | Назначение |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — (обязательна) | токен @BotFather |
| `TERRALOGIC_MCP_URL` | `http://127.0.0.1:8004/mcp` | MCP endpoint engine |
| `TERRALOGIC_REQUEST_TIMEOUT_S` | `900` | таймаут MCP-вызова (сбор идёт минуты) |
| `MAX_CONCURRENT_REPORTS` | `2` | лимит одновременных формирований |
| `TELEGRAM_PROXY` | — | прокси для api.telegram.org |
| `LOG_LEVEL` | `INFO` | уровень логирования |
| `SUPPORT_GROUP_ID` | — (сценарий выключен) | id служебной группы поддержки |
| `SUPPORT_SESSION_TTL_S` | `3600` | таймаут диалога без активности |
| `SUPPORT_DB_PATH` | `data/support.sqlite3` | SQLite сессий и маршрутизации |
| `REPORT_LIMIT` | `0` | бесплатных отчётов на пользователя (0 = нет) |
| `UNLIMITED_USER_IDS` | — (лимит для всех) | id пользователей без лимита, через запятую |
| `QUOTA_DB_PATH` | `data/quota.sqlite3` | SQLite счётчиков выданных отчётов |
| `ROBOKASSA_MERCHANT_LOGIN` | — (оплата выключена) | идентификатор магазина Robokassa |
| `ROBOKASSA_PASSWORD1` / `ROBOKASSA_PASSWORD2` | — | пара паролей из «Технических настроек» (тестовая при `ROBOKASSA_TEST=1`) |
| `ROBOKASSA_TEST` | `1` | 1 = тестовые платежи, 0 = боевые |
| `ROBOKASSA_WEBHOOK_HOST` / `ROBOKASSA_WEBHOOK_PORT` | `127.0.0.1` / `0` (выключен) | HTTP-приёмник ResultURL-уведомлений |
| `DEMO_REPORT_PATH` | — (не отправлять) | PDF демо-отчёта, прикрепляемый к `/start` |
| `OFFER_URL` / `PRIVACY_URL` | — (кнопки скрыты) | публичные ссылки на оферту и политику ПД в карточке заказа |
| `OFFER_VERSION` | из файла оферты | версия действующей редакции оферты |
| `OFFER_TEXT_PATH` | `assets/TerraLogicX_Public_Offer.md` | полный текст действующей оферты |
| `TELEGRAPH_ACCESS_TOKEN` | — | токен telegra.ph для публикации документов |

## Юридические документы (оферта и политика ПД)

Тексты лежат в `assets/TerraLogicX_Public_Offer.md` и
`assets/TerraLogicX_Privacy_Policy.md`, публикуются на telegra.ph:

```bash
python scripts/publish_legal_docs.py   # первый запуск печатает TELEGRAPH_ACCESS_TOKEN
```

Скрипт обновляет одни и те же страницы (`data/telegraph_pages.json`), их URL
постоянны — их и нужно вписать в `OFFER_URL` / `PRIVACY_URL`. В тексте оферты
метка `PRIVACY_URL` при публикации заменяется адресом страницы политики.

Карточка заказа до оплаты содержит предмет, итоговую сумму со скидкой, сроки
и кнопки «Публичная оферта» / «Политика ПД». По каждому заказу в SQLite
сохраняются номер заказа (InvId), Telegram ID, предмет (кадастровый номер,
ссылка или координаты), сумма и валюта, версия показанной оферты и время
подтверждения платежа; полный текст каждой редакции оферты хранится в
таблице `offer_versions` — условия любого заказа восстанавливаются.

## Диалог с поддержкой

Отдельный сценарий, не связанный с отчётами: пользователь входит в
диалог командой `/support` или кнопкой «✉️ Написать в поддержку» под
PDF-отчётом. Его сообщения (текст, фото, документы) копируются в
служебную группу Telegram со шапкой «👤 имя @username · id …». Оператор
отвечает в группе **реплаем** на сообщение бота — ответ копируется
пользователю с префиксом «Ответ поддержки:».

```
Пользователь → /support → бот → шапка + копия в группу → links (SQLite)
Оператор → reply в группе → бот → resolve user_id → копия пользователю
```

- сессии и маршрутизация — в SQLite (`SUPPORT_DB_PATH`), переживают
  перезапуск бота;
- диалог завершается кнопкой «Завершить диалог», повторным `/support`
  или по таймауту (`SUPPORT_SESSION_TTL_S`, фоновый sweeper уведомляет);
- пока `SUPPORT_GROUP_ID` не задан, `/support` отвечает заглушкой,
  остальное поведение бота не меняется.

Настройка (один раз): создать группу → добавить бота → дать права
администратора (иначе privacy mode не даст читать ответы операторов) →
взять `chat_id` из лога (`chat membership: chat_id=-100…`) → вписать в
`.env` → `systemctl --user restart terralogicx-bot`.

## Поведение

- `/start`, `/help` — статическая справка;
- `/status <КН>` — сводка сбора по ранее обработанному участку (read-only,
  `refresh_policy="never"`): постатусно источники и документы с причинами сбоев;
- текст = кадастровый номер (`50:21:0030210:123`), ссылка на объявление
  Авито или координаты с карты (`55.488626, 38.009447`); формат проверяется
  детерминированными парсерами, невалидный ввод не вызывает backend;
- квота и оплата: бесплатных отчётов нет (`REPORT_LIMIT=0`); при исчерпанном
  остатке бот показывает карточку заказа (предмет, цена, сроки, оферта и
  политика) с кнопками «Оплатить» / «Изменить участок»; первый заказ —
  500 ₽ (промо), последующие — 3000 ₽; оплата через Robokassa,
  подтверждение — ResultURL-webhook (плюс запасной опрос статуса кнопкой
  «Проверить оплату»); после подтверждения оплаты отчёт по заказанному
  участку формируется автоматически; списывается только успешно доставленный
  PDF, поэтому при сбое формирования оплаченный кредит сохраняется;
  пользователи из `UNLIMITED_USER_IDS` без лимита; счётчики, счета и
  кредиты — в SQLite (`QUOTA_DB_PATH`), переживают перезапуск;
- один активный запрос на пару (пользователь, вход): повтор — «уже
  формируется»; общий лимит параллелизма — `MAX_CONCURRENT_REPORTS`;
- прогресс: сразу после подтверждения бот отправляет статусное сообщение
  «⏳ Формирую экспресс-оценку» с прогресс-баром и процентами (MCP
  progress-уведомления engine, правка не чаще раза в 2.5 с); по
  завершении — успехе или ошибке — статусное сообщение удаляется;
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
