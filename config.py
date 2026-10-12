import os

BOT_NAME = "Vaani Voting Bot"
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
UPI_ID = os.getenv("UPI_ID", "ani21mesh@upi")
DB_PATH = os.getenv("DB_PATH", "vaani.db")

PLAN_DAYS = 30

PLANS = {
    "free":     {"name": "Free",     "price": 0,  "giveaways": 1,    "channels": 1,    "bonus": 0},
    "basic":    {"name": "Basic",    "price": 10, "giveaways": 3,    "channels": 2,    "bonus": 100},
    "pro":      {"name": "Pro",      "price": 30, "giveaways": 10,   "channels": 5,    "bonus": 500},
    "ultimate": {"name": "Ultimate", "price": 50, "giveaways": 9999, "channels": 9999, "bonus": 2000},
}

# moderation ke liye blocked words (comma se aur add kar sakte ho: env BLOCKED_WORDS)
BLOCKED_WORDS = [
    "porn", "xxx", "nude", "casino", "betting", "scam", "fuck", "bitch",
    "madarchod", "bhenchod", "chutiya", "randi", "gaand",
] + [w.strip().lower() for w in os.getenv("BLOCKED_WORDS", "").split(",") if w.strip()]
