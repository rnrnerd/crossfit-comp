"""
Битва за Херсонес 2026 — СОРЕВНОВАТЕЛЬНЫЙ ТМА (backend + админка)

Хранилище: PostgreSQL (Railway). Если DATABASE_URL не задан — отдаём демо-данные
из sample_data.py (для разработки фронта без БД).

Публичное API (для ТМА):
  GET /                    — сам ТМА (index.html)
  GET /admin               — админ-панель (admin.html)
  GET /healthz             — health
  GET /api/leaderboard     — лидерборд (?category=&gender=)
  GET /api/wods            — комплексы
  GET /api/schedule        — расписание
  GET /api/heats           — заходы

Админ API (требует заголовок X-Admin-Password = ADMIN_PASSWORD):
  POST /api/admin/check
  POST/PUT/DELETE /api/admin/wods[/{id}]
  GET/POST/DELETE /api/admin/athletes[/{id}]
  GET/POST /api/admin/scores
  POST/PUT/DELETE /api/admin/heats[/{id}]
"""

import os
import logging
from pathlib import Path
from aiohttp import web
from dotenv import load_dotenv

import sample_data

BASE_DIR = Path(__file__).resolve().parent
load_dotenv()

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL     = os.getenv("WEBAPP_URL", "").strip()
DATABASE_URL   = os.getenv("DATABASE_URL", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123").strip()

USE_DB = bool(DATABASE_URL)
db_pool = None

if USE_DB:
    import asyncpg
    logger.info("Storage: PostgreSQL")
else:
    logger.info("Storage: демо-данные (sample_data.py) — DATABASE_URL не задан")


# ── Схема + сидинг ───────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS athletes (
    id       SERIAL PRIMARY KEY,
    name     TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    gender   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS wods (
    id          SERIAL PRIMARY KEY,
    ord         INTEGER NOT NULL DEFAULT 0,
    name        TEXT NOT NULL,
    scoring     TEXT NOT NULL DEFAULT '',
    time_cap    TEXT NOT NULL DEFAULT '',
    day         TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    categories  TEXT NOT NULL DEFAULT '',
    genders     TEXT NOT NULL DEFAULT '',
    result_type TEXT NOT NULL DEFAULT 'reps'   -- time | weight | reps
);
-- scores.result — сырой результат атлета за комплекс (секунды / кг / повторы)
CREATE TABLE IF NOT EXISTS scores (
    athlete_id INTEGER REFERENCES athletes(id) ON DELETE CASCADE,
    wod_id     INTEGER REFERENCES wods(id) ON DELETE CASCADE,
    result     DOUBLE PRECISION,
    PRIMARY KEY (athlete_id, wod_id)
);
-- миграции для уже существующих баз
ALTER TABLE wods ADD COLUMN IF NOT EXISTS result_type TEXT NOT NULL DEFAULT 'reps';
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='scores' AND column_name='points') THEN
        ALTER TABLE scores RENAME COLUMN points TO result;
        ALTER TABLE scores ALTER COLUMN result DROP NOT NULL;
        ALTER TABLE scores ALTER COLUMN result DROP DEFAULT;
        UPDATE scores SET result = NULL;   -- старые «очки» больше не валидны как сырой результат
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS heats (
    id             SERIAL PRIMARY KEY,
    wod            TEXT NOT NULL DEFAULT '',
    heat           INTEGER NOT NULL DEFAULT 1,
    day            TEXT NOT NULL DEFAULT '',
    category       TEXT NOT NULL DEFAULT '',
    gender         TEXT NOT NULL DEFAULT '',
    briefing_start TEXT NOT NULL DEFAULT '',
    briefing_end   TEXT NOT NULL DEFAULT '',
    start_time     TEXT NOT NULL DEFAULT '',
    location       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS heat_athletes (
    id       SERIAL PRIMARY KEY,
    heat_id  INTEGER REFERENCES heats(id) ON DELETE CASCADE,
    lane     INTEGER NOT NULL DEFAULT 0,
    name     TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT ''
);
"""


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as c:
        await c.execute(SCHEMA)
        n = await c.fetchval("SELECT COUNT(*) FROM wods")
        if n == 0:
            await _seed(c)
            logger.info("DB засеяна демо-данными")


async def _seed(c):
    # wods
    for w in sample_data.WODS:
        await c.execute(
            """INSERT INTO wods (ord,name,scoring,time_cap,day,description,categories,genders,result_type)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            w["order"], w["name"], w["scoring"], w["time_cap"], w["day"], w["description"],
            ",".join(w.get("categories", [])), ",".join(w.get("genders", [])), w.get("result_type", "reps"))
    # athletes (демо; сырые результаты вносят судьи)
    for a in sample_data.LEADERBOARD:
        await c.execute(
            "INSERT INTO athletes (name,category,gender) VALUES ($1,$2,$3)",
            a["name"], a["category"], a["gender"])
    # heats
    for h in sample_data.HEATS:
        hid = await c.fetchval(
            """INSERT INTO heats (wod,heat,day,category,gender,briefing_start,briefing_end,start_time,location)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
            h["wod"], h["heat"], h.get("day", ""), h.get("category", ""), h.get("gender", ""),
            h.get("briefing_start", ""), h.get("briefing_end", ""), h.get("start_time", ""), h.get("location", ""))
        for at in h.get("athletes", []):
            await c.execute(
                "INSERT INTO heat_athletes (heat_id,lane,name,category) VALUES ($1,$2,$3,$4)",
                hid, at.get("lane", 0), at.get("name", ""), at.get("category", ""))


# ── CORS / helpers ───────────────────────────────────────────────────
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Password"
    return resp


def _json(data):
    return _cors(web.json_response(data))


def _splitlist(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _place_points(place):
    return max(0, 100 - 5 * (place - 1))


def rank_points(pairs, direction):
    """pairs: [(athlete_id, result)]. direction: 'asc' (меньше=лучше) / 'desc'.
    Возвращает {athlete_id: очки}. Равные результаты делят лучшее место (1224)."""
    s = sorted(pairs, key=lambda x: x[1], reverse=(direction == "desc"))
    out, idx, n = {}, 0, len(s)
    while idx < n:
        j = idx
        while j < n and s[j][1] == s[idx][1]:
            j += 1
        pts = _place_points(idx + 1)          # место группы = idx+1
        for k in range(idx, j):
            out[s[k][0]] = pts
        idx = j
    return out


async def _group_table(c, category, gender):
    """Считает по группе (категория+пол): сырые результаты + очки по каждому комплексу."""
    wods = await c.fetch("SELECT id,name,result_type,ord FROM wods ORDER BY ord,id")
    athletes = await c.fetch("SELECT id,name FROM athletes WHERE category=$1 AND gender=$2 ORDER BY name",
                             category, gender)
    aids = [a["id"] for a in athletes]
    results = {aid: {} for aid in aids}
    if aids:
        srows = await c.fetch("SELECT athlete_id,wod_id,result FROM scores WHERE athlete_id = ANY($1::int[])", aids)
        for srow in srows:
            if srow["result"] is not None:
                results[srow["athlete_id"]][srow["wod_id"]] = srow["result"]
    points = {aid: {} for aid in aids}
    for w in wods:
        direction = "asc" if w["result_type"] == "time" else "desc"
        pairs = [(aid, results[aid][w["id"]]) for aid in aids if w["id"] in results[aid]]
        for aid, p in rank_points(pairs, direction).items():
            points[aid][w["id"]] = p
    return wods, athletes, results, points


def require_admin(request):
    return request.headers.get("X-Admin-Password", "") == ADMIN_PASSWORD


# ── Data access (DB или демо) ────────────────────────────────────────
async def get_leaderboard(category, gender):
    if not USE_DB:
        rows = [r for r in sample_data.LEADERBOARD
                if (not category or r["category"] == category) and (not gender or r["gender"] == gender)]
        return sorted(rows, key=lambda r: r.get("points", 0), reverse=True)
    async with db_pool.acquire() as c:
        wods, athletes, results, points = await _group_table(c, category, gender)
        wname = {w["id"]: w["name"] for w in wods}
        out = []
        for a in athletes:
            pmap = points[a["id"]]
            total = sum(pmap.values())
            out.append({"name": a["name"], "category": category, "gender": gender,
                        "points": total, "avatar": "",
                        "wods": {wname[wid]: pts for wid, pts in pmap.items()}})
        out.sort(key=lambda r: r["points"], reverse=True)
        return out


async def get_wods():
    if not USE_DB:
        return sample_data.WODS
    async with db_pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM wods ORDER BY ord, id")
        return [{"id": r["id"], "order": r["ord"], "name": r["name"], "scoring": r["scoring"],
                 "time_cap": r["time_cap"], "day": r["day"], "description": r["description"],
                 "result_type": r["result_type"],
                 "categories": _splitlist(r["categories"]), "genders": _splitlist(r["genders"])} for r in rows]


async def get_schedule():
    # Расписание пока вне админки — отдаём из sample_data.
    return sample_data.SCHEDULE


async def get_heats():
    if not USE_DB:
        return sample_data.HEATS
    async with db_pool.acquire() as c:
        hrows = await c.fetch("SELECT * FROM heats ORDER BY day, start_time, id")
        out = []
        for h in hrows:
            ath = await c.fetch(
                "SELECT lane,name,category FROM heat_athletes WHERE heat_id=$1 ORDER BY lane", h["id"])
            out.append({"id": h["id"], "wod": h["wod"], "heat": h["heat"], "day": h["day"],
                        "category": h["category"], "gender": h["gender"],
                        "briefing_start": h["briefing_start"], "briefing_end": h["briefing_end"],
                        "start_time": h["start_time"], "location": h["location"],
                        "athletes": [{"lane": a["lane"], "name": a["name"], "category": a["category"]} for a in ath]})
        return out


# ── Публичные эндпоинты ──────────────────────────────────────────────
async def h_health(r):   return web.Response(text="ok")

async def h_index(r):
    f = BASE_DIR / "index.html"
    return web.FileResponse(f) if f.exists() else web.Response(text="index.html not found", status=404)

async def h_admin(r):
    f = BASE_DIR / "admin.html"
    return web.FileResponse(f) if f.exists() else web.Response(text="admin.html not found", status=404)

async def h_leaderboard(r):
    if r.method == "OPTIONS": return _cors(web.Response(status=204))
    return _json(await get_leaderboard(r.query.get("category", "").strip(), r.query.get("gender", "").strip()))

async def h_wods(r):
    if r.method == "OPTIONS": return _cors(web.Response(status=204))
    return _json(await get_wods())

async def h_schedule(r):
    if r.method == "OPTIONS": return _cors(web.Response(status=204))
    return _json(await get_schedule())

async def h_heats(r):
    if r.method == "OPTIONS": return _cors(web.Response(status=204))
    return _json(await get_heats())


# ── Админ-эндпоинты ──────────────────────────────────────────────────
def _admin_guard(r):
    if r.method == "OPTIONS":
        return _cors(web.Response(status=204))
    if not require_admin(r):
        return _json({"error": "unauthorized"})
    return None

async def a_check(r):
    if r.method == "OPTIONS": return _cors(web.Response(status=204))
    body = await r.json()
    return _json({"ok": body.get("password", "") == ADMIN_PASSWORD})

# WODs --------------------------------------------------------------
async def a_wod_create(r):
    g = _admin_guard(r);  return g if g is not None else await _wod_upsert(r, None)
async def a_wod_item(r):
    g = _admin_guard(r)
    if g is not None: return g
    if r.method == "DELETE":
        async with db_pool.acquire() as c:
            await c.execute("DELETE FROM wods WHERE id=$1", int(r.match_info["id"]))
        return _json({"ok": True})
    return await _wod_upsert(r, int(r.match_info["id"]))
async def _wod_upsert(r, wid):
    d = await r.json()
    cats = ",".join(d.get("categories", [])) if isinstance(d.get("categories"), list) else (d.get("categories") or "")
    gens = ",".join(d.get("genders", [])) if isinstance(d.get("genders"), list) else (d.get("genders") or "")
    rtype = d.get("result_type", "reps")
    if rtype not in ("time", "weight", "reps"):
        rtype = "reps"
    async with db_pool.acquire() as c:
        if wid is None:
            wid = await c.fetchval(
                """INSERT INTO wods (ord,name,scoring,time_cap,day,description,categories,genders,result_type)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                int(d.get("order", 0) or 0), d.get("name", ""), d.get("scoring", ""), d.get("time_cap", ""),
                d.get("day", ""), d.get("description", ""), cats, gens, rtype)
        else:
            await c.execute(
                """UPDATE wods SET ord=$2,name=$3,scoring=$4,time_cap=$5,day=$6,description=$7,
                   categories=$8,genders=$9,result_type=$10 WHERE id=$1""",
                wid, int(d.get("order", 0) or 0), d.get("name", ""), d.get("scoring", ""), d.get("time_cap", ""),
                d.get("day", ""), d.get("description", ""), cats, gens, rtype)
    return _json({"ok": True, "id": wid})
