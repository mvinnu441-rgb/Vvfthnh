import sqlite3
import time

import aiosqlite

from config import DB_PATH, OWNER_ID, PLANS, PLAN_DAYS

IntegrityError = sqlite3.IntegrityError

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    role TEXT DEFAULT 'user',
    plan TEXT DEFAULT 'free',
    plan_expiry INTEGER DEFAULT 0,
    bonus_quota INTEGER DEFAULT 0,
    banned INTEGER DEFAULT 0,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS channels(
    channel_id INTEGER PRIMARY KEY,
    owner_id INTEGER,
    title TEXT,
    username TEXT,
    added_at INTEGER
);
CREATE TABLE IF NOT EXISTS contests(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER,
    channel_id INTEGER,
    channel_title TEXT,
    channel_username TEXT,
    title TEXT,
    prize TEXT,
    status TEXT DEFAULT 'active',
    end_at INTEGER DEFAULT 0,          -- 0 = manual end
    service_msg_id INTEGER,
    dirty INTEGER DEFAULT 0,
    last_edit INTEGER DEFAULT 0,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS participants(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER,
    user_id INTEGER,
    username TEXT,
    first_name TEXT,
    msg_id INTEGER,
    card_photo INTEGER DEFAULT 0,
    suspended INTEGER DEFAULT 0,       -- channel chhoda
    removed INTEGER DEFAULT 0,         -- disqualified
    dirty INTEGER DEFAULT 0,
    joined_at INTEGER,
    UNIQUE(contest_id, user_id)
);
CREATE TABLE IF NOT EXISTS votes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER,
    voter_id INTEGER,
    target_id INTEGER,
    created_at INTEGER,
    UNIQUE(contest_id, voter_id)       -- one person, one vote (database level)
);
CREATE TABLE IF NOT EXISTS bonus_votes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER,
    target_id INTEGER,
    amount INTEGER,
    given_by INTEGER,
    note TEXT,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    plan TEXT,
    amount INTEGER,
    utr TEXT,
    screenshot_file_id TEXT,
    status TEXT DEFAULT 'pending',
    reviewed_by INTEGER,
    created_at INTEGER,
    reviewed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_votes_target ON votes(contest_id, target_id);
CREATE INDEX IF NOT EXISTS idx_part_contest ON participants(contest_id);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        # purane (candidates wale) test schema ko hatao
        cur = await conn.execute("PRAGMA table_info(contests)")
        cols = [r[1] for r in await cur.fetchall()]
        if cols and "service_msg_id" not in cols:
            await conn.executescript(
                "DROP TABLE IF EXISTS contests; DROP TABLE IF EXISTS candidates; "
                "DROP TABLE IF EXISTS votes; DROP TABLE IF EXISTS bonus_votes;"
            )
        await conn.executescript(SCHEMA)
        await conn.commit()


async def fetchall(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]


async def fetchone(sql, args=()):
    rows = await fetchall(sql, args)
    return rows[0] if rows else None


async def execute(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(sql, args)
        await conn.commit()
        return cur.lastrowid


async def count_rows(table, where="", args=()):
    row = await fetchone(f"SELECT COUNT(*) AS n FROM {table} {where}", args)
    return row["n"]


async def get_user(user_id):
    return await fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))


async def get_or_create_user(user_id, username, full_name):
    user = await get_user(user_id)
    if user is None:
        role = "owner" if user_id == OWNER_ID else "user"
        await execute(
            "INSERT INTO users(user_id,username,full_name,role,created_at) VALUES(?,?,?,?,?)",
            (user_id, username, full_name, role, int(time.time())),
        )
    else:
        await execute("UPDATE users SET username=?, full_name=? WHERE user_id=?", (username, full_name, user_id))
        if user_id == OWNER_ID and user["role"] != "owner":
            await execute("UPDATE users SET role='owner' WHERE user_id=?", (user_id,))
    user = await get_user(user_id)
    if user["plan"] != "free" and user["plan_expiry"] < int(time.time()):
        await execute("UPDATE users SET plan='free' WHERE user_id=?", (user_id,))
        user["plan"] = "free"
    return user


async def activate_plan(user_id, plan, days=PLAN_DAYS):
    """Activate a plan, extend expiry and add paid-votes quota."""
    user = await get_user(user_id)
    now = int(time.time())
    base = user["plan_expiry"] if (user["plan"] == plan and user["plan_expiry"] > now) else now
    expiry = base + days * 86400
    await execute(
        "UPDATE users SET plan=?, plan_expiry=?, bonus_quota=bonus_quota+? WHERE user_id=?",
        (plan, expiry, PLANS[plan]["bonus"], user_id),
    )
    return expiry


async def staff_ids():
    rows = await fetchall("SELECT user_id FROM users WHERE role IN ('owner','admin')")
    ids = {r["user_id"] for r in rows}
    if OWNER_ID:
        ids.add(OWNER_ID)
    return ids
