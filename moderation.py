import re

from config import BLOCKED_WORDS

LINK = re.compile(r"(https?://|t\.me/|www\.|@\w{3,}|\.com\b|\.in\b)", re.I)


def check(text: str, full: bool = True):
    """(ok, reason). Rule-based moderation: link/spam/blocked words.
    full=False -> sirf link check (Free/Basic). full=True -> Pro/Ultimate/Admin."""
    t = (text or "").strip()
    if not t:
        return False, "text is empty"
    if LINK.search(t):
        return False, "links and @usernames are not allowed"
    if not full:
        return True, ""
    low = t.lower()
    for w in BLOCKED_WORDS:
        if w in low:
            return False, "contains a blocked word"
    if re.search(r"(.)\1{5,}", low):
        return False, "looks like spam"
    return True, ""


def is_full(user) -> bool:
    return user["role"] in ("owner", "admin") or user["plan"] in ("pro", "ultimate")
