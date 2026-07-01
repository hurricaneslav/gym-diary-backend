"""
Бэкенд дневника тренировок
FastAPI + SQLite

Поддерживает несколько профилей на одного Telegram-пользователя
(например тренер ведёт дневники нескольких клиентов) и систему друзей,
где можно посмотреть "основной" профиль друга согласно его настройкам видимости.
"""

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3, json, hmac, hashlib, urllib.parse, os, secrets

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
            "INSERT INTO profiles (owner_id, name, is_main, show_workouts, show_exercises, show_comments) "
            "VALUES (?,?,1,1,1,1)",
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
        """)
        # Миграция: добавляем profile_id в старые таблицы, если его ещё нет
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(workouts)").fetchall()]
        if "profile_id" not in cols:
            conn.execute("ALTER TABLE workouts ADD COLUMN profile_id INTEGER")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(measurements)").fetchall()]
        if "profile_id" not in cols:
            conn.execute("ALTER TABLE measurements ADD COLUMN profile_id INTEGER")

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
    name:           Optional[str] = None
    is_main:        Optional[bool] = None
    show_workouts:  Optional[bool] = None
    show_exercises: Optional[bool] = None
    show_comments:  Optional[bool] = None

class FriendUsernameIn(BaseModel):
    username: str

class FriendCodeIn(BaseModel):
    code: str


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
        "show_comments": bool(r["show_comments"]), "is_active": r["id"] == active_id,
    } for r in rows]


@app.post("/profiles")
def create_profile(p: ProfileCreateIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    name = p.name.strip() or "Новый профиль"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO profiles (owner_id, name, is_main, show_workouts, show_exercises, show_comments) "
            "VALUES (?,?,0,1,1,1)",
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
            return {"name": name, "profile_name": None, "show_workouts": False, "show_exercises": False, "workouts": []}

        show_w = bool(profile["show_workouts"])
        show_e = bool(profile["show_exercises"])
        show_c = bool(profile["show_comments"])

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

    return {
        "name": name,
        "profile_name": profile["name"],
        "show_workouts": show_w,
        "show_exercises": show_e,
        "workouts": workouts,
    }


@app.get("/")
def root():
    return {"status": "ok", "service": "gym-diary-api"}