# Athletes ----------------------------------------------------------
async def a_athletes(r):
    g = _admin_guard(r)
    if g is not None: return g
    if r.method == "GET":
        cat = r.query.get("category", "").strip(); gen = r.query.get("gender", "").strip()
        async with db_pool.acquire() as c:
            if cat or gen:
                rows = await c.fetch("SELECT id,name,category,gender FROM athletes WHERE ($1='' OR category=$1) AND ($2='' OR gender=$2) ORDER BY name", cat, gen)
            else:
                rows = await c.fetch("SELECT id,name,category,gender FROM athletes ORDER BY name")
        return _json([dict(x) for x in rows])
    # POST create
    d = await r.json()
    async with db_pool.acquire() as c:
        aid = await c.fetchval("INSERT INTO athletes (name,category,gender) VALUES ($1,$2,$3) RETURNING id",
                               d.get("name", ""), d.get("category", ""), d.get("gender", ""))
    return _json({"ok": True, "id": aid})
async def a_athlete_item(r):
    g = _admin_guard(r)
    if g is not None: return g
    async with db_pool.acquire() as c:
        await c.execute("DELETE FROM athletes WHERE id=$1", int(r.match_info["id"]))
    return _json({"ok": True})

# Scores (сетка атлет × комплекс) -----------------------------------
async def a_scores(r):
    g = _admin_guard(r)
    if g is not None: return g
    if r.method == "GET":
        cat = r.query.get("category", "").strip(); gen = r.query.get("gender", "").strip()
        async with db_pool.acquire() as c:
            wods, athletes, results, points = await _group_table(c, cat, gen)
        res = []
        for a in athletes:
            aid = a["id"]
            res.append({"id": aid, "name": a["name"],
                        "results": {str(wid): v for wid, v in results[aid].items()},
                        "points": {str(wid): p for wid, p in points[aid].items()},
                        "total": sum(points[aid].values())})
        return _json({"wods": [{"id": w["id"], "name": w["name"], "result_type": w["result_type"]} for w in wods],
                      "athletes": res})
    # POST — сохранить сырой результат ячейки (или очистить, если result null/'')
    d = await r.json()
    aid = int(d["athlete_id"]); wid = int(d["wod_id"]); raw = d.get("result", None)
    async with db_pool.acquire() as c:
        if raw is None or raw == "":
            await c.execute("DELETE FROM scores WHERE athlete_id=$1 AND wod_id=$2", aid, wid)
            return _json({"ok": True, "result": None})
        try:
            val = max(0.0, float(raw))
        except (TypeError, ValueError):
            return _json({"error": "bad_result"})
        await c.execute(
            """INSERT INTO scores (athlete_id,wod_id,result) VALUES ($1,$2,$3)
               ON CONFLICT (athlete_id,wod_id) DO UPDATE SET result=EXCLUDED.result""",
            aid, wid, val)
    return _json({"ok": True, "result": val})

