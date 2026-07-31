"""
Бэкенд дневника тренировок
FastAPI + SQLite

Поддерживает несколько профилей на одного Telegram-пользователя
(например тренер ведёт дневники нескольких клиентов) и систему друзей,
где можно посмотреть "основной" профиль друга согласно его настройкам видимости.
"""

from fastapi import FastAPI, HTTPException, Header, Depends, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
import sqlite3, json, hmac, hashlib, urllib.parse, urllib.request, os, secrets, shutil, time, html, datetime, io

app = FastAPI()

# ── CORS — разрешаем запросы с GitHub Pages ──────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # после тестов замени на свой домен
    allow_methods=["*"],
    allow_headers=["*"],
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH   = os.environ.get("DB_PATH", "gym.db")

# ── Бот больше не отдельный сервис на long polling — см. секцию "Telegram-бот
# (вебхук)" ниже. WEBAPP_URL — та же ссылка на GitHub Pages, что раньше была
# у отдельного bot.py. TELEGRAM_WEBHOOK_SECRET — случайная строка в пути
# вебхука, чтобы никто посторонний не мог слать сюда фейковые "обновления".
# PUBLIC_URL — публичный домен ЭТОГО бэкенда на Railway (тот же, что
# VITE_API_URL у фронтенда) — если задан, при старте сам регистрирует вебхук
# у Telegram; если не задан — вебхук нужно один раз включить вручную (см.
# инструкцию по деплою).
WEBAPP_URL              = os.environ.get("WEBAPP_URL", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
PUBLIC_URL              = os.environ.get("PUBLIC_URL", "").rstrip("/")


# ── База данных ───────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_profile_and_user(conn, uid, username=None, first_name=None):
    """
    Гарантирует, что у Telegram-пользователя есть запись в users и хотя бы
    один профиль. Если он новый — создаётся "Профиль 1" (основной, всё видно).
    Возвращает active_profile_id.
    """
    row = conn.execute("SELECT active_profile_id FROM users WHERE user_id=?", (uid,)).fetchone()

    if row:
        if username is not None or first_name is not None:
            conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (username, first_name, uid)
            )
        if row["active_profile_id"]:
            return row["active_profile_id"]

    # нет активного профиля — ищем существующий или создаём "Профиль 1"
    existing = conn.execute(
        "SELECT id FROM profiles WHERE owner_id=? ORDER BY id LIMIT 1", (uid,)
    ).fetchone()
    if existing:
        pid = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO profiles (owner_id, name, is_main, show_workouts, show_exercises, show_comments, show_measurements) "
            "VALUES (?,?,1,1,1,1,1)",
            (uid, "Профиль 1")
        )
        pid = cur.lastrowid

    if row:
        conn.execute("UPDATE users SET active_profile_id=? WHERE user_id=?", (pid, uid))
    else:
        code = secrets.token_hex(4)
        conn.execute(
            "INSERT INTO users (user_id, username, first_name, active_profile_id, invite_code) VALUES (?,?,?,?,?)",
            (uid, username, first_name, pid, code)
        )
    return pid


