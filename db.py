import os
import time

import aiosqlite

import config

DB = os.path.join(config.DATA_DIR, "hosting.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    plan TEXT DEFAULT 'free',
    plan_until INTEGER DEFAULT 0,
    banned INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projects(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT,
    type TEXT,
    port INTEGER,
    env TEXT DEFAULT '{}',
    opts TEXT DEFAULT '',
    created INTEGER
);
"""


async def init():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    async with aiosqlite.connect(DB) as d:
        await d.executescript(SCHEMA)
        await d.commit()


async def _exec(sql, args=()):
    async with aiosqlite.connect(DB) as d:
        cur = await d.execute(sql, args)
        await d.commit()
        return cur.lastrowid


async def _all(sql, args=()):
    async with aiosqlite.connect(DB) as d:
        d.row_factory = aiosqlite.Row
        cur = await d.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]


async def _one(sql, args=()):
    rows = await _all(sql, args)
    return rows[0] if rows else None


# ---- пользователи ----
async def get_user(uid):
    await _exec("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))
    return await _one("SELECT * FROM users WHERE id=?", (uid,))


async def set_plan(uid, plan, until):
    await get_user(uid)
    await _exec("UPDATE users SET plan=?, plan_until=? WHERE id=?", (plan, until, uid))


async def set_ban(uid, value):
    await get_user(uid)
    await _exec("UPDATE users SET banned=? WHERE id=?", (value, uid))


async def expired_users():
    return await _all(
        "SELECT * FROM users WHERE plan!='free' AND plan_until>0 AND plan_until<?",
        (int(time.time()),),
    )


# ---- проекты ----
async def free_port():
    used = {r["port"] for r in await _all("SELECT port FROM projects WHERE port IS NOT NULL")}
    for p in range(config.PORT_MIN, config.PORT_MAX + 1):
        if p not in used:
            return p
    raise RuntimeError("Свободные порты закончились")


async def add_project(uid, name, ptype, port=None, opts=""):
    return await _exec(
        "INSERT INTO projects(user_id,name,type,port,opts,created) VALUES(?,?,?,?,?,?)",
        (uid, name, ptype, port, opts, int(time.time())),
    )


async def get_project(pid):
    return await _one("SELECT * FROM projects WHERE id=?", (pid,))


async def user_projects(uid):
    return await _all("SELECT * FROM projects WHERE user_id=? ORDER BY id", (uid,))


async def all_projects():
    return await _all("SELECT * FROM projects ORDER BY id")


async def delete_project(pid):
    await _exec("DELETE FROM projects WHERE id=?", (pid,))


async def set_env(pid, env_json):
    await _exec("UPDATE projects SET env=? WHERE id=?", (env_json, pid))


async def stats():
    u = await _one("SELECT COUNT(*) c FROM users")
    p = await _one("SELECT COUNT(*) c FROM projects")
    paid = await _one("SELECT COUNT(*) c FROM users WHERE plan!='free'")
    return u["c"], p["c"], paid["c"]
