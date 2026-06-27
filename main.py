"""
Gym diary backend
FastAPI + SQLite
"""

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3, json, hmac, hashlib, urllib.parse, os


app = FastAPI()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "gym.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():

    with get_db() as conn:

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS workouts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            exercises TEXT NOT NULL DEFAULT '[]'
        );


        CREATE TABLE IF NOT EXISTS measurements(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}'
        );

        """)


init_db()



def verify_telegram(init_data):

    if not BOT_TOKEN:

        try:
            params=dict(
                urllib.parse.parse_qsl(init_data)
            )

            user=json.loads(
                params.get("user","{}")
            )

            return str(user.get("id","dev_user"))

        except:
            return "dev_user"



    params=dict(
        urllib.parse.parse_qsl(
            init_data,
            keep_blank_values=True
        )
    )


    hash_value=params.pop("hash","")


    data_check="\n".join(
        f"{k}={v}"
        for k,v in sorted(params.items())
    )


    secret=hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256
    ).digest()


    calculated=hmac.new(
        secret,
        data_check.encode(),
        hashlib.sha256
    ).hexdigest()


    if not hmac.compare_digest(
        calculated,
        hash_value
    ):
        return None


    user=json.loads(
        params.get("user","{}")
    )

    return str(user.get("id"))



def get_user_id(
    x_init_data:str=Header(...)
):

    uid=verify_telegram(x_init_data)

    if not uid:
        raise HTTPException(
            status_code=401,
            detail="Invalid telegram"
        )

    return uid



class WorkoutIn(BaseModel):

    id:int=-1
    name:str
    date:str
    exercises:list



class MeasurementIn(BaseModel):

    id:int=-1
    name:str
    date:str
    data:dict={}



@app.get("/workouts")
def get_workouts(
    x_init_data:str=Header(...)
):

    uid=get_user_id(x_init_data)


    with get_db() as conn:

        rows=conn.execute(
            """
            SELECT *
            FROM workouts
            WHERE user_id=?
            ORDER BY date ASC
            """,
            (uid,)
        ).fetchall()


    return [
        {
            "id":r["id"],
            "name":r["name"],
            "date":r["date"],
            "exercises":json.loads(r["exercises"])
        }
        for r in rows
    ]



@app.post("/workouts")
def save_workout(
    w:WorkoutIn,
    x_init_data:str=Header(...)
):

    uid=get_user_id(x_init_data)


    with get_db() as conn:


        if w.id != -1:


            conn.execute(
                """
                UPDATE workouts
                SET name=?,date=?,exercises=?
                WHERE id=? AND user_id=?
                """,
                (
                    w.name,
                    w.date,
                    json.dumps(
                        w.exercises,
                        ensure_ascii=False
                    ),
                    w.id,
                    uid
                )
            )


            new_id=w.id


        else:


            cur=conn.execute(
                """
                INSERT INTO workouts
                (user_id,name,date,exercises)
                VALUES(?,?,?,?)
                """,
                (
                    uid,
                    w.name,
                    w.date,
                    json.dumps(
                        w.exercises,
                        ensure_ascii=False
                    )
                )
            )


            new_id=cur.lastrowid



    return {
        "id":new_id,
        "name":w.name,
        "date":w.date,
        "exercises":w.exercises
    }




@app.delete("/workouts/{id}")
def delete_workout(
    id:int,
    x_init_data:str=Header(...)
):

    uid=get_user_id(x_init_data)

    with get_db() as conn:

        conn.execute(
            """
            DELETE FROM workouts
            WHERE id=? AND user_id=?
            """,
            (id,uid)
        )


    return {"ok":True}



@app.get("/measurements")
def get_measurements(
    x_init_data:str=Header(...)
):

    uid=get_user_id(x_init_data)

    with get_db() as conn:

        rows=conn.execute(
            """
            SELECT *
            FROM measurements
            WHERE user_id=?
            ORDER BY date ASC
            """,
            (uid,)
        ).fetchall()


    result=[]

    for r in rows:

        item={
            "id":r["id"],
            "name":r["name"],
            "date":r["date"]
        }

        item.update(
            json.loads(r["data"])
        )

        result.append(item)


    return result



@app.post("/measurements")
def save_measurement(
    m:MeasurementIn,
    x_init_data:str=Header(...)
):

    uid=get_user_id(x_init_data)


    with get_db() as conn:


        if m.id != -1:

            conn.execute(
                """
                UPDATE measurements
                SET name=?,date=?,data=?
                WHERE id=? AND user_id=?
                """,
                (
                    m.name,
                    m.date,
                    json.dumps(
                        m.data,
                        ensure_ascii=False
                    ),
                    m.id,
                    uid
                )
            )


            new_id=m.id


        else:


            cur=conn.execute(
                """
                INSERT INTO measurements
                (user_id,name,date,data)
                VALUES(?,?,?,?)
                """,
                (
                    uid,
                    m.name,
                    m.date,
                    json.dumps(
                        m.data,
                        ensure_ascii=False
                    )
                )
            )


            new_id=cur.lastrowid


    return {
        "id":new_id,
        "name":m.name,
        "date":m.date,
        **m.data
    }



@app.delete("/measurements/{id}")
def delete_measurement(
    id:int,
    x_init_data:str=Header(...)
):

    uid=get_user_id(x_init_data)

    with get_db() as conn:

        conn.execute(
            """
            DELETE FROM measurements
            WHERE id=? AND user_id=?
            """,
            (id,uid)
        )


    return {"ok":True}



@app.patch("/exercises/rename")
def rename_exercise(
    body:dict,
    x_init_data:str=Header(...)
):

    uid=get_user_id(x_init_data)

    old=body["old"]
    new=body["new"]


    with get_db() as conn:


        rows=conn.execute(
            """
            SELECT id,exercises
            FROM workouts
            WHERE user_id=?
            """,
            (uid,)
        ).fetchall()



        for r in rows:

            exercises=json.loads(
                r["exercises"]
            )


            changed=False


            for e in exercises:

                if e.get("name")==old:

                    e["name"]=new
                    changed=True



            if changed:

                conn.execute(
                    """
                    UPDATE workouts
                    SET exercises=?
                    WHERE id=?
                    """,
                    (
                        json.dumps(
                            exercises,
                            ensure_ascii=False
                        ),
                        r["id"]
                    )
                )


    return {"ok":True}