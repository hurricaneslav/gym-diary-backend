"""
Бэкенд дневника тренировок
FastAPI + SQLite

Поддерживает несколько профилей на одного Telegram-пользователя
(например тренер ведёт дневники нескольких клиентов) и систему друзей,
где можно посмотреть "основной" профиль друга согласно его настройкам видимости.
"""

from fastapi import FastAPI, HTTPException, Header, Depends, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
import sqlite3, json, hmac, hashlib, urllib.parse, os, secrets, shutil, time, html

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
        """)
        # Миграция: добавляем profile_id в старые таблицы, если его ещё нет
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(workouts)").fetchall()]
        if "profile_id" not in cols:
            conn.execute("ALTER TABLE workouts ADD COLUMN profile_id INTEGER")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(measurements)").fetchall()]
        if "profile_id" not in cols:
            conn.execute("ALTER TABLE measurements ADD COLUMN profile_id INTEGER")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(profiles)").fetchall()]
        if "show_measurements" not in cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN show_measurements INTEGER NOT NULL DEFAULT 1")

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
