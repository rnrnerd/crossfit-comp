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
-- фото атлета (JPEG, сжатое на клиенте) + версия для сброса кэша
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS photo BYTEA;
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS photo_v INTEGER NOT NULL DEFAULT 0;
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
CREATE TABLE IF NOT EXISTS schedule (
    id       SERIAL PRIMARY KEY,
    day      TEXT NOT NULL DEFAULT '',
    time     TEXT NOT NULL DEFAULT '',
    title    TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    gender   TEXT NOT NULL DEFAULT ''
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
        # расписание сидим отдельно (таблица могла появиться позже)
        if await c.fetchval("SELECT COUNT(*) FROM schedule") == 0:
            for s in sample_data.SCHEDULE:
                await c.execute(
                    "INSERT INTO schedule (day,time,title,location,category,gender) VALUES ($1,$2,$3,$4,$5,$6)",
                    s.get("day", ""), s.get("time", ""), s.get("title", ""), s.get("location", ""),
                    s.get("category", ""), s.get("gender", ""))
            logger.info("Расписание засеяно демо")


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
    resp = _cors(web.json_response(data))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


def _splitlist(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


# Латинские буквы, визуально неотличимые от кириллических: в именах файлов
# и выгрузках они постоянно встречаются вперемешку («Сергей» с латинской C).
_HOMOGLYPHS = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "u": "и", "x": "х", "y": "у",
})
# zero-width space разделяет слова → в пробел; остальные невидимые просто убираем
_INVISIBLE = {0x200B: " "}
_INVISIBLE.update(dict.fromkeys([0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0x00AD], None))


def norm_name(s):
    """Нормализация ФИО для сопоставления: регистр, ё→е, невидимые символы,
    латинские двойники кириллицы, лишняя пунктуация и пробелы."""
    s = (s or "").translate(_INVISIBLE).lower().replace("ё", "е")
    s = s.translate(_HOMOGLYPHS)
    s = "".join(ch if (ch.isalpha() or ch.isspace()) else " " for ch in s)
    return " ".join(s.split())


def name_keys(s):
    """Ключи для матчинга: полное имя и «фамилия+имя» в отсортированном виде
    (порядок слов в базе бывает разный: «Иванов Иван» и «Иван Иванов»)."""
    n = norm_name(s)
    parts = n.split()
    keys = {n}
    if len(parts) >= 2:
        keys.add(" ".join(sorted(parts[:2])))
        keys.add(" ".join(sorted(parts)))
    return keys


def word_keys(s):
    """Значимые слова имени (от 4 букв) — для файлов вида «Задимидченко.jpg»
    или «IMG_2481 Задимидченко.jpg». Короткие слова и инициалы отбрасываем."""
    return {w for w in norm_name(s).split() if len(w) >= 4}


def _place_points(place):
    return max(0, 100 - 5 * (place - 1))


def rank_places(pairs, direction):
    """pairs: [(athlete_id, result)]. direction: 'asc' (меньше=лучше) / 'desc'.
    Возвращает {athlete_id: место}. Равные результаты делят лучшее место (1224)."""
    s = sorted(pairs, key=lambda x: x[1], reverse=(direction == "desc"))
    out, idx, n = {}, 0, len(s)
    while idx < n:
        j = idx
        while j < n and s[j][1] == s[idx][1]:
            j += 1
        place = idx + 1                        # место группы = idx+1
        for k in range(idx, j):
            out[s[k][0]] = place
        idx = j
    return out


