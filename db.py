import time
import aiosqlite
from config import DB_PATH, OWNER_ID

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    role TEXT DEFAULT 'user',          -- owner / admin / user
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
    title TEXT,
    rules TEXT,
    status TEXT DEFAULT 'draft',       -- draft / active / ended
    start_at INTEGER,
    end_at INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS candidates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER,
    name TEXT,
    user_id INTEGER,
    approved INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS votes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER,
    candidate_id INTEGER,
    voter_id INTEGER,
    created_at INTEGER,
    UNIQUE(contest_id, voter_id)       -- one person, one vote
);
-- bonus votes alag table me, leaderboard me "real + bonus" dikhane ke liye
CREATE TABLE IF NOT EXISTS bonus_votes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER,
    candidate_id INTEGER,
    amount INTEGER,                    -- negative = remove
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
    status TEXT DEFAULT 'pending',     -- pending / approved / rejected
    reviewed_by INTEGER,
    created_at INTEGER,
    reviewed_at INTEGER
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_or_create_user(user_id, username, full_name):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row is None:
            role = "owner" if user_id == OWNER_ID else "user"
            await db.execute(
                "INSERT INTO users(user_id,username,full_name,role,created_at) VALUES(?,?,?,?,?)",
                (user_id, username, full_name, role, int(time.time())),
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
            row = await cur.fetchone()
        else:
            await db.execute(
                "UPDATE users SET username=?, full_name=? WHERE user_id=?",
                (username, full_name, user_id),
            )
            await db.commit()
        user = dict(row)

    # premium expire hua to free par wapas
    if user["plan"] != "free" and user["plan_expiry"] < int(time.time()):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET plan='free' WHERE user_id=?", (user_id,))
            await db.commit()
        user["plan"] = "free"
    return user


async def count_rows(table, where="", args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM {table} {where}", args)
        return (await cur.fetchone())[0]
