import os

BOT_NAME = "Vaani Voting Bot"
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
UPI_ID = os.getenv("UPI_ID", "ani21mesh@upi")
DB_PATH = os.getenv("DB_PATH", "vaani.db")


def _link(v: str) -> str:
    v = (v or "").strip()
    if v.startswith("@"):
        return "https://t.me/" + v[1:]
    if v.startswith("t.me/"):
        return "https://" + v
    return v


# optional settings (purane bot jaise)
SUPPORT_GROUP = _link(os.getenv("SUPPORT_GROUP", ""))
SUPPORT_CHANNEL = _link(os.getenv("SUPPORT_CHANNEL", ""))
START_IMAGE = os.getenv("START_IMAGE", "").strip()
VOTE_IMAGE = os.getenv("VOTE_IMAGE", "").strip()
FSUB_IDS = [int(x) for x in os.getenv("FSUB_CHAT", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()]

TZ_OFFSET_MIN = int(os.getenv("TZ_OFFSET_MIN", "330"))  # India = +5:30
TZ_NAME = os.getenv("TZ_NAME", "IST")

PLAN_DAYS = 30

PLANS = {
    "free":     {"name": "Free",     "price": 0,  "giveaways": 1,    "channels": 1,    "bonus": 0},
    "basic":    {"name": "Basic",    "price": 10, "giveaways": 3,    "channels": 2,    "bonus": 100},
    "pro":      {"name": "Pro",      "price": 30, "giveaways": 10,   "channels": 5,    "bonus": 500},
    "ultimate": {"name": "Ultimate", "price": 50, "giveaways": 9999, "channels": 9999, "bonus": 2000},
}

BLOCKED_WORDS = [
    "porn", "xxx", "nude", "casino", "betting", "scam", "fuck", "bitch",
    "madarchod", "bhenchod", "chutiya", "randi", "gaand",
] + [w.strip().lower() for w in os.getenv("BLOCKED_WORDS", "").split(",") if w.strip()]