async def _group_table(c, category, gender):
    """Считает по группе (категория+пол): сырые результаты + очки по каждому комплексу."""
    wods = await c.fetch("SELECT id,name,result_type,ord FROM wods ORDER BY ord,id")
    athletes = await c.fetch(
        """SELECT id, name, photo_v, (photo IS NOT NULL) AS has_photo
           FROM athletes WHERE category=$1 AND gender=$2 ORDER BY name""", category, gender)
    aids = [a["id"] for a in athletes]
    results = {aid: {} for aid in aids}
    if aids:
        srows = await c.fetch("SELECT athlete_id,wod_id,result FROM scores WHERE athlete_id = ANY($1::int[])", aids)
        for srow in srows:
            if srow["result"] is not None:
                results[srow["athlete_id"]][srow["wod_id"]] = srow["result"]
    points = {aid: {} for aid in aids}
    places = {aid: {} for aid in aids}
    for w in wods:
        direction = "asc" if w["result_type"] == "time" else "desc"
        pairs = [(aid, results[aid][w["id"]]) for aid in aids if w["id"] in results[aid]]
        for aid, place in rank_places(pairs, direction).items():
            places[aid][w["id"]] = place
            points[aid][w["id"]] = _place_points(place)
    return wods, athletes, results, points, places


def require_admin(request):
    return request.headers.get("X-Admin-Password", "") == ADMIN_PASSWORD


# ── Data access (DB или демо) ────────────────────────────────────────
async def get_leaderboard(category, gender):
    if not USE_DB:
        rows = [r for r in sample_data.LEADERBOARD
                if (not category or r["category"] == category) and (not gender or r["gender"] == gender)]
        return sorted(rows, key=lambda r: r.get("points", 0), reverse=True)
    async with db_pool.acquire() as c:
        wods, athletes, results, points, places = await _group_table(c, category, gender)
        out = []
        for a in athletes:
            aid = a["id"]
            total = sum(points[aid].values())
            wlist = [{
                "name": w["name"],
                "result_type": w["result_type"],
                "result": results[aid].get(w["id"]),    # сырой результат или None
                "place": places[aid].get(w["id"]),       # место или None
                "points": points[aid].get(w["id"], 0),
            } for w in wods]
            avatar = f"/api/photo/{aid}?v={a['photo_v']}" if a["has_photo"] else ""
            out.append({"name": a["name"], "category": category, "gender": gender,
                        "points": total, "avatar": avatar, "wods": wlist})
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
    if not USE_DB:
        return sample_data.SCHEDULE
    async with db_pool.acquire() as c:
        rows = await c.fetch("SELECT id,day,time,title,location,category,gender FROM schedule ORDER BY day,time,id")
        return [dict(r) for r in rows]


async def get_heats():
    if not USE_DB:
        return sample_data.HEATS
    async with db_pool.acquire() as c:
        hrows = await c.fetch("SELECT * FROM heats ORDER BY day, start_time, id")
        # карта «имя → фото» (в заходах атлеты хранятся по ФИО, а не по id)
        prows = await c.fetch(
            "SELECT id,name,photo_v FROM athletes WHERE photo IS NOT NULL")
        photo_by_key = {}
        for p in prows:
            for k in name_keys(p["name"]):
                photo_by_key.setdefault(k, f"/api/photo/{p['id']}?v={p['photo_v']}")
        out = []
        for h in hrows:
            ath = await c.fetch(
                "SELECT lane,name,category FROM heat_athletes WHERE heat_id=$1 ORDER BY lane", h["id"])
            alist = []
            for a in ath:
                avatar = ""
                for k in name_keys(a["name"]):
                    if k in photo_by_key:
                        avatar = photo_by_key[k]
                        break
                alist.append({"lane": a["lane"], "name": a["name"],
                              "category": a["category"], "avatar": avatar})
            out.append({"id": h["id"], "wod": h["wod"], "heat": h["heat"], "day": h["day"],
                        "category": h["category"], "gender": h["gender"],
                        "briefing_start": h["briefing_start"], "briefing_end": h["briefing_end"],
                        "start_time": h["start_time"], "location": h["location"],
                        "athletes": alist})
        return out


# ── Публичные эндпоинты ──────────────────────────────────────────────
async def h_health(r):   return web.Response(text="ok")


