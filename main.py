"""
Бэкенд дневника тренировок
FastAPI + SQLite
"""

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3, json, hmac, hashlib, urllib.parse, os

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
        """)
    print("✅ База данных готова")

init_db()


# ── Проверка подписи Telegram ─────────────────────────────────────────────────

def verify_telegram(init_data: str) -> Optional[str]:
    """
    Проверяет подпись initData от Telegram и возвращает user_id.
    Возвращает None если подпись неверна.
    """
    if not BOT_TOKEN:
        # В режиме разработки без токена — берём user_id напрямую
        try:
            params = dict(urllib.parse.parse_qsl(init_data))
            user   = json.loads(params.get("user", "{}"))
            return str(user.get("id", "dev_user"))
        except Exception:
            return "dev_user"

    try:
        params     = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        hash_value = params.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed   = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed, hash_value):
            return None

        user = json.loads(params.get("user", "{}"))
        return str(user.get("id"))
    except Exception:
        return None


# ── Dependency: получить user_id из заголовка ─────────────────────────────────

def get_user_id(x_init_data: str = Header(...)) -> str:
    user_id = verify_telegram(x_init_data)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid Telegram auth")
    return user_id


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


# ── Роуты: тренировки ─────────────────────────────────────────────────────────

@app.get("/workouts")
def list_workouts(user_id: str = Header(..., alias="x-user-id"), x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM workouts WHERE user_id=? ORDER BY id DESC", (uid,)
        ).fetchall()
    return [{"id":r["id"],"name":r["name"],"date":r["date"],"exercises":json.loads(r["exercises"])} for r in rows]


@app.post("/workouts")
def save_workout(w: WorkoutIn, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM workouts WHERE user_id=? AND id=?", (uid, w.id)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE workouts SET name=?, date=?, exercises=? WHERE user_id=? AND id=?",
                (w.name, w.date, json.dumps(w.exercises, ensure_ascii=False), uid, w.id)
            )
        else:
            conn.execute(
                "INSERT INTO workouts (user_id, name, date, exercises) VALUES (?,?,?,?)",
                (uid, w.name, w.date, json.dumps(w.exercises, ensure_ascii=False))
            )
    return {"ok": True}


@app.delete("/workouts/{workout_id}")
def delete_workout(workout_id: int, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        conn.execute("DELETE FROM workouts WHERE user_id=? AND id=?", (uid, workout_id))
    return {"ok": True}


# ── Роуты: замеры ─────────────────────────────────────────────────────────────

@app.get("/measurements")
def list_measurements(x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM measurements WHERE user_id=? ORDER BY id DESC", (uid,)
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
        existing = conn.execute(
            "SELECT id FROM measurements WHERE user_id=? AND id=?", (uid, m.id)
        ).fetchone()
        data_json = json.dumps(m.data, ensure_ascii=False)
        if existing:
            conn.execute(
                "UPDATE measurements SET name=?, date=?, data=? WHERE user_id=? AND id=?",
                (m.name, m.date, data_json, uid, m.id)
            )
        else:
            conn.execute(
                "INSERT INTO measurements (user_id, name, date, data) VALUES (?,?,?,?)",
                (uid, m.name, m.date, data_json)
            )
    return {"ok": True}


@app.delete("/measurements/{measurement_id}")
def delete_measurement(measurement_id: int, x_init_data: str = Header(...)):
    uid = get_user_id(x_init_data)
    with get_db() as conn:
        conn.execute("DELETE FROM measurements WHERE user_id=? AND id=?", (uid, measurement_id))
    return {"ok": True}


@app.get("/")
def root():
    return {"status": "ok", "service": "gym-diary-api"}
