# Битва за Херсонес 2026 — Соревновательный ТМА + Админка

**Отдельный проект.** Никак не связан с регистрационным ботом и результатами
отбора (`crossfit-tma`) — другой код, другой сервис, другой бот.

## Что внутри

| Файл | Назначение |
|---|---|
| `backend.py` | API + админ-API + (опц.) телеграм-бот. Хранилище — PostgreSQL |
| `index.html` | ТМА для атлетов: Лидерборд / Комплексы / Расписание / Заходы |
| `admin.html` | Админка (десктоп): баллы, заходы, комплексы. Вход по паролю |
| `sample_data.py` | Демо-данные (сидинг БД при первом запуске / фолбэк без БД) |
| `assets/` | `logo.png`, `hero.png` (логотип и фон) |
| `dev.sh` | Локальный запуск: Postgres + бэкенд + туннель |

## Архитектура

```
АДМИНКА (ноутбук, /admin, пароль) ──записывает──► PostgreSQL ◄──читает── ТМА (телеграм)
```
- Админка пишет в Postgres, ТМА читает из Postgres. Лидерборд считается автоматически
  (сумма баллов по комплексам, больше баллов = выше).
- Если `DATABASE_URL` не задан — бэкенд отдаёт демо из `sample_data.py`.

## Эндпоинты

Публичные: `/` (ТМА), `/admin` (админка), `/healthz`,
`/api/leaderboard?category=&gender=`, `/api/wods`, `/api/schedule`, `/api/heats`.

Админ (заголовок `X-Admin-Password`): `/api/admin/check`, `/api/admin/wods[/{id}]`,
`/api/admin/athletes[/{id}]`, `/api/admin/scores`, `/api/admin/heats[/{id}]`.

## Локальный запуск

```bash
cd ~/crossfit-comp
./dev.sh
```
Скрипт сам поднимет PostgreSQL, бэкенд и туннель. После запуска:
- **Админка** (с ноутбука): http://localhost:8099/admin — пароль из `.env` (`ADMIN_PASSWORD`)
- **ТМА** (телеграм): ссылка `https://…trycloudflare.com` из вывода → в BotFather → Menu Button

Переменные — в `.env` (см. `.env.example`):
```
DATABASE_URL=postgresql://<user>@localhost:5432/crossfit_comp
ADMIN_PASSWORD=admin123
```

## Боевой запуск (Railway)

1. Новый репозиторий `crossfit-comp` на GitHub, залить файлы.
2. Railway: сервис из репо + **плагин PostgreSQL** (даёт `DATABASE_URL` автоматически).
3. Переменные сервиса: `ADMIN_PASSWORD` (обязательно смени!), `BOT_TOKEN`, `WEBAPP_URL`.
4. Админка будет на `https://<railway-домен>/admin`, ТМА — на нём же (`/`) или на GitHub Pages.

## Безопасность

Живой результат-бот (`crossfit-tma`) не затрагивается. Админка закрыта паролем
(`ADMIN_PASSWORD`); на проде смени дефолтный `admin123`.