# Heats -------------------------------------------------------------
async def a_heat_create(r):
    g = _admin_guard(r);  return g if g is not None else await _heat_upsert(r, None)
async def a_heat_item(r):
    g = _admin_guard(r)
    if g is not None: return g
    if r.method == "DELETE":
        async with db_pool.acquire() as c:
            await c.execute("DELETE FROM heats WHERE id=$1", int(r.match_info["id"]))
        return _json({"ok": True})
    return await _heat_upsert(r, int(r.match_info["id"]))
async def _heat_upsert(r, hid):
    d = await r.json()
    async with db_pool.acquire() as c:
        if hid is None:
            hid = await c.fetchval(
                """INSERT INTO heats (wod,heat,day,category,gender,briefing_start,briefing_end,start_time,location)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                d.get("wod", ""), int(d.get("heat", 1) or 1), d.get("day", ""), d.get("category", ""),
                d.get("gender", ""), d.get("briefing_start", ""), d.get("briefing_end", ""),
                d.get("start_time", ""), d.get("location", ""))
        else:
            await c.execute(
                """UPDATE heats SET wod=$2,heat=$3,day=$4,category=$5,gender=$6,briefing_start=$7,
                   briefing_end=$8,start_time=$9,location=$10 WHERE id=$1""",
                hid, d.get("wod", ""), int(d.get("heat", 1) or 1), d.get("day", ""), d.get("category", ""),
                d.get("gender", ""), d.get("briefing_start", ""), d.get("briefing_end", ""),
                d.get("start_time", ""), d.get("location", ""))
        await c.execute("DELETE FROM heat_athletes WHERE heat_id=$1", hid)
        for at in d.get("athletes", []):
            await c.execute("INSERT INTO heat_athletes (heat_id,lane,name,category) VALUES ($1,$2,$3,$4)",
                            hid, int(at.get("lane", 0) or 0), at.get("name", ""), at.get("category", ""))
    return _json({"ok": True, "id": hid})
# ── Сборка приложения ────────────────────────────────────────────────
def build_web_app():
    app = web.Application()
    app.router.add_get("/", h_index)
    app.router.add_get("/admin", h_admin)
    app.router.add_get("/healthz", h_health)
    for path, h in [("/api/leaderboard", h_leaderboard), ("/api/wods", h_wods),
                    ("/api/schedule", h_schedule), ("/api/heats", h_heats)]:
        app.router.add_get(path, h)
        app.router.add_options(path, h)

    # admin (каждый путь — один хендлер, диспетчеризация по методу внутри)
    app.router.add_route("*", "/api/admin/check", a_check)
    app.router.add_route("*", "/api/admin/wods", a_wod_create)          # POST
    app.router.add_route("*", "/api/admin/wods/{id}", a_wod_item)       # PUT / DELETE
    app.router.add_route("*", "/api/admin/athletes", a_athletes)        # GET / POST
    app.router.add_route("*", "/api/admin/athletes/{id}", a_athlete_item)  # DELETE
    app.router.add_route("*", "/api/admin/scores", a_scores)            # GET / POST
    app.router.add_route("*", "/api/admin/heats", a_heat_create)        # POST
    app.router.add_route("*", "/api/admin/heats/{id}", a_heat_item)     # PUT / DELETE

    assets = BASE_DIR / "assets"
    if assets.exists():
        app.router.add_static("/assets/", assets, show_index=False)
    return app


def main():
    port = int(os.environ.get("PORT", "8080"))
    if BOT_TOKEN:
        from telegram import Update, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup
        from telegram.ext import Application, CommandHandler, ContextTypes

        async def start(update, ctx):
            kb = ReplyKeyboardMarkup(
                [[KeyboardButton("🏟 Открыть соревнование", web_app=WebAppInfo(url=WEBAPP_URL))]],
                resize_keyboard=True) if WEBAPP_URL else None
            await update.message.reply_text("Битва за Херсонес 2026 — соревнование", reply_markup=kb)

        async def post_init(app):
            if USE_DB: await init_db()
            runner = web.AppRunner(build_web_app())
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", port).start()
            logger.info("HTTP server on %s", port)

        app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
        app.add_handler(CommandHandler("start", start))
        logger.info("Bot + API starting")
        app.run_polling(drop_pending_updates=True)
    else:
        async def on_startup(app):
            if USE_DB: await init_db()
        a = build_web_app()
        a.on_startup.append(on_startup)
        logger.info("API only on %s", port)
        web.run_app(a, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