async def h_photo(r):
    """Фото атлета. Кэшируем надолго — URL содержит ?v=<photo_v>,
    поэтому при замене фото ссылка меняется и кэш сбрасывается сам."""
    if not USE_DB:
        return web.Response(status=404)
    try:
        aid = int(r.match_info["id"])
    except (KeyError, ValueError):
        return web.Response(status=404)
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT photo FROM athletes WHERE id=$1", aid)
    if not row or not row["photo"]:
        return web.Response(status=404)
    resp = web.Response(body=bytes(row["photo"]), content_type="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

async def h_stats(r):
    if r.method == "OPTIONS": return _cors(web.Response(status=204))
    if USE_DB:
        async with db_pool.acquire() as c:
            n = await c.fetchval("SELECT COUNT(*) FROM athletes")
    else:
        n = len(sample_data.LEADERBOARD)
    return _json({"athletes": int(n or 0)})

def _html_nocache(path):
    # Читаем файл и отдаём как обычный ответ БЕЗ ETag/Last-Modified —
    # чтобы Telegram-WebView не «ревалидировал» к старой закэшированной версии.
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return web.Response(text="not found", status=404)
    resp = web.Response(text=text, content_type="text/html", charset="utf-8")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

async def h_index(r):
    return _html_nocache(BASE_DIR / "index.html")

async def h_admin(r):
    return _html_nocache(BASE_DIR / "admin.html")

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


# Фото атлетов -------------------------------------------------------
async def a_photos(r):
    """GET  — список атлетов с признаком наличия фото.
       POST — массовая загрузка: [{filename, data(base64 jpeg)}], матчинг по имени файла.
       DELETE ?id=N — удалить фото атлета."""
    g = _admin_guard(r)
    if g is not None: return g

    if r.method == "GET":
        async with db_pool.acquire() as c:
            rows = await c.fetch(
                """SELECT id,name,category,gender,photo_v,(photo IS NOT NULL) AS has_photo
                   FROM athletes ORDER BY category,gender,name""")
        return _json([{"id": x["id"], "name": x["name"], "category": x["category"],
                       "gender": x["gender"], "has_photo": x["has_photo"],
                       "photo_v": x["photo_v"]} for x in rows])

    if r.method == "DELETE":
        try:
            aid = int(r.query.get("id", ""))
        except ValueError:
            return _json({"error": "bad_id"})
        async with db_pool.acquire() as c:
            await c.execute("UPDATE athletes SET photo=NULL WHERE id=$1", aid)
        return _json({"ok": True})

    # POST — массовая загрузка
    import base64
    d = await r.json()
    files = d.get("files") or []
    async with db_pool.acquire() as c:
        rows = await c.fetch("SELECT id,name FROM athletes")
        # два индекса: строгий (по имени целиком) и по отдельным словам (фамилия)
        index, windex = {}, {}
        for row in rows:
            for k in name_keys(row["name"]):
                index.setdefault(k, set()).add(row["id"])
            for w in word_keys(row["name"]):
                windex.setdefault(w, set()).add(row["id"])

        matched, unmatched, ambiguous = [], [], []
        for f in files:
            fname = (f.get("filename") or "").rsplit(".", 1)[0]
            data = f.get("data") or ""
            hit, amb = None, False
            # 1) строгое совпадение имени
            for k in sorted(name_keys(fname), key=len, reverse=True):
                ids = index.get(k)
                if ids and len(ids) == 1:
                    hit = next(iter(ids))
                    break
                if ids and len(ids) > 1:
                    amb = True
            # 2) по значимым словам: атлет, у которого есть ВСЕ слова из имени файла
            if hit is None:
                cand = None
                for w in word_keys(fname):
                    ids = windex.get(w, set())
                    cand = set(ids) if cand is None else (cand & ids)
                if cand:
                    if len(cand) == 1:
                        hit = next(iter(cand))
                    else:
                        amb = True
            if hit is None:
                if amb:
                    ambiguous.append(fname)
                else:
                    # диагностика: показываем ближайшие имена из базы
                    import difflib
                    near = difflib.get_close_matches(
                        norm_name(fname), [norm_name(x["name"]) for x in rows], n=3, cutoff=0.4)
                    near_orig = [x["name"] for x in rows if norm_name(x["name"]) in near]
                    unmatched.append({"file": fname, "near": near_orig,
                                      "normalized": norm_name(fname)})
                continue
            try:
                raw = base64.b64decode(data.split(",", 1)[-1])
            except Exception:
                unmatched.append(fname)
                continue
            await c.execute(
                "UPDATE athletes SET photo=$2, photo_v=photo_v+1 WHERE id=$1", hit, raw)
            matched.append(fname)

    return _json({"ok": True, "matched": len(matched), "total_athletes": len(rows),
                  "unmatched": unmatched, "ambiguous": ambiguous})

# Scores (сетка атлет × комплекс) -----------------------------------
async def a_scores(r):
    g = _admin_guard(r)
    if g is not None: return g
    if r.method == "GET":
        cat = r.query.get("category", "").strip(); gen = r.query.get("gender", "").strip()
        async with db_pool.acquire() as c:
            wods, athletes, results, points, places = await _group_table(c, cat, gen)
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

# Расписание ---------------------------------------------------------
async def a_sched_create(r):
    g = _admin_guard(r);  return g if g is not None else await _sched_upsert(r, None)
async def a_sched_item(r):
    g = _admin_guard(r)
    if g is not None: return g
    if r.method == "DELETE":
        async with db_pool.acquire() as c:
            await c.execute("DELETE FROM schedule WHERE id=$1", int(r.match_info["id"]))
        return _json({"ok": True})
    return await _sched_upsert(r, int(r.match_info["id"]))
async def _sched_upsert(r, sid):
    d = await r.json()
    vals = (d.get("day", ""), d.get("time", ""), d.get("title", ""), d.get("location", ""),
            d.get("category", ""), d.get("gender", ""))
    async with db_pool.acquire() as c:
        if sid is None:
            sid = await c.fetchval(
                """INSERT INTO schedule (day,time,title,location,category,gender)
                   VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""", *vals)
        else:
            await c.execute(
                "UPDATE schedule SET day=$2,time=$3,title=$4,location=$5,category=$6,gender=$7 WHERE id=$1",
                sid, *vals)
    return _json({"ok": True, "id": sid})
# ── Сборка приложения ────────────────────────────────────────────────
def build_web_app():
    # client_max_size — под пачки фото при массовой загрузке (base64)
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app.router.add_get("/", h_index)
    app.router.add_get("/admin", h_admin)
    app.router.add_get("/healthz", h_health)
    app.router.add_get("/api/photo/{id}", h_photo)
    for path, h in [("/api/leaderboard", h_leaderboard), ("/api/wods", h_wods),
                    ("/api/schedule", h_schedule), ("/api/heats", h_heats), ("/api/stats", h_stats)]:
        app.router.add_get(path, h)
        app.router.add_options(path, h)

    # admin (каждый путь — один хендлер, диспетчеризация по методу внутри)
    app.router.add_route("*", "/api/admin/check", a_check)
    app.router.add_route("*", "/api/admin/wods", a_wod_create)          # POST
    app.router.add_route("*", "/api/admin/wods/{id}", a_wod_item)       # PUT / DELETE
    app.router.add_route("*", "/api/admin/athletes", a_athletes)        # GET / POST
    app.router.add_route("*", "/api/admin/athletes/{id}", a_athlete_item)  # DELETE
    app.router.add_route("*", "/api/admin/photos", a_photos)            # GET / POST / DELETE
    app.router.add_route("*", "/api/admin/scores", a_scores)            # GET / POST
    app.router.add_route("*", "/api/admin/heats", a_heat_create)        # POST
    app.router.add_route("*", "/api/admin/heats/{id}", a_heat_item)     # PUT / DELETE
    app.router.add_route("*", "/api/admin/schedule", a_sched_create)    # POST
    app.router.add_route("*", "/api/admin/schedule/{id}", a_sched_item) # PUT / DELETE

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
