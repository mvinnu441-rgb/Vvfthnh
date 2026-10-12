import re

from config import BLOCKED_WORDS

LINK = re.compile(r"(https?://|t\.me/|www\.|@\w{3,}|\.com\b|\.in\b)", re.I)


def check(text: str, full: bool = True):
    """(ok, reason). Rule-based moderation: link/spam/blocked words.
    full=False -> sirf link check (Free/Basic). full=True -> Pro/Ultimate/Admin."""
    t = (text or "").strip()
    if not t:
        return False, "text khali hai"
    if LINK.search(t):
        return False, "link ya @username allowed नहीं है"
    if not full:
        return True, ""
    low = t.lower()
    for w in BLOCKED_WORDS:
        if w in low:
            return False, "नियमों के खिलाफ शब्द मिला"
    if re.search(r"(.)\1{5,}", low):
        return False, "spam जैसा text मिला"
    return True, ""


def is_full(user) -> bool:
    return user["role"] in ("owner", "admin") or user["plan"] in ("pro", "ultimate")