def migrate_legacy_data(conn):
    """
    Старые тренировки/замеры были привязаны только к user_id (без профилей).
    Создаём дефолтный профиль каждому такому владельцу и проставляем profile_id.
    Безопасно запускать многократно — трогает только строки с profile_id IS NULL.
    """
    owners = set()
    for r in conn.execute("SELECT DISTINCT user_id FROM workouts WHERE profile_id IS NULL").fetchall():
        owners.add(r["user_id"])
    for r in conn.execute("SELECT DISTINCT user_id FROM measurements WHERE profile_id IS NULL").fetchall():
        owners.add(r["user_id"])
    for uid in owners:
        pid = ensure_profile_and_user(conn, uid)
        conn.execute("UPDATE workouts SET profile_id=? WHERE user_id=? AND profile_id IS NULL", (pid, uid))
        conn.execute("UPDATE measurements SET profile_id=? WHERE user_id=? AND profile_id IS NULL", (pid, uid))


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workouts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                name       TEXT NOT NULL,
                date       TEXT NOT NULL,
                exercises  TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS measurements (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                name    TEXT NOT NULL,
                date    TEXT NOT NULL,
                data    TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS templates (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                profile_id INTEGER NOT NULL,
                name       TEXT NOT NULL,
                exercises  TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS profiles (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id       TEXT NOT NULL,
                name           TEXT NOT NULL,
                is_main        INTEGER NOT NULL DEFAULT 0,
                show_workouts  INTEGER NOT NULL DEFAULT 1,
                show_exercises INTEGER NOT NULL DEFAULT 1,
                show_comments  INTEGER NOT NULL DEFAULT 1,
                created_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id           TEXT PRIMARY KEY,
                username          TEXT,
                first_name        TEXT,
                active_profile_id INTEGER,
                invite_code       TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS friends (
                user_a     TEXT NOT NULL,
                user_b     TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_a, user_b)
            );
            CREATE TABLE IF NOT EXISTS exercise_notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                name_lc    TEXT NOT NULL,
                note       TEXT NOT NULL DEFAULT '',
                UNIQUE(profile_id, name_lc)
            );
            CREATE TABLE IF NOT EXISTS progressions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id       INTEGER NOT NULL,
                exercise_name    TEXT NOT NULL,
                exercise_name_lc TEXT NOT NULL,
                mode             TEXT NOT NULL DEFAULT 'calculated',
                status           TEXT NOT NULL DEFAULT 'active',

                exercise_type    TEXT,
                goal             TEXT,
                rep_unit         TEXT NOT NULL DEFAULT 'reps',
                rep_range_low    INTEGER,
                rep_range_high   INTEGER,
                frequency        INTEGER,
                sets_count       INTEGER,
                increment        REAL,

                start_weight     REAL,
                start_reps       INTEGER,
                start_rir        REAL,
                total_sessions   INTEGER,
                deload_enabled   INTEGER NOT NULL DEFAULT 0,

                current_weight   REAL,
                current_reps     INTEGER,
                current_reps_detail TEXT,
                unilateral       INTEGER NOT NULL DEFAULT 0,
                fail_streak      INTEGER NOT NULL DEFAULT 0,

                created_at       TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_progressions_active_unique
                ON progressions(profile_id, exercise_name_lc) WHERE status = 'active';
            CREATE TABLE IF NOT EXISTS progression_sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                progression_id  INTEGER NOT NULL,
                session_index   INTEGER NOT NULL,
                role            TEXT,

                planned_weight  REAL NOT NULL,
                planned_reps    INTEGER NOT NULL,
                planned_sets    INTEGER NOT NULL,

                status          TEXT NOT NULL DEFAULT 'pending',
                workout_id      INTEGER,
                actual_weight   REAL,
                actual_reps     INTEGER,
                actual_sets     INTEGER,
                actual_rir      REAL,
                completed_at    TEXT,
                planned_detail  TEXT,
                actual_detail   TEXT,

                UNIQUE(progression_id, session_index)
            );
        """)
        # Миграция: добавляем profile_id в старые таблицы, если его ещё нет
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(progressions)").fetchall()]
        if "current_reps_detail" not in cols:
            conn.execute("ALTER TABLE progressions ADD COLUMN current_reps_detail TEXT")
        if "unilateral" not in cols:
            conn.execute("ALTER TABLE progressions ADD COLUMN unilateral INTEGER NOT NULL DEFAULT 0")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(workouts)").fetchall()]
        if "profile_id" not in cols:
            conn.execute("ALTER TABLE workouts ADD COLUMN profile_id INTEGER")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(measurements)").fetchall()]
        if "profile_id" not in cols:
            conn.execute("ALTER TABLE measurements ADD COLUMN profile_id INTEGER")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(profiles)").fetchall()]
        if "show_measurements" not in cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN show_measurements INTEGER NOT NULL DEFAULT 1")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "is_premium" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(progression_sessions)").fetchall()]
        if "planned_detail" not in cols:
            conn.execute("ALTER TABLE progression_sessions ADD COLUMN planned_detail TEXT")
        if "actual_detail" not in cols:
            conn.execute("ALTER TABLE progression_sessions ADD COLUMN actual_detail TEXT")

        migrate_legacy_data(conn)

    print("✅ База данных готова")

init_db()


# ── Проверка подписи Telegram ─────────────────────────────────────────────────

def parse_telegram_user(init_data: str) -> Optional[dict]:
    """
    Проверяет подпись initData от Telegram и возвращает данные пользователя
    {id, username, first_name}. Возвращает None если подпись неверна.
    """
    if not BOT_TOKEN:
        try:
            params = dict(urllib.parse.parse_qsl(init_data))
            user   = json.loads(params.get("user", "{}"))
            return {
                "id": str(user.get("id", "dev_user")),
                "username": user.get("username"),
                "first_name": user.get("first_name", "Тестер"),
            }
        except Exception:
            return {"id": "dev_user", "username": None, "first_name": "Тестер"}

    try:
        params     = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        hash_value = params.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed   = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed, hash_value):
            return None

        user = json.loads(params.get("user", "{}"))
        return {
            "id": str(user.get("id")),
            "username": user.get("username"),
            "first_name": user.get("first_name", "Пользователь"),
        }
    except Exception:
        return None


# ── Dependency: получить user_id из заголовка (и обновить его профиль/юзернейм) ──

def get_user_id(x_init_data: str = Header(...)) -> str:
    info = parse_telegram_user(x_init_data)
    if not info or not info.get("id"):
        raise HTTPException(status_code=401, detail="Invalid Telegram auth")
    uid = info["id"]
    with get_db() as conn:
        ensure_profile_and_user(conn, uid, info.get("username"), info.get("first_name"))
    return uid


def get_active_profile_id(conn, uid: str) -> int:
    return ensure_profile_and_user(conn, uid)


def normalize_pair(a: str, b: str):
    return (a, b) if a < b else (b, a)


# ── Прогрессия: чистые расчётные функции ─────────────────────────────────────
# Никакого состояния и никаких обращений к БД — только математика. Специально
# отделены от роутов, чтобы их можно было прогнать на фейковых данных без
# поднятия сервера (см. tests в конце файла / отдельный тест-скрипт).
#
# Состояние прогрессии — не одно число повторов, а список повторов по каждому
# рабочему подходу (reps_detail), одной длины с sets_count. Вес один на всю
# сессию (кроме лёгкого дня — см. ниже, там тоже один вес, просто ниже цель
# по повторам). Причина: разные подходы "не сгорают" одинаково — если 3-й
# подход не дотянул, это не должно портить прогресс по 1-му и 2-му.

VARYING_TYPES = ("main_compound", "accessory_compound")  # у этих типов есть роли heavy/light
ROLE_TEMPLATES = {
    1: ["heavy"],
    2: ["heavy", "light"],
    3: ["heavy", "light", "heavy"],
    4: ["heavy", "light", "heavy", "light"],
}
LIGHT_REPS_PCT = 0.55  # лёгкий день: тот же вес, повторы — доля от цели тяжёлого дня (~RIR 3-4)


def round_to_increment(weight: float, increment: float) -> float:
    if not increment:
        return weight
    return round(round(weight / increment) * increment, 3)


def role_template(exercise_type: str, frequency: int):
    if exercise_type in VARYING_TYPES:
        return ROLE_TEMPLATES.get(max(1, min(frequency or 1, 4)), ROLE_TEMPLATES[1])
    return [None]  # изоляция/произвольное — без вариации по дням, каждая сессия "как heavy"


def role_adjust_detail(role: str, anchor_weight: float, anchor_reps_detail: list):
    """Вес/повторы по подходам для light-сессии недели, от heavy-цели этой же недели (anchor)."""
    if role != "light":
        return anchor_weight, list(anchor_reps_detail)
    light_reps = [max(1, round(r * LIGHT_REPS_PCT)) for r in anchor_reps_detail]
    return anchor_weight, light_reps


def climb_detail(weight: float, reps_detail: list, low: int, high: int, increment: float):
    """Шаг двойной прогрессии на границе недели (используется для ОПТИМИСТИЧНОГО предпоказа
    цели до того, как сессия реально отыграна — реальный факт потом переопределит анкор
    через adapt_actual_detail)."""
    if all(r >= high for r in reps_detail):
        return round_to_increment(weight + increment, increment), [low] * len(reps_detail)
    return weight, [min(high, r + 1) for r in reps_detail]


def normalize_range_detail(weight: float, reps_detail: list, low: int, high: int, increment: float):
    """Проекция текущей точки в новый диапазон повторов при ручном редактировании параметров
    прогрессии — просто зажимаем каждый подход в новые границы, без пересчёта веса."""
    return weight, [min(high, max(low, int(r))) for r in reps_detail]


def generate_remaining_sessions(exercise_type, frequency, low, high, increment, sets_count,
                                 total_sessions, from_index, anchor_weight, anchor_reps_detail):
    """
    Строит план сессий от from_index (включительно) до total_sessions, начиная с состояния
    (anchor_weight, anchor_reps_detail) — цель ближайшей heavy/"стандартной" сессии, по подходам.
    Роль каждого индекса определяется его АБСОЛЮТНОЙ позицией в шаблоне (не сдвигается при
    перегенерации), поэтому пересчёт с середины недели не путает фазу heavy/light.
    Возвращает список кортежей (session_index, role, planned_weight, planned_reps_detail, planned_sets).
    """
    template = role_template(exercise_type, frequency)
    freq = len(template)
    w, reps_detail = anchor_weight, list(anchor_reps_detail)
    out = []
    for idx in range(from_index, total_sessions + 1):
        phase = (idx - 1) % freq
        if idx > from_index and phase == 0:
            w, reps_detail = climb_detail(w, reps_detail, low, high, increment)
        role = template[phase]
        if role in (None, "heavy"):
            out.append((idx, role, w, list(reps_detail), sets_count))
        else:
            aw, adetail = role_adjust_detail(role, w, reps_detail)
            out.append((idx, role, aw, adetail, sets_count))
    return out


def adapt_actual_detail(planned_weight, planned_reps_detail, actual_detail: list,
                         low, high, increment, prior_fail_streak: int):
    """
    По факту одной heavy/"стандартной" сессии (по каждому подходу отдельно) решает новую
    точку (anchor_weight, anchor_reps_detail), от которой будут перегенерированы все
    дальнейшие сессии, и новый fail_streak.

    Step UP:   все рабочие подходы достигли верха диапазона → вес +increment, повторы к низу.
    Hold:      хотя бы один подход не дотянул до верха, но 1-й подход ≥ низа диапазона →
               вес тот же, следующая цель по каждому подходу +1 повтор (успешные подходы,
               уже на верху диапазона, остаются на нём).
    Step DOWN: 1-й подход не набрал низ диапазона ДВАЖДЫ подряд на одном весе → -10%.
    """
    n = len(planned_reps_detail)
    if len(actual_detail) < n:
        # неполный объём — не оцениваем UP/DOWN, просто переносим факт по сделанным подходам,
        # план для недостающих подходов не трогаем; fail_streak сбрасываем (как и раньше)
        new_reps = list(planned_reps_detail)
        for i, s in enumerate(actual_detail):
            new_reps[i] = int(s["reps"])
        actual_weight = actual_detail[0]["weight"] if actual_detail else planned_weight
        return actual_weight, new_reps, 0

    actual_weight = actual_detail[0]["weight"]
    actual_reps = [int(s["reps"]) for s in actual_detail[:n]]

    if actual_reps[0] < low and actual_weight == planned_weight:
        if prior_fail_streak >= 1:
            return round_to_increment(actual_weight * 0.9, increment), [low] * n, 0
        return planned_weight, planned_reps_detail, prior_fail_streak + 1

    if all(r >= high for r in actual_reps):
        return round_to_increment(actual_weight + increment, increment), [low] * n, 0

    new_reps = [high if r >= high else max(1, r + 1) for r in actual_reps]
    return actual_weight, new_reps, 0


# ── Унилатеральный режим: тонкий адаптер поверх того же движка ──────────────
# Идея: раздельный подход по сторонам — это ровно тот же adapt_actual_detail/
# climb_detail/generate_remaining_sessions, только на "плоском" списке повторов
# вдвое длиннее (sets_count*2), где в каждой паре слабая сторона идёт первой.
# Ядро выше не меняется и не знает про стороны вообще — риск сломать уже
# протестированный билатеральный путь нулевой. "L"/"R" на экране — это "более
# слабая/сильная сторона В ЭТОМ подходе на момент генерации плана", а не
# анатомически закреплённая рука/нога: если стороны на одной сессии поменялись
# местами по факту, на следующей сессии они просто пересортируются заново.

def _pack_unilateral_detail(weight: float, reps_flat: list) -> list:
    """Плоский список длиной 2*n -> список подходов {weightL,repsL,weightR,repsR} для
    planned_detail/отображения (repsL — слабая сторона пары, repsR — сильная)."""
    return [
        {"weightL": weight, "repsL": reps_flat[2 * i], "weightR": weight, "repsR": reps_flat[2 * i + 1], "bilateral": True}
        for i in range(len(reps_flat) // 2)
    ]


def _unpack_unilateral_actual(actual_detail: list):
    """Список фактических подходов -> (вес, плоский список {"weight","reps"} длиной 2*к-во
    подходов, слабая сторона пары первой). Понимает и {weightL,repsL,weightR,repsR}, и обычный
    {weight,reps} — второй вариант (сет по факту залогирован как билатеральный) трактуется как
    "обе стороны сделали одинаково" — это и есть fallback смешанного режима."""
    flat, weight = [], None
    for s in actual_detail:
        if s.get("repsL") is not None:
            wl = s.get("weightL")
            rl, rr = int(s["repsL"]), int(s.get("repsR") if s.get("repsR") is not None else s["repsL"])
        else:
            wl = s.get("weight")
            rl = rr = int(s.get("reps") or 0)
        if weight is None:
            weight = wl
        lo, hi = (rl, rr) if rl <= rr else (rr, rl)
        flat.append({"weight": wl, "reps": lo})
        flat.append({"weight": wl, "reps": hi})
    return weight, flat


# ── Модели ────────────────────────────────────────────────────────────────────

class WorkoutIn(BaseModel):
    id:        int
    name:      str
    date:      str
    exercises: list

class MeasurementIn(BaseModel):
    id:   int
    name: str
    date: str
    data: dict = {}

class TemplateIn(BaseModel):
    id:        int
    name:      str
    exercises: list   # [{name: str, sets_count: int, bilateral: bool}, ...]

class ProfileCreateIn(BaseModel):
    name: str

class ProfileUpdateIn(BaseModel):
    name:              Optional[str] = None
    is_main:           Optional[bool] = None
    show_workouts:     Optional[bool] = None
    show_exercises:    Optional[bool] = None
    show_comments:     Optional[bool] = None
    show_measurements: Optional[bool] = None

class FriendUsernameIn(BaseModel):
    username: str

class FriendCodeIn(BaseModel):
    code: str

class ExerciseNoteIn(BaseModel):
    name: str
    note: str = ""

class ExerciseNoteRenameIn(BaseModel):
    old_name: str
    new_name: str

class ManualSetIn(BaseModel):
    weight:  Optional[float] = None
    reps:    Optional[int] = None
    weightL: Optional[float] = None
    repsL:   Optional[int] = None
    weightR: Optional[float] = None
    repsR:   Optional[int] = None

class ManualSessionIn(BaseModel):
    sets: list[ManualSetIn]   # каждый подход — свой вес/повторы, без общего "на всю тренировку"

class ProgressionCreateIn(BaseModel):
    exercise_name:  str
    mode:           str                       # 'manual' | 'calculated'
    # calculated:
    exercise_type:  Optional[str] = None       # main_compound|accessory_compound|isolation|custom
    goal:           Optional[str] = None       # strength|hypertrophy|strength_hypertrophy
    rep_unit:       str = "reps"               # оставлено для обратной совместимости, всегда "reps"
    rep_range_low:  Optional[int] = None
    rep_range_high: Optional[int] = None
    frequency:      Optional[int] = None
    sets_count:     Optional[int] = None
    increment:      Optional[float] = None
    start_weight:   Optional[float] = None
    start_reps:     Optional[int] = None
    start_rir:      Optional[float] = None
    weeks:          Optional[int] = None       # длительность цикла в неделях (total_sessions = weeks*frequency)
    deload_enabled: bool = False
    unilateral:     bool = False               # раздельный учёт по сторонам (см. _pack_unilateral_detail)
    # manual:
    manual_sessions: list[ManualSessionIn] = []

class ProgressionEditIn(BaseModel):
    goal:           Optional[str] = None
    rep_range_low:  Optional[int] = None
    rep_range_high: Optional[int] = None
    frequency:      Optional[int] = None
    sets_count:     Optional[int] = None
    increment:      Optional[float] = None
    deload_enabled: Optional[bool] = None

class SessionLogIn(BaseModel):
    actual_weight: float
    actual_reps:   int
    actual_sets:   int
    actual_rir:    Optional[float] = None
    workout_id:    Optional[int] = None
    actual_detail: Optional[list[ManualSetIn]] = None   # подробный факт по каждому подходу (произвольный режим)

class NewCycleIn(BaseModel):
    weeks:          Optional[int] = None
    goal:           Optional[str] = None
    rep_range_low:  Optional[int] = None
    rep_range_high: Optional[int] = None
    frequency:      Optional[int] = None
    sets_count:     Optional[int] = None
    increment:      Optional[float] = None
    deload_enabled: Optional[bool] = None


# ── Роуты: тренировки ─────────────────────────────────────────────────────────

@app.get("/workouts")
def list_workouts(x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        rows = conn.execute(
            "SELECT * FROM workouts WHERE profile_id=? ORDER BY id DESC", (pid,)
        ).fetchall()
    return [{"id":r["id"],"name":r["name"],"date":r["date"],"exercises":json.loads(r["exercises"])} for r in rows]


@app.post("/workouts")
def save_workout(w: WorkoutIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        if w.id != -1:
            existing = conn.execute(
                "SELECT id FROM workouts WHERE profile_id=? AND id=?", (pid, w.id)
            ).fetchone()
        else:
            existing = None

        if existing:
            conn.execute(
                "UPDATE workouts SET name=?, date=?, exercises=? WHERE profile_id=? AND id=?",
                (w.name, w.date, json.dumps(w.exercises, ensure_ascii=False), pid, w.id)
            )
            return {"ok": True, "id": w.id}
        else:
            cur = conn.execute(
                "INSERT INTO workouts (user_id, profile_id, name, date, exercises) VALUES (?,?,?,?,?)",
                (uid, pid, w.name, w.date, json.dumps(w.exercises, ensure_ascii=False))
            )
            return {"ok": True, "id": cur.lastrowid}


@app.delete("/workouts/{workout_id}")
def delete_workout(workout_id: int, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        conn.execute("DELETE FROM workouts WHERE profile_id=? AND id=?", (pid, workout_id))
    return {"ok": True}


# ── Роуты: шаблоны тренировок ─────────────────────────────────────────────────

@app.get("/templates")
def list_templates(x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        rows = conn.execute(
            "SELECT * FROM templates WHERE profile_id=? ORDER BY id DESC", (pid,)
        ).fetchall()
    return [{"id":r["id"],"name":r["name"],"exercises":json.loads(r["exercises"])} for r in rows]


@app.post("/templates")
def save_template(t: TemplateIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        if t.id != -1:
            existing = conn.execute(
                "SELECT id FROM templates WHERE profile_id=? AND id=?", (pid, t.id)
            ).fetchone()
        else:
            existing = None

        if existing:
            conn.execute(
                "UPDATE templates SET name=?, exercises=? WHERE profile_id=? AND id=?",
                (t.name, json.dumps(t.exercises, ensure_ascii=False), pid, t.id)
            )
            return {"ok": True, "id": t.id}
        else:
            cur = conn.execute(
                "INSERT INTO templates (user_id, profile_id, name, exercises) VALUES (?,?,?,?)",
                (uid, pid, t.name, json.dumps(t.exercises, ensure_ascii=False))
            )
            return {"ok": True, "id": cur.lastrowid}


@app.delete("/templates/{template_id}")
def delete_template(template_id: int, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        conn.execute("DELETE FROM templates WHERE profile_id=? AND id=?", (pid, template_id))
    return {"ok": True}


# ── Роуты: замеры ─────────────────────────────────────────────────────────────

@app.get("/measurements")
def list_measurements(x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        rows = conn.execute(
            "SELECT * FROM measurements WHERE profile_id=? ORDER BY id DESC", (pid,)
        ).fetchall()
    result = []
    for r in rows:
        item = {"id":r["id"],"name":r["name"],"date":r["date"]}
        item.update(json.loads(r["data"]))
        result.append(item)
    return result


@app.post("/measurements")
def save_measurement(m: MeasurementIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        if m.id != -1:
            existing = conn.execute(
                "SELECT id FROM measurements WHERE profile_id=? AND id=?", (pid, m.id)
            ).fetchone()
        else:
            existing = None

        data_json = json.dumps(m.data, ensure_ascii=False)
        if existing:
            conn.execute(
                "UPDATE measurements SET name=?, date=?, data=? WHERE profile_id=? AND id=?",
                (m.name, m.date, data_json, pid, m.id)
            )
            return {"ok": True, "id": m.id}
        else:
            cur = conn.execute(
                "INSERT INTO measurements (user_id, profile_id, name, date, data) VALUES (?,?,?,?,?)",
                (uid, pid, m.name, m.date, data_json)
            )
            return {"ok": True, "id": cur.lastrowid}


@app.delete("/measurements/{measurement_id}")
def delete_measurement(measurement_id: int, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        conn.execute("DELETE FROM measurements WHERE profile_id=? AND id=?", (pid, measurement_id))
    return {"ok": True}


# ── Роуты: заметки к упражнениям ────────────────────────────────────────────
# Общее описание упражнения (техника, сетап и т.д.) — привязано к профилю и
# названию упражнения (без учёта регистра), не к конкретной тренировке.

@app.get("/exercise-notes")
def list_exercise_notes(x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        rows = conn.execute(
            "SELECT name_lc, note FROM exercise_notes WHERE profile_id=?", (pid,)
        ).fetchall()
    return {r["name_lc"]: r["note"] for r in rows}


@app.put("/exercise-notes")
def save_exercise_note(body: ExerciseNoteIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    name_lc = body.name.strip().lower()
    if not name_lc:
        raise HTTPException(400, "Пустое название упражнения")
    note = body.note.strip()
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        if note:
            conn.execute(
                "INSERT OR REPLACE INTO exercise_notes (profile_id, name_lc, note) VALUES (?,?,?)",
                (pid, name_lc, note)
            )
        else:
            # Пустое описание — просто убираем запись, нет смысла её хранить
            conn.execute("DELETE FROM exercise_notes WHERE profile_id=? AND name_lc=?", (pid, name_lc))
    return {"ok": True}


@app.put("/exercise-notes/rename")
def rename_exercise_note(body: ExerciseNoteRenameIn, x_init_data: str = Header(...)):
    """Переносит заметку на новое имя при переименовании упражнения (см. commitRenameEx на фронте)."""
    uid = get_user_id(x_init_data)
    old_lc = body.old_name.strip().lower()
    new_lc = body.new_name.strip().lower()
    if not old_lc or not new_lc or old_lc == new_lc:
        return {"ok": True}
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        clash = conn.execute(
            "SELECT 1 FROM exercise_notes WHERE profile_id=? AND name_lc=?", (pid, new_lc)
        ).fetchone()
        if clash:
            # Под новым именем уже есть своя заметка — не перетираем её, старую просто убираем
            conn.execute("DELETE FROM exercise_notes WHERE profile_id=? AND name_lc=?", (pid, old_lc))
        else:
            conn.execute(
                "UPDATE exercise_notes SET name_lc=? WHERE profile_id=? AND name_lc=?",
                (new_lc, pid, old_lc)
            )
        # Прогрессии ищут совпадение тренировок по exercise_name_lc — без этого
        # переименование "отвязывает" прогрессию от будущих тренировок. Если под
        # новым именем уже есть своя активная прогрессия — конфликт частичного
        # уникального индекса; переименование заметки при этом всё равно должно
        # пройти, поэтому глушим только эту часть.
        try:
            conn.execute(
                "UPDATE progressions SET exercise_name=?, exercise_name_lc=? WHERE profile_id=? AND exercise_name_lc=?",
                (body.new_name.strip(), new_lc, pid, old_lc)
            )
        except sqlite3.IntegrityError:
            pass
    return {"ok": True}


# ── Роуты: профили ─────────────────────────────────────────────────────────────

@app.get("/profiles")
def list_profiles(x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        active_id = get_active_profile_id(conn, uid)
        rows = conn.execute("SELECT * FROM profiles WHERE owner_id=? ORDER BY id", (uid,)).fetchall()
    return [{
        "id": r["id"], "name": r["name"], "is_main": bool(r["is_main"]),
        "show_workouts": bool(r["show_workouts"]), "show_exercises": bool(r["show_exercises"]),
        "show_comments": bool(r["show_comments"]), "show_measurements": bool(r["show_measurements"]),
        "is_active": r["id"] == active_id,
    } for r in rows]


@app.post("/profiles")
def create_profile(p: ProfileCreateIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    name = p.name.strip() or "Новый профиль"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO profiles (owner_id, name, is_main, show_workouts, show_exercises, show_comments, show_measurements) "
            "VALUES (?,?,0,1,1,1,1)",
            (uid, name)
        )
        return {"ok": True, "id": cur.lastrowid}


@app.put("/profiles/{profile_id}")
def update_profile(profile_id: int, p: ProfileUpdateIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id=? AND owner_id=?", (profile_id, uid)).fetchone()
        if not row:
            raise HTTPException(404, "Профиль не найден")

        fields, values = [], []
        if p.name is not None:
            fields.append("name=?"); values.append(p.name.strip() or row["name"])
        if p.show_workouts is not None:
            fields.append("show_workouts=?"); values.append(int(p.show_workouts))
        if p.show_exercises is not None:
            fields.append("show_exercises=?"); values.append(int(p.show_exercises))
        if p.show_comments is not None:
            fields.append("show_comments=?"); values.append(int(p.show_comments))
        if p.show_measurements is not None:
            fields.append("show_measurements=?"); values.append(int(p.show_measurements))
        if p.is_main is not None:
            if p.is_main:
                conn.execute("UPDATE profiles SET is_main=0 WHERE owner_id=?", (uid,))
            fields.append("is_main=?"); values.append(int(p.is_main))

        if fields:
            values.extend([profile_id, uid])
            conn.execute(f"UPDATE profiles SET {', '.join(fields)} WHERE id=? AND owner_id=?", values)
    return {"ok": True}


@app.delete("/profiles/{profile_id}")
def delete_profile(profile_id: int, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM profiles WHERE owner_id=?", (uid,)).fetchone()["c"]
        if count <= 1:
            raise HTTPException(400, "Нельзя удалить последний профиль")

        conn.execute("DELETE FROM workouts WHERE profile_id=?", (profile_id,))
        conn.execute("DELETE FROM measurements WHERE profile_id=?", (profile_id,))
        conn.execute("DELETE FROM templates WHERE profile_id=?", (profile_id,))
        conn.execute("DELETE FROM profiles WHERE id=? AND owner_id=?", (profile_id, uid))

        active = conn.execute("SELECT active_profile_id FROM users WHERE user_id=?", (uid,)).fetchone()
        if active and active["active_profile_id"] == profile_id:
            fallback = conn.execute("SELECT id FROM profiles WHERE owner_id=? ORDER BY id LIMIT 1", (uid,)).fetchone()
            if fallback:
                conn.execute("UPDATE users SET active_profile_id=? WHERE user_id=?", (fallback["id"], uid))
    return {"ok": True}


@app.post("/profiles/{profile_id}/activate")
def activate_profile(profile_id: int, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        row = conn.execute("SELECT id FROM profiles WHERE id=? AND owner_id=?", (profile_id, uid)).fetchone()
        if not row:
            raise HTTPException(404, "Профиль не найден")
        conn.execute("UPDATE users SET active_profile_id=? WHERE user_id=?", (profile_id, uid))
    return {"ok": True}


# Те же подписи полей замеров, что и во фронтенде (App.jsx → MEASUREMENT_FIELDS),
# используются только для читаемого текстового экспорта.
_MEASUREMENT_FIELD_LABELS = {
    "weight": "Вес тела", "waist": "Талия", "chest": "Грудь", "shoulders": "Плечи",
    "armRight": "Правая рука", "armLeft": "Левая рука",
    "forearmRight": "Правое предплечье", "forearmLeft": "Левое предплечье",
    "glutes": "Ягодицы", "quadRight": "Правый квадрицепс", "quadLeft": "Левый квадрицепс",
    "calfRight": "Правая икра", "calfLeft": "Левая икра",
}


def _fmt_date_ru(iso: str) -> str:
    try:
        y, m, d = iso.split("-")
        return f"{d}.{m}.{y}"
    except Exception:
        return iso


def _fmt_set_line(s: dict):
    if s.get("bilateral"):
        wl, rl = s.get("weightL") or "—", s.get("repsL") or "—"
        wr, rr = s.get("weightR") or "—", s.get("repsR") or "—"
        if not any([s.get("weightL"), s.get("repsL"), s.get("weightR"), s.get("repsR")]):
            return None
        return f"Л {wl} кг × {rl}  ·  П {wr} кг × {rr}"
    w, r = s.get("weight"), s.get("reps")
    if not (w or r):
        return None
    return f"{w or '—'} кг × {r or '—'} повт"


def _fmt_progression_role(role):
    return {"heavy": "Тяжёлая", "light": "Лёгкая"}.get(role, "")


def _fmt_progression_session_line(s, rep_unit: str) -> str:
    s = dict(s)  # s может быть как sqlite3.Row (экспорт), так и обычным dict — Row не умеет .get()
    unit = "повт"
    role = f" ({_fmt_progression_role(s['role'])})" if s.get("role") else ""

    planned_detail = s.get("planned_detail")
    if isinstance(planned_detail, str):
        planned_detail = json.loads(planned_detail)
    if planned_detail:
        plan = "План: " + "; ".join(_fmt_set_line(d) or "—" for d in planned_detail) + role
    else:
        plan = f"План: {s['planned_weight']} кг × {s['planned_reps']} {unit} × {s['planned_sets']} подх{role}"

    if s["status"] == "done":
        actual_detail = s.get("actual_detail")
        if isinstance(actual_detail, str):
            actual_detail = json.loads(actual_detail)
        if actual_detail:
            fact = "Факт: " + "; ".join(_fmt_set_line(d) or "—" for d in actual_detail)
        else:
            fact = f"Факт: {s['actual_weight']} кг × {s['actual_reps']} {unit} × {s['actual_sets']} подх"
        return f"{plan}  →  {fact}"
    if s["status"] == "skipped":
        return f"{plan}  →  пропущена"
    return f"{plan}  →  ещё не выполнена"


def _build_progression_export(progression_rows_with_sessions) -> list:
    lines = []
    for prog, sessions in progression_rows_with_sessions:
        status = {"active": "активна", "completed": "завершена"}.get(prog["status"], prog["status"])
        mode = "произвольная" if prog["mode"] == "manual" else "расчётная"
        lines.append(f"• {prog['exercise_name']} — {mode}, {status}")
        if prog["mode"] == "calculated":
            goal_label = {"strength": "сила", "hypertrophy": "гипертрофия",
                          "strength_hypertrophy": "сила+гипертрофия"}.get(prog["goal"], prog["goal"] or "—")
            lines.append(f"  Цель: {goal_label}  ·  диапазон {prog['rep_range_low']}–{prog['rep_range_high']}  ·  шаг {prog['increment']} кг")
        for s in sessions:
            lines.append(f"  {s['session_index']}. {_fmt_progression_session_line(s, prog['rep_unit'] or 'reps')}")
        lines.append("")
    return lines



def _build_profile_export(profile_name: str, workout_rows, measurement_rows, exercise_notes: dict,
                           progression_rows_with_sessions=None) -> str:
    lines = [
        f"ДНЕВНИК ТРЕНИРОВОК — {profile_name}",
        f"Экспорт от {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}",
        "",
        "=" * 40,
        f"ТРЕНИРОВКИ ({len(workout_rows)})",
        "=" * 40,
        "",
    ]

    # history_by_name: имя упражнения -> список (дата, [строки подходов], комментарий),
    # собирается за один проход по тренировкам — используется дальше для раздела УПРАЖНЕНИЯ
    history_by_name = {}
    for w in sorted(workout_rows, key=lambda r: (r["date"], r["id"])):
        exercises = json.loads(w["exercises"])
        lines.append(f"{_fmt_date_ru(w['date'])} · {w['name']}")
        for i, ex in enumerate(exercises, 1):
            ex_name = (ex.get("name") or f"Упражнение {i}").strip()
            set_lines = [fs for fs in (_fmt_set_line(s) for s in ex.get("sets", [])) if fs]
            comment = (ex.get("comment") or "").strip()
            lines.append(f"  {i}. {ex_name}")
            for si, fs in enumerate(set_lines, 1):
                lines.append(f"     {si}) {fs}")
            if comment:
                lines.append(f"     Комментарий: {comment}")
            if ex_name:
                history_by_name.setdefault(ex_name, []).append((w["date"], set_lines, comment))
        lines += ["", "-" * 40, ""]

    lines += ["=" * 40, f"ЗАМЕРЫ ({len(measurement_rows)})", "=" * 40, ""]

    for m in sorted(measurement_rows, key=lambda r: (r["date"], r["id"])):
        data = json.loads(m["data"])
        lines.append(f"{_fmt_date_ru(m['date'])} · {m['name']}")
        filled = False
        for key, label in _MEASUREMENT_FIELD_LABELS.items():
            val = data.get(key)
            if val not in (None, ""):
                unit = "кг" if key == "weight" else "см"
                lines.append(f"  {label}: {val} {unit}")
                filled = True
        comment = (data.get("comment") or "").strip()
        if not filled and not comment:
            lines.append("  (ничего не заполнено)")
        if comment:
            lines.append(f"  Комментарий: {comment}")
        lines.append("")

    # Упражнения — список + описание техники + ПОЛНАЯ история по каждому (как в
    # приложении на вкладке «Упражнения»), а не просто перечисление названий.
    sorted_names = sorted(history_by_name.keys(), key=lambda s: s.lower())
    lines += ["=" * 40, f"УПРАЖНЕНИЯ ({len(sorted_names)})", "=" * 40, ""]
    for name in sorted_names:
        lines.append(f"• {name}")
        note = (exercise_notes.get(name.strip().lower()) or "").strip()
        if note:
            lines.append(f"  Описание: {note}")
        entries = sorted(history_by_name[name], key=lambda e: e[0])
        lines.append(f"  История ({len(entries)}):")
        for date, set_lines, comment in entries:
            body = "; ".join(set_lines) if set_lines else "(без данных)"
            lines.append(f"    {_fmt_date_ru(date)} — {body}")
            if comment:
                lines.append(f"      Комментарий: {comment}")
        lines.append("")

    if progression_rows_with_sessions:
        lines += ["=" * 40, f"ПРОГРЕССИИ ({len(progression_rows_with_sessions)})", "=" * 40, ""]
        lines += _build_progression_export(progression_rows_with_sessions)

    return "\n".join(lines)




# Отправка файла ботом прямо в чат — самый надёжный способ доставить файл
# пользователю внутри Telegram: сохранить/переслать документ из чата
# поддерживается на 100% всегда и везде (в отличие от WebApp.downloadFile,
# который на части версий iOS пока просто ничего не делает). BOT_TOKEN уже
# задан выше (используется и для проверки initData).


def _send_telegram_document(chat_id: str, filename: str, content: bytes, caption: str = "") -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан на бэкенде (Railway → Variables)")
    boundary = secrets.token_hex(16)
    body = io.BytesIO()

    def w(part):
        body.write(part.encode("utf-8") if isinstance(part, str) else part)

    w(f"--{boundary}\r\n")
    w(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n')
    if caption:
        w(f"--{boundary}\r\n")
        w(f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n')
    w(f"--{boundary}\r\n")
    w(f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n')
    w("Content-Type: text/plain; charset=utf-8\r\n\r\n")
    w(content)
    w("\r\n")
    w(f"--{boundary}--\r\n")

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Неизвестная ошибка Telegram API"))


# ── Telegram-бот (вебхук) ────────────────────────────────────────────────────
# Раньше отдельный сервис bot.py на long polling: он сам, постоянно, каждые
# несколько секунд стучался к серверам Telegram ("есть что-то новое?"). Для
# Railway это непрерывный ИСХОДЯЩИЙ трафик, а "уход в сон" у Railway считается
# именно по отсутствию исходящего трафика — поэтому такой сервис никогда не
# засыпал, даже когда ботом реально никто не пользовался, и жёг деньги просто
# так. Вебхук устроен наоборот: Telegram сам присылает HTTP-запрос СЮДА, когда
# есть новое сообщение — между сообщениями никакого трафика нет вообще, и этот
# бэкенд спит между запросами так же, как и всегда (весь остальной код тут
# уже был к этому готов — своих исходящих фоновых соединений у него и не было).
# Вся логика — ровно то, что раньше делал bot.py: ответ на /start (и на
# /start с payload приглашения) с кнопкой открытия Mini App.

def _send_telegram_message(chat_id, text: str, reply_markup: dict = None) -> None:
    if not BOT_TOKEN:
        return
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception:
        pass  # ответ боту — не критичная операция, не должна ронять обработку вебхука


@app.post("/telegram/webhook/{secret}", include_in_schema=False)
async def telegram_webhook(secret: str, request: Request):
    # Секрет прямо в пути — простая защита от посторонних POST-запросов на
    # этот адрес (Telegram всегда шлёт его же, раз он задан при setWebhook).
    if not TELEGRAM_WEBHOOK_SECRET or secret != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(404, "Not found")
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id and text.startswith("/start"):
        parts = text.split(maxsplit=1)
        webapp_url = WEBAPP_URL
        reply_text = "Привет! Нажми кнопку чтобы открыть дневник тренировок 👇"
        if len(parts) > 1:
            invite_payload = parts[1].strip()
            sep = "&" if "?" in webapp_url else "?"
            webapp_url = f"{webapp_url}{sep}invite={invite_payload}"
            reply_text = "Тебя пригласили в дневник тренировок! Нажми кнопку — и вы автоматически станете друзьями 👇"
        reply_markup = {"inline_keyboard": [[{"text": "💪 Открыть дневник", "web_app": {"url": webapp_url}}]]}
        _send_telegram_message(chat_id, reply_text, reply_markup)

    # Telegram ждёт быстрый 200 OK на любое обновление, иначе будет повторять
    # доставку — отвечаем ok даже на сообщения, которые не /start.
    return {"ok": True}


@app.on_event("startup")
def _register_telegram_webhook():
    if not (BOT_TOKEN and PUBLIC_URL and TELEGRAM_WEBHOOK_SECRET):
        return  # не всё настроено — тихо пропускаем, вебхук можно включить и вручную (см. деплой)
    url = f"{PUBLIC_URL}/telegram/webhook/{TELEGRAM_WEBHOOK_SECRET}"
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={urllib.parse.quote(url, safe='')}",
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception:
        pass  # не мешаем запуску бэкенда, если Telegram недоступен в момент старта


def _profile_export_data(profile_id: int, uid: str):
    with get_db() as conn:
        profile = conn.execute(
            "SELECT * FROM profiles WHERE id=? AND owner_id=?", (profile_id, uid)
        ).fetchone()
        if not profile:
            raise HTTPException(404, "Профиль не найден")
        workout_rows = conn.execute("SELECT * FROM workouts WHERE profile_id=?", (profile_id,)).fetchall()
        measurement_rows = conn.execute("SELECT * FROM measurements WHERE profile_id=?", (profile_id,)).fetchall()
        note_rows = conn.execute(
            "SELECT name_lc, note FROM exercise_notes WHERE profile_id=?", (profile_id,)
        ).fetchall()
        exercise_notes = {r["name_lc"]: r["note"] for r in note_rows}
        progression_rows = conn.execute(
            "SELECT * FROM progressions WHERE profile_id=? ORDER BY id", (profile_id,)
        ).fetchall()
        progressions_with_sessions = []
        for prog in progression_rows:
            sessions = conn.execute(
                "SELECT * FROM progression_sessions WHERE progression_id=? ORDER BY session_index", (prog["id"],)
            ).fetchall()
            progressions_with_sessions.append((prog, sessions))
    text = _build_profile_export(profile["name"], workout_rows, measurement_rows, exercise_notes,
                                  progressions_with_sessions)
    return profile["name"], text


@app.post("/profiles/{profile_id}/export-to-chat")
def export_to_chat(profile_id: int, x_init_data: str = Header(...)):
    """Отправляет текстовый экспорт профиля документом в чат с ботом — надёжно
    работает на любой платформе, в отличие от скачивания файла внутри веб-вью."""
    uid = get_user_id(x_init_data)
    profile_name, text = _profile_export_data(profile_id, uid)
    filename = f"{profile_name}.txt"
    try:
        _send_telegram_document(uid, filename, text.encode("utf-8"), caption=f"Экспорт профиля «{profile_name}»")
    except Exception as e:
        raise HTTPException(502, f"Не удалось отправить файл в Telegram: {e}")
    return {"ok": True}


@app.get("/profiles/{profile_id}/export")
def export_profile(profile_id: int, x_init_data: str = Header(...)):
    """Текстовый экспорт профиля для скачивания напрямую из браузера (вне Telegram) —
    внутри Telegram на телефонах используется export-to-chat, см. выше."""
    uid = get_user_id(x_init_data)
    profile_name, text = _profile_export_data(profile_id, uid)
    ascii_name = "workout-export.txt"
    utf8_name = urllib.parse.quote(f"{profile_name}.txt")
    headers = {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"}
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8", headers=headers)


# ── Роуты: друзья ──────────────────────────────────────────────────────────────

@app.get("/me/invite-link")
def get_invite_link(x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        row = conn.execute("SELECT invite_code FROM users WHERE user_id=?", (uid,)).fetchone()
        code = row["invite_code"] if row else None
        if not code:
            code = secrets.token_hex(4)
            conn.execute("UPDATE users SET invite_code=? WHERE user_id=?", (code, uid))
    return {"code": code}


@app.get("/friends")
def list_friends(x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_a, user_b FROM friends WHERE user_a=? OR user_b=?", (uid, uid)
        ).fetchall()
        friend_ids = [r["user_b"] if r["user_a"] == uid else r["user_a"] for r in rows]
        friends = []
        for fid in friend_ids:
            u = conn.execute(
                "SELECT user_id, username, first_name FROM users WHERE user_id=?", (fid,)
            ).fetchone()
            if u:
                friends.append({
                    "id": u["user_id"],
                    "username": u["username"],
                    "name": u["first_name"] or u["username"] or "Без имени",
                })
    return friends


@app.get("/friends/search")
def search_users(q: str = "", x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    query = q.strip().lstrip("@").lower()
    if not query:
        return []
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, username, first_name FROM users WHERE lower(username)=? AND user_id!=?",
            (query, uid)
        ).fetchall()
    return [{"id": r["user_id"], "username": r["username"], "name": r["first_name"] or r["username"]} for r in rows]


@app.post("/friends/add-by-username")
def add_friend_by_username(body: FriendUsernameIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    uname = body.username.strip().lstrip("@").lower()
    with get_db() as conn:
        target = conn.execute("SELECT user_id FROM users WHERE lower(username)=?", (uname,)).fetchone()
        if not target:
            raise HTTPException(404, "Пользователь не найден. Возможно он ещё не открывал приложение.")
        tid = target["user_id"]
        if tid == uid:
            raise HTTPException(400, "Нельзя добавить себя")
        a, b = normalize_pair(uid, tid)
        conn.execute("INSERT OR IGNORE INTO friends (user_a, user_b) VALUES (?,?)", (a, b))
    return {"ok": True}


@app.post("/friends/add-by-code")
def add_friend_by_code(body: FriendCodeIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        target = conn.execute("SELECT user_id FROM users WHERE invite_code=?", (body.code.strip(),)).fetchone()
        if not target:
            raise HTTPException(404, "Ссылка недействительна")
        tid = target["user_id"]
        if tid == uid:
            return {"ok": True}
        a, b = normalize_pair(uid, tid)
        conn.execute("INSERT OR IGNORE INTO friends (user_a, user_b) VALUES (?,?)", (a, b))
    return {"ok": True}


@app.delete("/friends/{friend_id}")
def remove_friend(friend_id: str, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    a, b = normalize_pair(uid, friend_id)
    with get_db() as conn:
        conn.execute("DELETE FROM friends WHERE user_a=? AND user_b=?", (a, b))
    return {"ok": True}


@app.get("/friends/{friend_id}/profile")
def get_friend_profile(friend_id: str, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    a, b = normalize_pair(uid, friend_id)
    with get_db() as conn:
        is_friend = conn.execute(
            "SELECT 1 FROM friends WHERE user_a=? AND user_b=?", (a, b)
        ).fetchone()
        if not is_friend:
            raise HTTPException(403, "Это не ваш друг")

        user = conn.execute(
            "SELECT first_name, username FROM users WHERE user_id=?", (friend_id,)
        ).fetchone()
        name = (user["first_name"] if user else None) or (user["username"] if user else None) or "Пользователь"

        profile = conn.execute(
            "SELECT * FROM profiles WHERE owner_id=? AND is_main=1", (friend_id,)
        ).fetchone()

        if not profile:
            return {
                "name": name, "profile_name": None,
                "show_workouts": False, "show_exercises": False, "show_measurements": False,
                "workouts": [], "measurements": [],
            }

        show_w = bool(profile["show_workouts"])
        show_e = bool(profile["show_exercises"])
        show_c = bool(profile["show_comments"])
        show_m = bool(profile["show_measurements"])

        workouts = []
        if show_w or show_e:
            rows = conn.execute(
                "SELECT * FROM workouts WHERE profile_id=? ORDER BY id DESC", (profile["id"],)
            ).fetchall()
            for r in rows:
                exs = json.loads(r["exercises"])
                if not show_c:
                    for ex in exs:
                        ex["comment"] = ""
                workouts.append({"id": r["id"], "name": r["name"], "date": r["date"], "exercises": exs})

        measurements = []
        if show_m:
            rows = conn.execute(
                "SELECT * FROM measurements WHERE profile_id=? ORDER BY id DESC", (profile["id"],)
            ).fetchall()
            for r in rows:
                item = {"id": r["id"], "name": r["name"], "date": r["date"]}
                item.update(json.loads(r["data"]))
                measurements.append(item)

    return {
        "name": name,
        "profile_name": profile["name"],
        "show_workouts": show_w,
        "show_exercises": show_e,
        "show_measurements": show_m,
        "workouts": workouts,
        "measurements": measurements,
    }


# ── Роуты: прогрессия ────────────────────────────────────────────────────────
# Премиум-фича: доступ только тем, у кого users.is_premium=1 (выдаётся через
# /admin/premium). Расчётная логика — чистые функции выше (round_to_increment,
# climb, normalize_overflow, generate_remaining_sessions, adapt_actual),
# роуты только читают/пишут БД и вызывают их.

def _require_premium(conn, uid: str):
    row = conn.execute("SELECT is_premium FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row or not row["is_premium"]:
        raise HTTPException(403, "Раздел «Прогрессия» доступен премиум-пользователям")


def _get_owned_progression(conn, pid: int, progression_id: int):
    row = conn.execute(
        "SELECT * FROM progressions WHERE id=? AND profile_id=?", (progression_id, pid)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Прогрессия не найдена")
    return row


def _serialize_progression(row) -> dict:
    d = dict(row)
    d["deload_enabled"] = bool(d.get("deload_enabled"))
    d["unilateral"] = bool(d.get("unilateral"))
    if d.get("current_reps_detail"):
        d["current_reps_detail"] = json.loads(d["current_reps_detail"])
    return d


def _serialize_session(row) -> dict:
    d = dict(row)
    if d.get("planned_detail"):
        d["planned_detail"] = json.loads(d["planned_detail"])
    if d.get("actual_detail"):
        d["actual_detail"] = json.loads(d["actual_detail"])
    return d


def _maybe_complete(conn, progression_id: int):
    """Если все сессии done/skipped — цикл завершён."""
    left = conn.execute(
        "SELECT COUNT(*) c FROM progression_sessions WHERE progression_id=? AND status='pending'",
        (progression_id,)
    ).fetchone()["c"]
    if left == 0:
        conn.execute("UPDATE progressions SET status='completed', updated_at=datetime('now') WHERE id=? AND status='active'",
                     (progression_id,))


def _insert_sessions(conn, progression_id: int, sessions, unilateral: bool = False, display_sets_count=None):
    """sessions: список (session_index, role, planned_weight, planned_reps_detail, planned_sets).
    Для unilateral=True planned_reps_detail — уже "плоский" список длиной 2*display_sets_count
    (см. модуль про унилатеральный режим выше), display_sets_count — истинное число физических
    подходов (для колонки planned_sets, которая не должна задваиваться)."""
    rows = []
    for idx, role, w, reps_detail, sets_count in sessions:
        if unilateral:
            detail = _pack_unilateral_detail(w, reps_detail)
            planned_sets = display_sets_count if display_sets_count is not None else sets_count
        else:
            detail = [{"weight": w, "reps": r} for r in reps_detail]
            planned_sets = sets_count
        rows.append((progression_id, idx, role, w, reps_detail[0], planned_sets,
                     json.dumps(detail, ensure_ascii=False)))
    conn.executemany(
        "INSERT INTO progression_sessions (progression_id, session_index, role, planned_weight, planned_reps, planned_sets, planned_detail) "
        "VALUES (?,?,?,?,?,?,?)", rows
    )


def _insert_manual_sessions(conn, progression_id: int, manual_sessions: list):
    """Каждая сессия — список подходов {weight,reps}, свой на каждый подход (не общий на тренировку)."""
    rows = []
    for idx, sess in enumerate(manual_sessions, 1):
        detail = [{"weight": s.weight, "reps": s.reps} for s in sess.sets]
        rows.append((progression_id, idx, detail[0]["weight"], detail[0]["reps"], len(detail),
                     json.dumps(detail, ensure_ascii=False)))
    conn.executemany(
        "INSERT INTO progression_sessions (progression_id, session_index, planned_weight, planned_reps, planned_sets, planned_detail) "
        "VALUES (?,?,?,?,?,?)", rows
    )


@app.get("/me/premium")
def get_my_premium(x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        row = conn.execute("SELECT is_premium FROM users WHERE user_id=?", (uid,)).fetchone()
    return {"is_premium": bool(row and row["is_premium"])}


@app.get("/progressions")
def list_progressions(x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        _require_premium(conn, uid)
        rows = conn.execute(
            "SELECT * FROM progressions WHERE profile_id=? ORDER BY id DESC", (pid,)
        ).fetchall()
        result = []
        for r in rows:
            d = _serialize_progression(r)
            counts = conn.execute(
                "SELECT COUNT(*) total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done "
                "FROM progression_sessions WHERE progression_id=?", (r["id"],)
            ).fetchone()
            d["sessions_total"] = counts["total"]
            d["sessions_done"] = counts["done"] or 0
            nxt = conn.execute(
                "SELECT * FROM progression_sessions WHERE progression_id=? AND status='pending' "
                "ORDER BY session_index LIMIT 1", (r["id"],)
            ).fetchone()
            d["next_session"] = _serialize_session(nxt) if nxt else None
            result.append(d)
    return result


@app.post("/progressions")
def create_progression(body: ProgressionCreateIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    name = body.exercise_name.strip()
    name_lc = name.lower()
    if not name:
        raise HTTPException(400, "Пустое название упражнения")

    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        _require_premium(conn, uid)

        if body.mode == "manual":
            if not body.manual_sessions:
                raise HTTPException(400, "Нужна хотя бы одна сессия плана")
            for i, sess in enumerate(body.manual_sessions, 1):
                if not sess.sets:
                    raise HTTPException(400, f"Тренировка {i}: нужен хотя бы один подход")
            total_sessions = len(body.manual_sessions)
            try:
                cur = conn.execute(
                    "INSERT INTO progressions (profile_id, exercise_name, exercise_name_lc, mode, status, total_sessions) "
                    "VALUES (?,?,?,'manual','active',?)",
                    (pid, name, name_lc, total_sessions)
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, "У этого упражнения уже есть активная прогрессия")
            new_id = cur.lastrowid
            _insert_manual_sessions(conn, new_id, body.manual_sessions)
            return {"ok": True, "id": new_id}

        # calculated
        required = [body.exercise_type, body.goal, body.rep_range_low, body.rep_range_high,
                    body.frequency, body.sets_count, body.increment, body.start_weight,
                    body.start_reps, body.weeks]
        if any(v is None for v in required):
            raise HTTPException(400, "Не заполнены все обязательные поля расчётной прогрессии")
        if body.rep_range_high <= body.rep_range_low:
            raise HTTPException(400, "Верх диапазона повторов должен быть больше низа")
        if body.increment <= 0 or body.sets_count < 1 or body.weeks < 1 or body.frequency < 1:
            raise HTTPException(400, "Некорректные числовые параметры")
        if body.exercise_type in VARYING_TYPES and body.frequency not in (1, 2, 3, 4):
            raise HTTPException(400, "Для этого типа упражнения частота — от 1 до 4 раз в неделю")

        total_sessions = body.weeks * body.frequency
        eff_sets = body.sets_count * 2 if body.unilateral else body.sets_count
        start_reps_detail = [body.start_reps] * eff_sets
        try:
            cur = conn.execute(
                "INSERT INTO progressions (profile_id, exercise_name, exercise_name_lc, mode, status, "
                "exercise_type, goal, rep_unit, rep_range_low, rep_range_high, frequency, sets_count, "
                "increment, start_weight, start_reps, start_rir, total_sessions, deload_enabled, unilateral, "
                "current_weight, current_reps, current_reps_detail) "
                "VALUES (?,?,?,'calculated','active',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, name, name_lc, body.exercise_type, body.goal, body.rep_unit,
                 body.rep_range_low, body.rep_range_high, body.frequency, body.sets_count,
                 body.increment, body.start_weight, body.start_reps, body.start_rir,
                 total_sessions, int(body.deload_enabled), int(body.unilateral),
                 body.start_weight, body.start_reps, json.dumps(start_reps_detail))
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "У этого упражнения уже есть активная прогрессия")
        new_id = cur.lastrowid

        sessions = generate_remaining_sessions(
            body.exercise_type, body.frequency, body.rep_range_low, body.rep_range_high,
            body.increment, eff_sets, total_sessions, 1, body.start_weight, start_reps_detail
        )
        _insert_sessions(conn, new_id, sessions, unilateral=body.unilateral, display_sets_count=body.sets_count)
        return {"ok": True, "id": new_id}


@app.get("/progressions/{progression_id}")
def get_progression(progression_id: int, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        _require_premium(conn, uid)
        prog = _get_owned_progression(conn, pid, progression_id)
        sessions = conn.execute(
            "SELECT * FROM progression_sessions WHERE progression_id=? ORDER BY session_index",
            (progression_id,)
        ).fetchall()
    d = _serialize_progression(prog)
    d["sessions"] = [_serialize_session(s) for s in sessions]
    return d


@app.put("/progressions/{progression_id}")
def edit_progression(progression_id: int, body: ProgressionEditIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        _require_premium(conn, uid)
        prog = _get_owned_progression(conn, pid, progression_id)
        if prog["mode"] != "calculated" or prog["status"] != "active":
            raise HTTPException(400, "Редактировать можно только активную расчётную прогрессию")

        goal          = body.goal if body.goal is not None else prog["goal"]
        low           = body.rep_range_low if body.rep_range_low is not None else prog["rep_range_low"]
        high          = body.rep_range_high if body.rep_range_high is not None else prog["rep_range_high"]
        frequency     = body.frequency if body.frequency is not None else prog["frequency"]
        sets_count    = body.sets_count if body.sets_count is not None else prog["sets_count"]
        increment     = body.increment if body.increment is not None else prog["increment"]
        deload        = body.deload_enabled if body.deload_enabled is not None else bool(prog["deload_enabled"])
        if high <= low:
            raise HTTPException(400, "Верх диапазона повторов должен быть больше низа")

        conn.execute(
            "UPDATE progressions SET goal=?, rep_range_low=?, rep_range_high=?, frequency=?, sets_count=?, "
            "increment=?, deload_enabled=?, updated_at=datetime('now') WHERE id=?",
            (goal, low, high, frequency, sets_count, increment, int(deload), progression_id)
        )

        nxt = conn.execute(
            "SELECT MIN(session_index) i FROM progression_sessions WHERE progression_id=? AND status='pending'",
            (progression_id,)
        ).fetchone()["i"]
        if nxt is not None:
            cw = prog["current_weight"]
            eff_sets = sets_count * 2 if prog["unilateral"] else sets_count
            detail = json.loads(prog["current_reps_detail"]) if prog["current_reps_detail"] else [prog["current_reps"] or low] * (prog["sets_count"] or 1)
            cw, detail = normalize_range_detail(cw, detail, low, high, increment)
            if len(detail) < eff_sets:
                detail = detail + [detail[-1]] * (eff_sets - len(detail))
            elif len(detail) > eff_sets:
                detail = detail[:eff_sets]
            conn.execute(
                "UPDATE progressions SET current_weight=?, current_reps=?, current_reps_detail=? WHERE id=?",
                (cw, detail[0], json.dumps(detail), progression_id)
            )
            conn.execute(
                "DELETE FROM progression_sessions WHERE progression_id=? AND status='pending'", (progression_id,)
            )
            sessions = generate_remaining_sessions(
                prog["exercise_type"], frequency, low, high, increment, eff_sets,
                prog["total_sessions"], nxt, cw, detail
            )
            _insert_sessions(conn, progression_id, sessions, unilateral=bool(prog["unilateral"]), display_sets_count=sets_count)
    return {"ok": True}


@app.delete("/progressions/{progression_id}")
def delete_progression(progression_id: int, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        _require_premium(conn, uid)
        _get_owned_progression(conn, pid, progression_id)
        conn.execute("DELETE FROM progression_sessions WHERE progression_id=?", (progression_id,))
        conn.execute("DELETE FROM progressions WHERE id=?", (progression_id,))
    return {"ok": True}


@app.post("/progressions/{progression_id}/sessions/{session_id}/log")
def log_progression_session(progression_id: int, session_id: int, body: SessionLogIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        _require_premium(conn, uid)
        prog = _get_owned_progression(conn, pid, progression_id)
        session = conn.execute(
            "SELECT * FROM progression_sessions WHERE id=? AND progression_id=?", (session_id, progression_id)
        ).fetchone()
        if not session:
            raise HTTPException(404, "Сессия не найдена")
        if session["status"] != "pending":
            raise HTTPException(400, "Эта сессия уже отыграна или пропущена")

        uni = bool(prog["unilateral"])
        if body.actual_detail and uni:
            # для отображения факта пользователю сохраняем в исходной Л/П форме, а не
            # в "рассортированной по слабой/сильной стороне" — та нужна только для расчёта
            stored_detail = [
                {"weightL": s.weightL if s.weightL is not None else s.weight,
                 "repsL": s.repsL if s.repsL is not None else s.reps,
                 "weightR": s.weightR if s.weightR is not None else s.weight,
                 "repsR": s.repsR if s.repsR is not None else s.reps,
                 "bilateral": True}
                for s in body.actual_detail
            ]
        else:
            stored_detail = [{"weight": s.weight, "reps": s.reps} for s in body.actual_detail] if body.actual_detail else None

        conn.execute(
            "UPDATE progression_sessions SET status='done', actual_weight=?, actual_reps=?, actual_sets=?, "
            "actual_rir=?, workout_id=?, actual_detail=?, completed_at=datetime('now') WHERE id=?",
            (body.actual_weight, body.actual_reps, body.actual_sets, body.actual_rir, body.workout_id,
             json.dumps(stored_detail, ensure_ascii=False) if stored_detail else None,
             session_id)
        )

        if prog["mode"] == "calculated" and session["role"] in (None, "heavy"):
            eff_sets = prog["sets_count"] * 2 if uni else prog["sets_count"]
            if session["planned_detail"]:
                pd = json.loads(session["planned_detail"])
                planned_reps_detail = [x for d in pd for x in (d["repsL"], d["repsR"])] if uni else [d["reps"] for d in pd]
            else:
                planned_reps_detail = [session["planned_reps"]] * eff_sets

            if body.actual_detail:
                if uni:
                    actual_records = [{"weight": s.weight, "reps": s.reps, "weightL": s.weightL,
                                        "repsL": s.repsL, "weightR": s.weightR, "repsR": s.repsR}
                                       for s in body.actual_detail]
                    _, actual_detail = _unpack_unilateral_actual(actual_records)
                else:
                    actual_detail = [{"weight": s.weight, "reps": s.reps} for s in body.actual_detail]
            else:
                # фолбэк на случай, если фронт не прислал детализацию по подходам —
                # раскладываем агрегат на нужное число подходов одним и тем же значением
                actual_detail = [{"weight": body.actual_weight, "reps": body.actual_reps}] * (body.actual_sets or 1)
                if uni:
                    actual_detail = actual_detail * 2  # без детализации по сторонам считаем обе стороны равными

            new_w, new_r, new_fail = adapt_actual_detail(
                session["planned_weight"], planned_reps_detail, actual_detail,
                prog["rep_range_low"], prog["rep_range_high"], prog["increment"], prog["fail_streak"]
            )
            conn.execute(
                "UPDATE progressions SET current_weight=?, current_reps=?, current_reps_detail=?, "
                "fail_streak=?, updated_at=datetime('now') WHERE id=?",
                (new_w, new_r[0], json.dumps(new_r), new_fail, progression_id)
            )
            conn.execute(
                "DELETE FROM progression_sessions WHERE progression_id=? AND status='pending' AND session_index>?",
                (progression_id, session["session_index"])
            )
            if session["session_index"] < prog["total_sessions"]:
                sessions = generate_remaining_sessions(
                    prog["exercise_type"], prog["frequency"], prog["rep_range_low"], prog["rep_range_high"],
                    prog["increment"], eff_sets, prog["total_sessions"],
                    session["session_index"] + 1, new_w, new_r
                )
                _insert_sessions(conn, progression_id, sessions, unilateral=uni, display_sets_count=prog["sets_count"])

        _maybe_complete(conn, progression_id)

        updated = _get_owned_progression(conn, pid, progression_id)
        sessions_rows = conn.execute(
            "SELECT * FROM progression_sessions WHERE progression_id=? ORDER BY session_index", (progression_id,)
        ).fetchall()
    d = _serialize_progression(updated)
    d["sessions"] = [_serialize_session(s) for s in sessions_rows]
    return d


@app.post("/progressions/{progression_id}/sessions/{session_id}/skip")
def skip_progression_session(progression_id: int, session_id: int, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        _require_premium(conn, uid)
        _get_owned_progression(conn, pid, progression_id)
        session = conn.execute(
            "SELECT * FROM progression_sessions WHERE id=? AND progression_id=?", (session_id, progression_id)
        ).fetchone()
        if not session or session["status"] != "pending":
            raise HTTPException(400, "Сессию нельзя пропустить")
        conn.execute("UPDATE progression_sessions SET status='skipped' WHERE id=?", (session_id,))
        _maybe_complete(conn, progression_id)
    return {"ok": True}


@app.post("/progressions/{progression_id}/undo-last")
def undo_last_log(progression_id: int, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        _require_premium(conn, uid)
        prog = _get_owned_progression(conn, pid, progression_id)
        sessions = conn.execute(
            "SELECT * FROM progression_sessions WHERE progression_id=? ORDER BY session_index", (progression_id,)
        ).fetchall()
        done = [s for s in sessions if s["status"] == "done"]
        if not done:
            raise HTTPException(400, "Нечего отменять — ни одной отыгранной сессии")
        target = max(done, key=lambda s: s["session_index"])

        conn.execute(
            "UPDATE progression_sessions SET status='pending', actual_weight=NULL, actual_reps=NULL, "
            "actual_sets=NULL, actual_rir=NULL, workout_id=NULL, completed_at=NULL WHERE id=?", (target["id"],)
        )

        if prog["mode"] == "calculated" and target["role"] in (None, "heavy"):
            # переигрываем историю до target, чтобы честно восстановить current_weight/current_reps_detail
            uni = bool(prog["unilateral"])
            eff_sets = prog["sets_count"] * 2 if uni else prog["sets_count"]
            w = prog["start_weight"]
            r = [prog["start_reps"]] * eff_sets
            fail = 0
            for s in sessions:
                if s["session_index"] >= target["session_index"]:
                    break
                if s["status"] == "done" and s["role"] in (None, "heavy"):
                    if s["planned_detail"]:
                        pd = json.loads(s["planned_detail"])
                        planned_reps_detail = [x for d in pd for x in (d["repsL"], d["repsR"])] if uni else [d["reps"] for d in pd]
                    else:
                        planned_reps_detail = [s["planned_reps"]] * eff_sets
                    if s["actual_detail"]:
                        stored = json.loads(s["actual_detail"])
                        actual_detail = _unpack_unilateral_actual(stored)[1] if uni else stored
                    else:
                        actual_detail = [{"weight": s["actual_weight"], "reps": s["actual_reps"]}] * (s["actual_sets"] or 1)
                        if uni:
                            actual_detail = actual_detail * 2
                    w, r, fail = adapt_actual_detail(
                        s["planned_weight"], planned_reps_detail, actual_detail,
                        prog["rep_range_low"], prog["rep_range_high"], prog["increment"], fail
                    )
            conn.execute(
                "UPDATE progressions SET current_weight=?, current_reps=?, current_reps_detail=?, fail_streak=?, "
                "status='active', updated_at=datetime('now') WHERE id=?",
                (w, r[0], json.dumps(r), fail, progression_id)
            )
            conn.execute(
                "DELETE FROM progression_sessions WHERE progression_id=? AND status='pending' AND session_index>=?",
                (progression_id, target["session_index"])
            )
            new_sessions = generate_remaining_sessions(
                prog["exercise_type"], prog["frequency"], prog["rep_range_low"], prog["rep_range_high"],
                prog["increment"], eff_sets, prog["total_sessions"], target["session_index"], w, r
            )
            _insert_sessions(conn, progression_id, new_sessions, unilateral=uni, display_sets_count=prog["sets_count"])
        else:
            conn.execute("UPDATE progressions SET status='active' WHERE id=? AND status='completed'", (progression_id,))
    return {"ok": True}


@app.post("/progressions/{progression_id}/new-cycle")
def new_progression_cycle(progression_id: int, body: NewCycleIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        pid = get_active_profile_id(conn, uid)
        _require_premium(conn, uid)
        prog = _get_owned_progression(conn, pid, progression_id)
        if prog["mode"] != "calculated" or prog["status"] != "completed":
            raise HTTPException(400, "Новый цикл можно начать только для завершённой расчётной прогрессии")

        goal        = body.goal or prog["goal"]
        low         = body.rep_range_low if body.rep_range_low is not None else prog["rep_range_low"]
        high        = body.rep_range_high if body.rep_range_high is not None else prog["rep_range_high"]
        frequency   = body.frequency if body.frequency is not None else prog["frequency"]
        sets_count  = body.sets_count if body.sets_count is not None else prog["sets_count"]
        increment   = body.increment if body.increment is not None else prog["increment"]
        deload      = body.deload_enabled if body.deload_enabled is not None else bool(prog["deload_enabled"])
        weeks       = body.weeks if body.weeks is not None else max(1, (prog["total_sessions"] or frequency) // (prog["frequency"] or 1))
        total_sessions = weeks * frequency
        uni = bool(prog["unilateral"])
        eff_sets = sets_count * 2 if uni else sets_count
        start_w = prog["current_weight"]
        start_r_detail = json.loads(prog["current_reps_detail"]) if prog["current_reps_detail"] else [prog["current_reps"]] * (prog["sets_count"] or 1)
        if len(start_r_detail) < eff_sets:
            start_r_detail = start_r_detail + [start_r_detail[-1]] * (eff_sets - len(start_r_detail))
        elif len(start_r_detail) > eff_sets:
            start_r_detail = start_r_detail[:eff_sets]

        try:
            cur = conn.execute(
                "INSERT INTO progressions (profile_id, exercise_name, exercise_name_lc, mode, status, "
                "exercise_type, goal, rep_unit, rep_range_low, rep_range_high, frequency, sets_count, "
                "increment, start_weight, start_reps, total_sessions, deload_enabled, unilateral, current_weight, "
                "current_reps, current_reps_detail) "
                "VALUES (?,?,?,'calculated','active',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, prog["exercise_name"], prog["exercise_name_lc"], prog["exercise_type"], goal, prog["rep_unit"],
                 low, high, frequency, sets_count, increment, start_w, start_r_detail[0], total_sessions, int(deload),
                 int(uni), start_w, start_r_detail[0], json.dumps(start_r_detail))
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "У этого упражнения уже есть активная прогрессия")
        new_id = cur.lastrowid
        sessions = generate_remaining_sessions(
            prog["exercise_type"], frequency, low, high, increment, eff_sets, total_sessions, 1, start_w, start_r_detail
        )
        _insert_sessions(conn, new_id, sessions, unilateral=uni, display_sets_count=sets_count)
    return {"ok": True, "id": new_id}


@app.get("/")
def root():
    return {"status": "ok", "service": "gym-diary-api"}


# ── Админка ─────────────────────────────────────────────────────────────────
# Встроенная в тот же FastAPI-процесс админка для прямого управления базой:
# просмотр и редактирование любых таблиц, произвольные SQL-запросы, скачивание
# и загрузка файла базы целиком. Защищена паролем через Basic Auth.
#
# Специально не используются внешние инструменты (sqlite-web/Adminer и т.п.)
# и почти никаких новых зависимостей (кроме python-multipart для загрузки
# файла) — весь код живёт в этом же main.py. Поэтому переезд с Railway на
# VPS/другой хостинг не требует ничего, кроме переноса переменных окружения
# (BOT_TOKEN, DB_PATH, ADMIN_PASSWORD) и самого файла базы.
#
# ВАЖНО: задай ADMIN_PASSWORD в Railway → Variables (длинный случайный пароль).
# Без него админка целиком отключена (см. verify_admin ниже).

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_admin_security = HTTPBasic()


def verify_admin(credentials: HTTPBasicCredentials = Depends(_admin_security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(500, "ADMIN_PASSWORD не задан на сервере (Railway → Variables)")
    if not secrets.compare_digest(credentials.password, ADMIN_PASSWORD):
        raise HTTPException(401, "Неверный пароль", headers={"WWW-Authenticate": "Basic"})
    return True


ADMIN_PAGE_SIZE = 50

ADMIN_CSS = """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0A0A0A;color:#FFF;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:15px;padding:24px;max-width:1100px;margin:0 auto}
a{color:#FFF;text-decoration:none}
h1{font-size:20px;font-weight:700;margin-bottom:20px;letter-spacing:-.02em}
h2{font-size:15px;font-weight:600;margin:24px 0 12px;color:#AAA}
.nav{display:flex;gap:16px;margin-bottom:24px;border-bottom:1px solid #2A2A2A;padding-bottom:14px;font-size:13px}
.nav a{color:#888}
.nav a:hover{color:#FFF}
table{width:100%;border-collapse:collapse;margin-bottom:16px;font-size:13px}
th,td{border:1px solid #2A2A2A;padding:8px 10px;text-align:left;vertical-align:top;max-width:280px;overflow-wrap:break-word}
th{color:#888;font-weight:600;background:#111}
tr:hover td{background:#0F0F0F}
.card{border:1px solid #2A2A2A;padding:14px 16px;margin-bottom:10px;background:#111}
.btn{display:inline-block;padding:9px 14px;border:1px solid #FFF;background:transparent;color:#FFF;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}
.btn:hover{background:#FFF;color:#000}
.btn.danger{border-color:#FF4444;color:#FF4444}
.btn.danger:hover{background:#FF4444;color:#FFF}
.btn.ghost{border-color:#333;color:#888}
.btn.ghost:hover{border-color:#888;color:#FFF}
input,textarea{width:100%;background:#111;border:1px solid #2A2A2A;color:#FFF;font-size:13px;padding:9px 10px;font-family:inherit;margin-bottom:10px}
label{display:block;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#666;margin-bottom:4px}
.field{margin-bottom:14px}
.warn{border:1px solid #4A3A1A;background:#161208;color:#E0A030;padding:10px 12px;font-size:13px;margin-bottom:16px}
.err{border:1px solid #4A1A1A;background:#160808;color:#FF6B6B;padding:10px 12px;font-size:13px;margin-bottom:16px}
.ok{border:1px solid #1A4A2A;background:#081608;color:#5FD088;padding:10px 12px;font-size:13px;margin-bottom:16px}
.pager{display:flex;gap:10px;align-items:center;margin-bottom:16px;font-size:13px;color:#888}
.actions{display:flex;gap:6px}
.actions form{display:inline}
</style>
"""


def admin_page(title: str, body: str) -> HTMLResponse:
    nav = (
        '<div class="nav">'
        '<a href="/admin">Дашборд</a>'
        '<a href="/admin/premium">Премиум</a>'
        '<a href="/admin/sql">SQL-запрос</a>'
        '<a href="/admin/db/download">Скачать базу</a>'
        '<a href="/admin/db/upload">Загрузить базу</a>'
        "</div>"
    )
    return HTMLResponse(
        f"<html><head><meta charset='utf-8'><title>{html.escape(title)}</title>{ADMIN_CSS}</head>"
        f"<body><h1>{html.escape(title)}</h1>{nav}{body}</body></html>"
    )


def _valid_table(conn, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _table_names(conn):
    return [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()]


def _table_columns(conn, table: str):
    # table уже проверен через _valid_table перед вызовом — подстановка в PRAGMA безопасна
    return [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_dashboard(_: bool = Depends(verify_admin)):
    with get_db() as conn:
        tables = _table_names(conn)
        cards = ""
        for t in tables:
            count = conn.execute(f'SELECT COUNT(*) c FROM "{t}"').fetchone()["c"]
            cards += (
                f'<div class="card"><a href="/admin/table/{t}"><strong>{html.escape(t)}</strong></a>'
                f" — {count} записей</div>"
            )
    return admin_page("Админка · gym-diary", f"<h2>Таблицы</h2>{cards}")


@app.get("/admin/premium", response_class=HTMLResponse, include_in_schema=False)
def admin_premium_page(_: bool = Depends(verify_admin)):
    with get_db() as conn:
        users = conn.execute(
            "SELECT user_id, username, first_name, is_premium FROM users ORDER BY first_name, username"
        ).fetchall()

    rows_html = ""
    for u in users:
        label = html.escape(u["first_name"] or u["username"] or u["user_id"])
        uname = f" (@{html.escape(u['username'])})" if u["username"] else ""
        checked = "checked" if u["is_premium"] else ""
        rows_html += (
            f'<label style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #1A1A1A;'
            f'text-transform:none;font-size:14px;color:#FFF">'
            f'<input type="checkbox" name="premium" value="{html.escape(u["user_id"])}" {checked} '
            f'style="width:auto;margin:0">{label}{uname}'
            f"</label>"
        )

    body = (
        '<div class="card">'
        '<form method="post" action="/admin/premium/bulk" style="display:flex;gap:8px">'
        '<button class="btn" name="action" value="enable_all" type="submit">Включить всем</button>'
        '<button class="btn ghost" name="action" value="disable_all" type="submit">Выключить всем</button>'
        "</form></div>"
        '<form method="post" action="/admin/premium">'
        f"{rows_html}"
        '<div style="height:14px"></div>'
        '<button class="btn" type="submit">Сохранить</button></form>'
    )
    return admin_page("Премиум-пользователи", body)


@app.post("/admin/premium", response_class=HTMLResponse, include_in_schema=False)
async def admin_premium_save(request: Request, _: bool = Depends(verify_admin)):
    form = await request.form()
    checked_ids = set(form.getlist("premium"))
    with get_db() as conn:
        all_ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
        for uid in all_ids:
            conn.execute(
                "UPDATE users SET is_premium=? WHERE user_id=?",
                (1 if uid in checked_ids else 0, uid)
            )
    return RedirectResponse("/admin/premium", status_code=303)


@app.post("/admin/premium/bulk", response_class=HTMLResponse, include_in_schema=False)
async def admin_premium_bulk(request: Request, _: bool = Depends(verify_admin)):
    form = await request.form()
    action = form.get("action")
    with get_db() as conn:
        if action == "enable_all":
            conn.execute("UPDATE users SET is_premium=1")
        elif action == "disable_all":
            conn.execute("UPDATE users SET is_premium=0")
    return RedirectResponse("/admin/premium", status_code=303)


@app.get("/admin/table/{table}", response_class=HTMLResponse, include_in_schema=False)
def admin_table_view(table: str, page: int = 1, _: bool = Depends(verify_admin)):
    with get_db() as conn:
        if not _valid_table(conn, table):
            raise HTTPException(404, "Таблицы не существует")
        cols = _table_columns(conn, table)
        total = conn.execute(f'SELECT COUNT(*) c FROM "{table}"').fetchone()["c"]
        page = max(page, 1)
        offset = (page - 1) * ADMIN_PAGE_SIZE
        rows = conn.execute(
            f'SELECT rowid AS _rowid, * FROM "{table}" ORDER BY rowid DESC LIMIT ? OFFSET ?',
            (ADMIN_PAGE_SIZE, offset)
        ).fetchall()

    thead = "".join(f"<th>{html.escape(c)}</th>" for c in cols) + "<th></th>"
    trs = ""
    for r in rows:
        tds = "".join(
            f"<td>{html.escape(str(r[c])) if r[c] is not None else ''}</td>" for c in cols
        )
        rid = r["_rowid"]
        actions = (
            '<div class="actions">'
            f'<a class="btn ghost" href="/admin/table/{table}/edit/{rid}">Изм.</a>'
            f'<form method="post" action="/admin/table/{table}/delete/{rid}" '
            f"onsubmit=\"return confirm('Удалить запись?')\">"
            '<button class="btn danger" type="submit">Удалить</button></form>'
            "</div>"
        )
        trs += f"<tr>{tds}<td>{actions}</td></tr>"

    pages = max((total - 1) // ADMIN_PAGE_SIZE + 1, 1)
    pager = f'<div class="pager"><span>Стр. {page} из {pages} · {total} записей</span>'
    if page > 1:
        pager += f'<a class="btn ghost" href="?page={page-1}">← Назад</a>'
    if page < pages:
        pager += f'<a class="btn ghost" href="?page={page+1}">Вперёд →</a>'
    pager += "</div>"

    body = (
        f'<a class="btn" href="/admin/table/{table}/new">+ Новая запись</a>'
        f'<div style="height:16px"></div>{pager}'
        f"<table><thead><tr>{thead}</tr></thead><tbody>{trs}</tbody></table>{pager}"
    )
    return admin_page(f"Таблица: {table}", body)


@app.get("/admin/table/{table}/new", response_class=HTMLResponse, include_in_schema=False)
def admin_row_new_form(table: str, _: bool = Depends(verify_admin)):
    with get_db() as conn:
        if not _valid_table(conn, table):
            raise HTTPException(404, "Таблицы не существует")
        cols = _table_columns(conn, table)

    fields = "".join(
        f'<div class="field"><label>{html.escape(c)}</label><input name="{html.escape(c)}"></div>'
        for c in cols
    )
    body = (
        f'<form method="post" action="/admin/table/{table}/new">{fields}'
        '<button class="btn" type="submit">Создать</button> '
        f'<a class="btn ghost" href="/admin/table/{table}">Отмена</a></form>'
        '<div style="height:12px"></div>'
        '<p style="color:#555;font-size:12px">Пустые поля не отправляются — так автоинкрементные id остаются пустыми и подставляются сами.</p>'
    )
    return admin_page(f"Новая запись · {table}", body)


@app.post("/admin/table/{table}/new", response_class=HTMLResponse, include_in_schema=False)
async def admin_row_new_submit(table: str, request: Request, _: bool = Depends(verify_admin)):
    form = await request.form()
    with get_db() as conn:
        if not _valid_table(conn, table):
            raise HTTPException(404, "Таблицы не существует")
        cols = _table_columns(conn, table)
        cols_present = [c for c in cols if (form.get(c) or "") != ""] or cols
        col_list = ", ".join(f'"{c}"' for c in cols_present)
        placeholders = ", ".join("?" for _ in cols_present)
        values = [form.get(c, "") for c in cols_present]
        try:
            conn.execute(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})', values)
        except sqlite3.Error as e:
            return admin_page(f"Новая запись · {table}", f'<div class="err">Ошибка: {html.escape(str(e))}</div>'
                               f'<a class="btn ghost" href="/admin/table/{table}/new">Назад</a>')
    return RedirectResponse(f"/admin/table/{table}", status_code=303)


@app.get("/admin/table/{table}/edit/{rowid}", response_class=HTMLResponse, include_in_schema=False)
def admin_row_edit_form(table: str, rowid: int, _: bool = Depends(verify_admin)):
    with get_db() as conn:
        if not _valid_table(conn, table):
            raise HTTPException(404, "Таблицы не существует")
        cols = _table_columns(conn, table)
        row = conn.execute(f'SELECT rowid AS _rowid, * FROM "{table}" WHERE rowid=?', (rowid,)).fetchone()
        if not row:
            raise HTTPException(404, "Запись не найдена")

    fields = ""
    for c in cols:
        val = row[c] if row[c] is not None else ""
        sval = html.escape(str(val))
        if isinstance(val, str) and len(val) > 60:
            fields += f'<div class="field"><label>{html.escape(c)}</label><textarea name="{html.escape(c)}" rows="4">{sval}</textarea></div>'
        else:
            fields += f'<div class="field"><label>{html.escape(c)}</label><input name="{html.escape(c)}" value="{sval}"></div>'

    body = (
        f'<form method="post" action="/admin/table/{table}/edit/{rowid}">{fields}'
        '<button class="btn" type="submit">Сохранить</button> '
        f'<a class="btn ghost" href="/admin/table/{table}">Отмена</a></form>'
    )
    return admin_page(f"Изменить запись · {table}", body)


@app.post("/admin/table/{table}/edit/{rowid}", response_class=HTMLResponse, include_in_schema=False)
async def admin_row_edit_submit(table: str, rowid: int, request: Request, _: bool = Depends(verify_admin)):
    form = await request.form()
    with get_db() as conn:
        if not _valid_table(conn, table):
            raise HTTPException(404, "Таблицы не существует")
        cols = _table_columns(conn, table)
        set_clause = ", ".join(f'"{c}"=?' for c in cols)
        values = [form.get(c, "") for c in cols] + [rowid]
        try:
            conn.execute(f'UPDATE "{table}" SET {set_clause} WHERE rowid=?', values)
        except sqlite3.Error as e:
            return admin_page(f"Изменить запись · {table}", f'<div class="err">Ошибка: {html.escape(str(e))}</div>'
                               f'<a class="btn ghost" href="/admin/table/{table}/edit/{rowid}">Назад</a>')
    return RedirectResponse(f"/admin/table/{table}", status_code=303)


@app.post("/admin/table/{table}/delete/{rowid}", include_in_schema=False)
def admin_row_delete(table: str, rowid: int, _: bool = Depends(verify_admin)):
    with get_db() as conn:
        if not _valid_table(conn, table):
            raise HTTPException(404, "Таблицы не существует")
        conn.execute(f'DELETE FROM "{table}" WHERE rowid=?', (rowid,))
    return RedirectResponse(f"/admin/table/{table}", status_code=303)


ADMIN_SQL_WARNING = (
    '<div class="warn">Выполняется прямо на базе. Изменяющие запросы '
    "(UPDATE/DELETE/INSERT) применяются сразу и без подтверждения — будь аккуратен, "
    "особенно без WHERE.</div>"
)


@app.get("/admin/sql", response_class=HTMLResponse, include_in_schema=False)
def admin_sql_form(_: bool = Depends(verify_admin)):
    body = (
        ADMIN_SQL_WARNING
        + '<form method="post" action="/admin/sql">'
        + '<textarea name="query" rows="6" placeholder="SELECT * FROM workouts LIMIT 20"></textarea>'
        + '<button class="btn" type="submit">Выполнить</button></form>'
    )
    return admin_page("SQL-запрос", body)


@app.post("/admin/sql", response_class=HTMLResponse, include_in_schema=False)
async def admin_sql_run(request: Request, _: bool = Depends(verify_admin)):
    form = await request.form()
    query = (form.get("query") or "").strip()
    result_html = ""
    if query:
        try:
            with get_db() as conn:
                cur = conn.execute(query)
                if cur.description:
                    rows = cur.fetchall()
                    cols = [d[0] for d in cur.description]
                    thead = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
                    trs = "".join(
                        "<tr>" + "".join(
                            f"<td>{html.escape(str(v)) if v is not None else ''}</td>" for v in r
                        ) + "</tr>"
                        for r in rows
                    )
                    result_html = (
                        f'<div class="ok">{len(rows)} строк</div>'
                        f"<table><thead><tr>{thead}</tr></thead><tbody>{trs}</tbody></table>"
                    )
                else:
                    result_html = f'<div class="ok">Выполнено. Изменено строк: {cur.rowcount}</div>'
        except Exception as e:
            result_html = f'<div class="err">Ошибка: {html.escape(str(e))}</div>'

    body = (
        ADMIN_SQL_WARNING
        + f'<form method="post" action="/admin/sql"><textarea name="query" rows="6">{html.escape(query)}</textarea>'
        + '<button class="btn" type="submit">Выполнить</button></form>'
        + f'<div style="height:16px"></div>{result_html}'
    )
    return admin_page("SQL-запрос", body)


@app.get("/admin/db/download", include_in_schema=False)
def admin_db_download(_: bool = Depends(verify_admin)):
    if not os.path.exists(DB_PATH):
        raise HTTPException(404, "Файл базы не найден")
    with get_db() as conn:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    return FileResponse(DB_PATH, filename="gym.db", media_type="application/octet-stream")


@app.get("/admin/db/upload", response_class=HTMLResponse, include_in_schema=False)
def admin_db_upload_form(_: bool = Depends(verify_admin)):
    body = (
        '<div class="warn">Загрузка полностью заменит текущую базу данных новым файлом. '
        "Старая версия автоматически сохранится рядом (файл с суффиксом .bak-&lt;время&gt;), "
        "если понадобится откатиться.</div>"
        '<form method="post" action="/admin/db/upload" enctype="multipart/form-data">'
        '<input type="file" name="file" accept=".db,.sqlite,.sqlite3" required>'
        '<button class="btn danger" type="submit">Заменить базу</button></form>'
    )
    return admin_page("Загрузить базу", body)


@app.post("/admin/db/upload", response_class=HTMLResponse, include_in_schema=False)
async def admin_db_upload_submit(file: UploadFile = File(...), _: bool = Depends(verify_admin)):
    content = await file.read()
    if content[:16] != b"SQLite format 3\x00":
        return admin_page(
            "Загрузить базу",
            '<div class="err">Файл не похож на базу SQLite — ничего не изменено.</div>'
            '<a class="btn ghost" href="/admin/db/upload">Назад</a>',
        )

    backup_path = f"{DB_PATH}.bak-{int(time.time())}"
    if os.path.exists(DB_PATH):
        shutil.copy(DB_PATH, backup_path)
    with open(DB_PATH, "wb") as f:
        f.write(content)

    body = (
        f'<div class="ok">База заменена. Резервная копия старой версии: {html.escape(backup_path)}</div>'
        '<a class="btn" href="/admin">К дашборду</a>'
    )
    return admin_page("Готово", body)
