import asyncio
import html
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import moderation
import ui
from channels import user_channels
from config import FSUB_IDS, PLANS, TZ_NAME, TZ_OFFSET_MIN, VOTE_IMAGE

router = Router()
esc = html.escape

BOT_USERNAME = ""
_last_tap = {}
_link_cache = {}
MIN_TAP_GAP = 0.5


class NewContest(StatesGroup):
    title = State()
    prize = State()
    duration = State()


class BonusInput(StatesGroup):
    amount = State()


# ------------------------------------------------------------------ helpers
def is_staff(user):
    return user["role"] in ("owner", "admin")


async def _user(obj):
    u = obj.from_user
    return await db.get_or_create_user(u.id, u.username, u.full_name)


def menu_kb(extra=None):
    kb = InlineKeyboardBuilder()
    for text, data in (extra or []):
        kb.button(text=text, callback_data=data)
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def fmt_time(ts):
    tz = timezone(timedelta(minutes=TZ_OFFSET_MIN))
    return datetime.fromtimestamp(ts, tz).strftime("%d %b %Y, %I:%M %p") + f" {TZ_NAME}"


def pname(r):
    return r.get("first_name") or (("@" + r["username"]) if r.get("username") else str(r["user_id"]))


def score(r):
    return f"{r['total']} ({r['real']} real + {r['paid']} paid)" if r["paid"] else f"{r['total']}"


def part_link(contest, msg_id):
    if not msg_id:
        return None
    if contest.get("channel_username"):
        return f"https://t.me/{contest['channel_username']}/{msg_id}"
    return f"https://t.me/c/{str(contest['channel_id']).replace('-100', '', 1)}/{msg_id}"


def deep_link(cid):
    return f"https://t.me/{BOT_USERNAME}?start=vote_{cid}"


async def get_contest(cid):
    return await db.fetchone("SELECT * FROM contests WHERE id=?", (cid,))


async def get_part(cid, uid):
    return await db.fetchone("SELECT * FROM participants WHERE contest_id=? AND user_id=?", (cid, uid))


async def is_member(bot: Bot, chat_id, user_id):
    """True / False / None(verify nahi hua)"""
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return None
    if m.status in ("left", "kicked"):
        return False
    if m.status == "restricted":
        return bool(getattr(m, "is_member", True))
    return True


async def channel_link(bot: Bot, channel_id, username):
    if username:
        return f"https://t.me/{username}"
    if channel_id in _link_cache:
        return _link_cache[channel_id]
    try:
        link = await bot.export_chat_invite_link(channel_id)
    except Exception:
        link = None
    _link_cache[channel_id] = link
    return link


# ------------------------------------------------------------------ leaderboard
async def tally(cid):
    rows = await db.fetchall(
        """SELECT p.user_id, p.username, p.first_name, p.msg_id, p.joined_at,
           (SELECT COUNT(*) FROM votes v WHERE v.contest_id=p.contest_id AND v.target_id=p.user_id) AS real,
           (SELECT COALESCE(SUM(amount),0) FROM bonus_votes b WHERE b.contest_id=p.contest_id AND b.target_id=p.user_id) AS paid
           FROM participants p WHERE p.contest_id=? AND p.suspended=0 AND p.removed=0""",
        (cid,),
    )
    for r in rows:
        r["paid"] = max(0, r["paid"])
        r["total"] = r["real"] + r["paid"]
    rows.sort(key=lambda r: (-r["total"], -r["real"], r["joined_at"] or 0))
    return rows


def service_text(c, rows):
    active = c["status"] == "active"
    head = f"🗳 <b>Vote #{c['id']}" + (f" — {esc(c['title'])}" if c["title"] else "") + "</b>"
    lines = [head]
    if c["prize"]:
        lines.append(f"🎁 Prize: {esc(c['prize'])}")
    if active:
        lines.append("⏳ समाप्ति: " + (fmt_time(c["end_at"]) if c["end_at"] else "host द्वारा (manual)"))
    else:
        lines.append("🏁 <b>Vote समाप्त</b>")
    lines.append(f"👥 Participants: {len(rows)}\n")
    if rows:
        cap = 10 if active else 20
        lines.append("🏆 <b>Live Leaderboard (Top 10)</b>" if active else "🏆 <b>Results</b>")
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(rows[:cap]):
            mark = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{mark} <a href=\"tg://user?id={r['user_id']}\">{esc(pname(r))}</a> — {score(r)}")
        if len(rows) > cap:
            lines.append(f"…और {len(rows) - cap} participants")
    else:
        lines.append("अभी कोई participant नहीं — नीचे बटन से join करो!")
    if active:
        lines.append("\nसिर्फ़ channel subscribers participate और vote कर सकते हैं।")
    if any(r["paid"] for r in rows):
        lines.append("ℹ️ paid = host द्वारा दिए गए extra votes।")
    return "\n".join(lines)


def service_kb(c):
    if c["status"] != "active":
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="🗳 Participate", url=deep_link(c["id"]))
    return kb.as_markup()


async def push_service(bot: Bot, cid):
    c = await get_contest(cid)
    if not c or not c["service_msg_id"]:
        return
    rows = await tally(cid)
    try:
        await bot.edit_message_text(
            text=service_text(c, rows), chat_id=c["channel_id"],
            message_id=c["service_msg_id"], reply_markup=service_kb(c),
        )
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            logging.warning("push_service %s: %s", cid, e)
    await db.execute("UPDATE contests SET dirty=0,last_edit=? WHERE id=?", (int(time.time()), cid))


# ------------------------------------------------------------------ participant card
def card_text(p, real, paid):
    uname = f"@{p['username']}" if p["username"] else "—"
    votes = f"{real} real + {paid} paid = {real + paid}" if paid else f"{real}"
    t = (
        "❤️‍🔥 <b>PARTICIPANT DETAILS</b> ❤️‍🔥\n\n"
        f"‣ User: {esc(p['first_name'] or '')}\n"
        f"‣ User-ID: <code>{p['user_id']}</code>\n"
        f"‣ Username: {esc(uname)}\n"
        f"‣ Votes: <b>{votes}</b>\n\n"
        "Note: Only channel subscribers can vote."
    )
    if p["removed"]:
        t += "\n\n🚫 Disqualified."
    elif p["suspended"]:
        t += "\n\n⚠️ Voting disabled — participant ने channel छोड़ दिया।"
    return t


def card_kb(cid, uid, total):
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🗳 Vote • {total}", callback_data=f"v:{cid}:{uid}")
    return kb.as_markup()


async def counts(cid, uid):
    real = await db.count_rows("votes", "WHERE contest_id=? AND target_id=?", (cid, uid))
    paid = (await db.fetchone(
        "SELECT COALESCE(SUM(amount),0) AS s FROM bonus_votes WHERE contest_id=? AND target_id=?", (cid, uid)))["s"]
    return real, max(0, paid)


async def refresh_card(bot: Bot, cid, uid):
    c, p = await get_contest(cid), await get_part(cid, uid)
    if not c or not p or not p["msg_id"]:
        await db.execute("UPDATE participants SET dirty=0 WHERE contest_id=? AND user_id=?", (cid, uid))
        return
    real, paid = await counts(cid, uid)
    text = card_text(p, real, paid)
    kb = None if (p["suspended"] or p["removed"] or c["status"] != "active") else card_kb(cid, uid, real + paid)
    try:
        if p["card_photo"]:
            await bot.edit_message_caption(chat_id=c["channel_id"], message_id=p["msg_id"], caption=text, reply_markup=kb)
        else:
            await bot.edit_message_text(text=text, chat_id=c["channel_id"], message_id=p["msg_id"], reply_markup=kb)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return  # dirty rehne do, agle round me
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            logging.warning("refresh_card %s/%s: %s", cid, uid, e)
    await db.execute("UPDATE participants SET dirty=0 WHERE contest_id=? AND user_id=?", (cid, uid))


async def mark_dirty(cid, uid=None):
    if uid is not None:
        await db.execute("UPDATE participants SET dirty=1 WHERE contest_id=? AND user_id=?", (cid, uid))
    await db.execute("UPDATE contests SET dirty=1 WHERE id=?", (cid,))


# ------------------------------------------------------------------ finish + background
async def finish_contest(bot: Bot, cid):
    c = await get_contest(cid)
    if not c or c["status"] != "active":
        return
    # jo participants channel chhod chuke hain unhe hatao (missed updates ke liye)
    for p in await db.fetchall("SELECT user_id FROM participants WHERE contest_id=? AND suspended=0 AND removed=0", (cid,)):
        if await is_member(bot, c["channel_id"], p["user_id"]) is False:
            await db.execute("UPDATE participants SET suspended=1 WHERE contest_id=? AND user_id=?", (cid, p["user_id"]))
        await asyncio.sleep(0.05)
    await db.execute("UPDATE contests SET status='ended' WHERE id=?", (cid,))
    await push_service(bot, cid)
    rows = await tally(cid)
    title = f"Vote #{cid}" + (f" — {esc(c['title'])}" if c["title"] else "")
    if rows and rows[0]["total"] > 0:
        top = rows[0]
        tied = [r for r in rows if r["total"] == top["total"]]
        if len(tied) > 1:
            names = ", ".join(esc(pname(r)) for r in tied)
            text = f"🏁 <b>{title}</b> समाप्त!\n\nबराबरी: {names} ({top['total']} votes)"
        else:
            text = (f"🏁 <b>{title}</b> समाप्त!\n\n🎉 विजेता: "
                    f"<a href=\"tg://user?id={top['user_id']}\">{esc(pname(top))}</a> — {score(top)}")
    else:
        text = f"🏁 <b>{title}</b> समाप्त! किसी को vote नहीं मिला।"
    try:
        await bot.send_message(c["channel_id"], text)
    except Exception as e:
        logging.warning("announce failed: %s", e)
    try:
        await bot.send_message(c["owner_id"], "✅ आपका vote समाप्त हुआ।\n\n" + text)
    except Exception:
        pass


async def background(bot: Bot):
    while True:
        try:
            now = int(time.time())
            for c in await db.fetchall(
                "SELECT id FROM contests WHERE status='active' AND end_at>0 AND end_at<=?", (now,)
            ):
                await finish_contest(bot, c["id"])
            cards = await db.fetchall(
                "SELECT p.contest_id, p.user_id FROM participants p JOIN contests c ON c.id=p.contest_id "
                "WHERE p.dirty=1 AND c.status='active' LIMIT 8"
            )
            for r in cards:
                await refresh_card(bot, r["contest_id"], r["user_id"])
                await asyncio.sleep(1.1)
            for c in await db.fetchall(
                "SELECT id FROM contests WHERE status='active' AND dirty=1 AND last_edit<=?", (now - 15,)
            ):
                await push_service(bot, c["id"])
                await asyncio.sleep(1)
        except Exception:
            logging.exception("background loop")
        await asyncio.sleep(3)


# ------------------------------------------------------------------ participation (deep link)
async def participate(msg: Message, bot: Bot, cid: int):
    c = await get_contest(cid)
    if not c or c["status"] != "active" or (c["end_at"] and c["end_at"] <= int(time.time())):
        return await msg.answer("❌ यह vote खत्म हो चुका है या ID गलत है।")
    user = await _user(msg)
    if user["banned"]:
        return await msg.answer("🚫 आप ban हैं।")
    u = msg.from_user
    joined = await is_member(bot, c["channel_id"], u.id)
    if joined is not True:
        link = await channel_link(bot, c["channel_id"], c["channel_username"])
        if joined is None or not link:
            return await msg.answer("🔒 आपकी subscription verify नहीं हो पाई। Vote के creator से संपर्क करो।")
        kb = InlineKeyboardBuilder()
        kb.button(text=f"📢 Join {c['channel_title']}", url=link)
        return await msg.answer(
            f"🔒 सिर्फ़ {esc(c['channel_title'])} के subscribers participate कर सकते हैं। Join करो, फिर यही link दोबारा खोलो।",
            reply_markup=kb.as_markup())

    existing = await get_part(cid, u.id)
    if existing:
        if existing["removed"]:
            return await msg.answer("🚫 आपको इस vote से disqualify किया गया है।")
        link = part_link(c, existing["msg_id"])
        tail = f"\n\n<a href=\"{link}\">अपना card देखो</a>" if link else ""
        if existing["suspended"]:
            await db.execute("UPDATE participants SET suspended=0,dirty=1 WHERE contest_id=? AND user_id=?", (cid, u.id))
            await mark_dirty(cid)
            return await msg.answer("✅ Welcome back! आपकी participation फिर से चालू हो गई।" + tail)
        return await msg.answer("✅ आप पहले ही participate कर चुके हैं।" + tail)

    owner = await db.get_user(c["owner_id"])
    if owner and moderation.is_full(owner):
        ok, _ = moderation.check(u.first_name or "", True)
        if not ok:
            return await msg.answer("❌ आपका profile नाम इस vote के नियमों के अनुसार मंज़ूर नहीं।")

    try:
        await db.execute(
            "INSERT INTO participants(contest_id,user_id,username,first_name,joined_at) VALUES(?,?,?,?,?)",
            (cid, u.id, u.username, u.first_name or u.full_name, int(time.time())),
        )
    except db.IntegrityError:
        return await msg.answer("✅ आप पहले ही participate कर चुके हैं।")
    p = await get_part(cid, u.id)
    try:
        if VOTE_IMAGE:
            sent = await bot.send_photo(c["channel_id"], VOTE_IMAGE, caption=card_text(p, 0, 0), reply_markup=card_kb(cid, u.id, 0))
        else:
            sent = await bot.send_message(c["channel_id"], card_text(p, 0, 0), reply_markup=card_kb(cid, u.id, 0))
    except Exception as e:
        await db.execute("DELETE FROM participants WHERE contest_id=? AND user_id=?", (cid, u.id))
        logging.warning("card post failed: %s", e)
        return await msg.answer("❌ Channel पर आपका card post नहीं हो पाया। Creator से संपर्क करो।")
    await db.execute("UPDATE participants SET msg_id=?, card_photo=? WHERE contest_id=? AND user_id=?",
                     (sent.message_id, 1 if VOTE_IMAGE else 0, cid, u.id))
    await mark_dirty(cid)
    link = part_link(c, sent.message_id)
    tail = f"\n\n<a href=\"{link}\">अपना card देखो</a>" if link else ""
    await msg.answer("✅ आप vote में शामिल हो गए! आपकी details channel में post हो गई हैं।" + tail)


# ------------------------------------------------------------------ voting
@router.callback_query(F.data.startswith("v:"))
async def vote(cb: CallbackQuery, bot: Bot):
    _, cid, target = cb.data.split(":")
    cid, target = int(cid), int(target)
    voter = cb.from_user.id

    now = time.monotonic()
    if now - _last_tap.get(voter, 0) < MIN_TAP_GAP:
        return await cb.answer("⚠️ बहुत तेज़ tap मत करो।")
    if len(_last_tap) > 5000:
        _last_tap.clear()
    _last_tap[voter] = now

    c = await get_contest(cid)
    if not c or c["status"] != "active" or (c["end_at"] and c["end_at"] <= int(time.time())):
        return await cb.answer("❌ यह vote खत्म हो चुका है।", show_alert=True)
    if cb.from_user.is_bot:
        return await cb.answer("Bots vote नहीं दे सकते।", show_alert=True)
    user = await _user(cb)
    if user["banned"]:
        return await cb.answer("🚫 आप ban हैं।", show_alert=True)
    if voter == target:
        return await cb.answer("🚫 आप अपने ही card पर vote नहीं दे सकते।", show_alert=True)
    if await is_member(bot, c["channel_id"], voter) is not True:
        return await cb.answer("🔒 vote देने के लिए पहले channel subscribe करो।", show_alert=True)
    me = await get_part(cid, voter)
    if me and (me["suspended"] or me["removed"]):
        return await cb.answer("🔒 आपकी participation अभी बंद है।", show_alert=True)
    t = await get_part(cid, target)
    if not t or t["removed"]:
        return await cb.answer("❌ Participant नहीं मिला।", show_alert=True)
    if t["suspended"]:
        return await cb.answer("⚠️ इस participant ने channel छोड़ दिया है, vote नहीं ले सकता।", show_alert=True)

    cur = await db.fetchone("SELECT target_id FROM votes WHERE contest_id=? AND voter_id=?", (cid, voter))
    if cur and cur["target_id"] != target:
        prev = await get_part(cid, cur["target_id"])
        name = pname(prev) if prev else "किसी और"
        return await cb.answer(f"⚠️ आप {name} को vote दे चुके हैं। पहले उनके button से vote हटाओ।", show_alert=True)
    if cur:
        await db.execute("DELETE FROM votes WHERE contest_id=? AND voter_id=?", (cid, voter))
        action = "removed"
    else:
        try:
            await db.execute("INSERT INTO votes(contest_id,voter_id,target_id,created_at) VALUES(?,?,?,?)",
                             (cid, voter, target, int(time.time())))
        except db.IntegrityError:
            return await cb.answer("आपका vote पहले ही दर्ज है।", show_alert=True)
        action = "cast"
    await mark_dirty(cid, target)
    real, paid = await counts(cid, target)
    if action == "removed":
        await cb.answer(f"🗳 Vote हटाया। {pname(t)} के अब {real + paid} vote(s) हैं।")
    else:
        await cb.answer(f"✅ {pname(t)} को vote मिला! अब {real + paid} vote(s)।")


# ------------------------------------------------------------------ channel join / leave
async def process_leave(bot: Bot, channel_id, uid):
    for c in await db.fetchall("SELECT id FROM contests WHERE channel_id=? AND status='active'", (channel_id,)):
        cid = c["id"]
        p = await get_part(cid, uid)
        if p and not p["suspended"] and not p["removed"]:
            await db.execute("UPDATE participants SET suspended=1,dirty=1 WHERE contest_id=? AND user_id=?", (cid, uid))
        # voter ho ya participant, dono ke diye hue vote hat jaate hain
        given = await db.fetchall("SELECT target_id FROM votes WHERE contest_id=? AND voter_id=?", (cid, uid))
        if given:
            await db.execute("DELETE FROM votes WHERE contest_id=? AND voter_id=?", (cid, uid))
            for g in given:
                await db.execute("UPDATE participants SET dirty=1 WHERE contest_id=? AND user_id=?", (cid, g["target_id"]))
        await db.execute("UPDATE contests SET dirty=1 WHERE id=?", (cid,))


async def process_join(bot: Bot, channel_id, uid):
    for c in await db.fetchall("SELECT id FROM contests WHERE channel_id=? AND status='active'", (channel_id,)):
        cid = c["id"]
        p = await get_part(cid, uid)
        if p and p["suspended"] and not p["removed"]:
            await db.execute("UPDATE participants SET suspended=0,dirty=1 WHERE contest_id=? AND user_id=?", (cid, uid))
            await db.execute("UPDATE contests SET dirty=1 WHERE id=?", (cid,))


@router.chat_member()
async def on_member(ev: ChatMemberUpdated, bot: Bot):
    m = ev.new_chat_member
    uid = m.user.id
    gone = m.status in ("left", "kicked") or (m.status == "restricted" and not getattr(m, "is_member", True))
    if gone:
        await process_leave(bot, ev.chat.id, uid)
    else:
        await process_join(bot, ev.chat.id, uid)
        if ev.chat.id in FSUB_IDS:
            try:
                await bot.send_message(uid, f"Thanks for joining {esc(ev.chat.title or '')}! ❤️\n\n/start भेजकर आगे बढ़ो।")
            except Exception:
                pass


# ------------------------------------------------------------------ create flow
@router.callback_query(F.data == "new_giveaway")
async def new_giveaway(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _user(cb)
    if user["banned"]:
        return await cb.answer("आप ban हैं।", show_alert=True)
    if not is_staff(user):
        limit = PLANS[user["plan"]]["giveaways"]
        n = await db.count_rows("contests", "WHERE owner_id=? AND status='active'", (user["user_id"],))
        if n >= limit:
            return await cb.answer("Active vote की limit पूरी हो गई। Premium लें 💎", show_alert=True)
    chans = await user_channels(user["user_id"])
    if not chans:
        await ui.edit(cb, "पहले कोई channel connect करो।", reply_markup=menu_kb([("🔗 Channels", "channels")]))
        return await cb.answer()
    kb = InlineKeyboardBuilder()
    for c in chans:
        kb.button(text=c["title"][:30], callback_data=f"cp:{c['channel_id']}")
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(1)
    await ui.edit(cb, "🎁 <b>New Giveaway</b>\n\nकिस channel पर vote चलाना है?", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("cp:"))
async def pick_channel(cb: CallbackQuery, state: FSMContext):
    chid = int(cb.data.split(":")[1])
    user = await _user(cb)
    ch = await db.fetchone("SELECT * FROM channels WHERE channel_id=?", (chid,))
    if not ch or (ch["owner_id"] != user["user_id"] and not is_staff(user)):
        return await cb.answer("Channel नहीं मिला।", show_alert=True)
    await state.update_data(channel_id=chid, channel_title=ch["title"], channel_username=ch["username"])
    await state.set_state(NewContest.title)
    await ui.edit(cb, "1️⃣ Vote का <b>title</b> भेजो (नहीं चाहिए तो - भेजो)", reply_markup=menu_kb())
    await cb.answer()


@router.message(StateFilter(NewContest.title))
async def get_title(msg: Message, state: FSMContext):
    if not msg.text:
        return await msg.answer("Text भेजो।")
    t = "" if msg.text.strip() == "-" else msg.text.strip()
    if t:
        user = await _user(msg)
        ok, why = moderation.check(t, moderation.is_full(user))
        if not ok or len(t) > 80:
            return await msg.answer(f"❌ Title मंज़ूर नहीं: {why or 'बहुत लंबा (max 80)'}")
    await state.update_data(title=t)
    await state.set_state(NewContest.prize)
    await msg.answer("2️⃣ <b>Prize</b> क्या है? (नहीं है तो - भेजो)", reply_markup=menu_kb())


@router.message(StateFilter(NewContest.prize))
async def get_prize(msg: Message, state: FSMContext):
    if not msg.text:
        return await msg.answer("Text भेजो।")
    prize = "" if msg.text.strip() == "-" else msg.text.strip()
    if prize:
        user = await _user(msg)
        ok, why = moderation.check(prize, moderation.is_full(user))
        if not ok or len(prize) > 100:
            return await msg.answer(f"❌ Prize मंज़ूर नहीं: {why or 'बहुत लंबा'}")
    await state.update_data(prize=prize)
    await state.set_state(NewContest.duration)
    kb = InlineKeyboardBuilder()
    for label, h in (("1 घंटा", 1), ("6 घंटे", 6), ("24 घंटे", 24), ("3 दिन", 72), ("7 दिन", 168), ("♾ Manual end", 0)):
        kb.button(text=label, callback_data=f"dur:{h}")
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(3, 3, 1)
    await msg.answer("3️⃣ Vote कितने समय चलेगा? (timer पूरा होते ही अपने आप विजेता घोषित होगा)", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("dur:"))
async def get_duration(cb: CallbackQuery, state: FSMContext):
    if await state.get_state() != NewContest.duration.state:
        return await cb.answer("Session खत्म हो गया, दोबारा शुरू करो।", show_alert=True)
    hours = int(cb.data.split(":")[1])
    await state.update_data(hours=hours)
    d = await state.get_data()
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Publish", callback_data="pub")
    kb.button(text="❌ Cancel", callback_data="menu")
    kb.adjust(2)
    await ui.edit(
        cb,
        "📝 <b>Preview</b>\n\n"
        f"Channel: {esc(d['channel_title'])}\nTitle: {esc(d['title'] or '-')}\nPrize: {esc(d['prize'] or '-')}\n"
        f"Duration: {str(hours) + ' घंटे' if hours else 'Manual end'}\n\n"
        "Publish करने पर channel में <b>Participate</b> बटन वाला post लगेगा।",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data == "pub")
async def publish(cb: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    if await state.get_state() != NewContest.duration.state or "hours" not in d:
        return await cb.answer("Session खत्म हो गया, दोबारा शुरू करो।", show_alert=True)
    user = await _user(cb)
    now = int(time.time())
    end_at = now + d["hours"] * 3600 if d["hours"] else 0
    cid = await db.execute(
        "INSERT INTO contests(owner_id,channel_id,channel_title,channel_username,title,prize,status,end_at,created_at) "
        "VALUES(?,?,?,?,?,?,'active',?,?)",
        (user["user_id"], d["channel_id"], d["channel_title"], d["channel_username"], d["title"], d["prize"], end_at, now),
    )
    c = await get_contest(cid)
    try:
        sent = await bot.send_message(d["channel_id"], service_text(c, []), reply_markup=service_kb(c))
    except Exception as e:
        await db.execute("DELETE FROM contests WHERE id=?", (cid,))
        await state.clear()
        await ui.edit(cb, f"❌ Channel पर post नहीं हो पाया। Bot अभी भी Admin है?\n\n{esc(str(e))}", reply_markup=menu_kb())
        return await cb.answer()
    await db.execute("UPDATE contests SET service_msg_id=?, last_edit=? WHERE id=?", (sent.message_id, now, cid))
    await state.clear()
    await ui.edit(
        cb,
        f"🎉 <b>Vote #{cid} live हो गया!</b>\n\nParticipate link:\n{deep_link(cid)}",
        reply_markup=menu_kb([("⚙️ Manage", f"mc:{cid}")]),
        disable_web_page_preview=True,
    )
    await cb.answer()


# ------------------------------------------------------------------ my giveaways / manage
async def _load(cb, cid):
    user = await _user(cb)
    contest = await get_contest(cid)
    if not contest or (contest["owner_id"] != user["user_id"] and not is_staff(user)):
        await cb.answer("Access नहीं है।", show_alert=True)
        return None, None
    return user, contest


@router.callback_query(F.data == "my_giveaways")
async def my_giveaways(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await db.fetchall(
        "SELECT id,title,status FROM contests WHERE owner_id=? ORDER BY id DESC LIMIT 20", (cb.from_user.id,))
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"{'🟢' if r['status'] == 'active' else '⚫'} #{r['id']} {(r['title'] or '')[:24]}",
                  callback_data=f"mc:{r['id']}")
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(1)
    await ui.edit(cb, "📁 <b>My Giveaways</b>" if rows else "अभी कोई vote नहीं है।", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("mc:"))
async def manage(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user, c = await _load(cb, int(cb.data.split(":")[1]))
    if not c:
        return
    rows = await tally(c["id"])
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Refresh", callback_data=f"mc:{c['id']}")
    kb.button(text="👥 Participants / Paid votes", callback_data=f"ps:{c['id']}:0")
    kb.button(text="📊 Analytics", callback_data=f"an:{c['id']}")
    if c["status"] == "active":
        kb.button(text="⏹ End Now", callback_data=f"en:{c['id']}")
    kb.button(text="⬅️ My Giveaways", callback_data="my_giveaways")
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(1)
    text = service_text(c, rows)
    if c["status"] == "active":
        text += f"\n\n🔗 Participate link:\n{deep_link(c['id'])}"
    await ui.edit(cb, text, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    await cb.answer()


@router.callback_query(F.data.startswith("en:"))
async def end_ask(cb: CallbackQuery):
    user, c = await _load(cb, int(cb.data.split(":")[1]))
    if not c:
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ हाँ, खत्म करो", callback_data=f"enc:{c['id']}")
    kb.button(text="⬅️ वापस", callback_data=f"mc:{c['id']}")
    kb.adjust(2)
    await ui.edit(cb, "क्या vote अभी खत्म करना है? विजेता घोषित हो जाएगा।", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("enc:"))
async def end_now(cb: CallbackQuery, bot: Bot):
    user, c = await _load(cb, int(cb.data.split(":")[1]))
    if not c:
        return
    await cb.answer("खत्म कर रहा हूँ…")
    await finish_contest(bot, c["id"])
    await ui.edit(cb, "⏹ Vote खत्म हो गया।", reply_markup=menu_kb([("⚙️ Manage", f"mc:{c['id']}")]))


# ------------------------------------------------------------------ participants / paid votes
@router.callback_query(F.data.startswith("ps:"))
async def plist(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    _, cid, page = cb.data.split(":")
    user, c = await _load(cb, int(cid))
    if not c:
        return
    rows = await tally(c["id"])
    per = 8
    pages = max(1, (len(rows) + per - 1) // per)
    page = min(max(0, int(page)), pages - 1)
    kb = InlineKeyboardBuilder()
    for i, r in enumerate(rows[page * per:(page + 1) * per], start=page * per + 1):
        kb.button(text=f"{i}. {pname(r)[:18]} ({r['real']}+{r['paid']})", callback_data=f"pd:{c['id']}:{r['user_id']}")
    kb.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"ps:{c['id']}:{page - 1}")
    nav.button(text=f"{page + 1}/{pages}", callback_data=f"ps:{c['id']}:{page}")
    if page < pages - 1:
        nav.button(text="➡️", callback_data=f"ps:{c['id']}:{page + 1}")
    kb.attach(nav)
    back = InlineKeyboardBuilder()
    back.button(text="⬅️ Manage", callback_data=f"mc:{c['id']}")
    kb.attach(back)
    quota = "Unlimited" if is_staff(user) else str(user["bonus_quota"])
    await ui.edit(
        cb,
        f"👥 <b>Participants</b> ({len(rows)})\nPaid-votes quota: <b>{quota}</b>\n\nबटन में (real+paid) दिखता है। Participant चुनो:"
        if rows else "अभी कोई participant नहीं।",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("pd:"))
async def pdetail(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    _, cid, uid = cb.data.split(":")
    user, c = await _load(cb, int(cid))
    if not c:
        return
    p = await get_part(c["id"], int(uid))
    if not p:
        return await cb.answer("Participant नहीं मिला।", show_alert=True)
    real, paid = await counts(c["id"], p["user_id"])
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Paid votes दें / घटाएँ", callback_data=f"pv:{c['id']}:{p['user_id']}")
    kb.button(text="🚫 Disqualify", callback_data=f"px:{c['id']}:{p['user_id']}")
    kb.button(text="⬅️ Participants", callback_data=f"ps:{c['id']}:0")
    kb.adjust(1)
    await ui.edit(
        cb,
        f"👤 <b>{esc(pname(p))}</b>\nID: <code>{p['user_id']}</code>\n"
        f"Real: {real}\nPaid: {paid}\nTotal: <b>{real + paid}</b>",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("pv:"))
async def paid_ask(cb: CallbackQuery, state: FSMContext):
    _, cid, uid = cb.data.split(":")
    user, c = await _load(cb, int(cid))
    if not c:
        return
    p = await get_part(c["id"], int(uid))
    if not p:
        return await cb.answer("Participant नहीं मिला।", show_alert=True)
    await state.set_state(BonusInput.amount)
    await state.update_data(contest_id=c["id"], target_id=p["user_id"])
    await ui.edit(
        cb,
        f"<b>{esc(pname(p))}</b> को कितने paid votes?\n\nसंख्या भेजो (जैसे 50). घटाने के लिए minus लगाओ (जैसे -20)।",
        reply_markup=menu_kb([("⬅️ वापस", f"pd:{c['id']}:{p['user_id']}")]),
    )
    await cb.answer()


@router.message(StateFilter(BonusInput.amount))
async def paid_amount(msg: Message, state: FSMContext):
    try:
        amount = int((msg.text or "").strip())
    except ValueError:
        return await msg.answer("❌ सिर्फ़ संख्या भेजो (जैसे 50 या -20)।")
    if amount == 0 or abs(amount) > 1_000_000:
        return await msg.answer("❌ संख्या 0 नहीं हो सकती और 10,00,000 से ज़्यादा नहीं।")
    d = await state.get_data()
    user = await _user(msg)
    c = await get_contest(d["contest_id"])
    p = await get_part(d["contest_id"], d["target_id"])
    if not c or not p or (c["owner_id"] != user["user_id"] and not is_staff(user)):
        await state.clear()
        return await msg.answer("Access नहीं है।")
    if not is_staff(user):
        if c["status"] != "active":
            await state.clear()
            return await msg.answer("Vote समाप्त हो चुका है।")
        if amount > 0:
            if user["bonus_quota"] < amount:
                return await msg.answer(f"❌ आपका quota सिर्फ़ {user['bonus_quota']} है। Premium लें 💎")
            await db.execute("UPDATE users SET bonus_quota=bonus_quota-? WHERE user_id=?", (amount, user["user_id"]))
        else:
            given = (await db.fetchone(
                "SELECT COALESCE(SUM(amount),0) AS s FROM bonus_votes WHERE contest_id=? AND target_id=? AND given_by=?",
                (c["id"], p["user_id"], user["user_id"])))["s"]
            if given < -amount:
                return await msg.answer(f"❌ आप इस participant से सिर्फ़ {given} paid votes हटा सकते हो।")
            await db.execute("UPDATE users SET bonus_quota=bonus_quota+? WHERE user_id=?", (-amount, user["user_id"]))
    await db.execute(
        "INSERT INTO bonus_votes(contest_id,target_id,amount,given_by,note,created_at) VALUES(?,?,?,?,?,?)",
        (c["id"], p["user_id"], amount, user["user_id"], "owner" if is_staff(user) else "plan", int(time.time())),
    )
    await mark_dirty(c["id"], p["user_id"])
    await state.clear()
    real, paid = await counts(c["id"], p["user_id"])
    await msg.answer(
        f"✅ {esc(pname(p))}: अब {real + paid} ({real} real + {paid} paid)\nCard और leaderboard कुछ सेकंड में update होंगे।",
        reply_markup=menu_kb([("➕ और Paid votes", f"pd:{c['id']}:{p['user_id']}"), ("⚙️ Manage", f"mc:{c['id']}")]),
    )


@router.callback_query(F.data.startswith("px:"))
async def dq_ask(cb: CallbackQuery):
    _, cid, uid = cb.data.split(":")
    user, c = await _load(cb, int(cid))
    if not c:
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ हाँ, disqualify करो", callback_data=f"pxc:{cid}:{uid}")
    kb.button(text="⬅️ वापस", callback_data=f"pd:{cid}:{uid}")
    kb.adjust(1)
    await ui.edit(cb, "इस participant को हटाना है? उसका card हट जाएगा और उसे मिले votes रद्द होंगे।", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("pxc:"))
async def dq_do(cb: CallbackQuery, bot: Bot):
    _, cid, uid = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    user, c = await _load(cb, cid)
    if not c:
        return
    p = await get_part(cid, uid)
    if p:
        await db.execute("UPDATE participants SET removed=1 WHERE contest_id=? AND user_id=?", (cid, uid))
        await db.execute("DELETE FROM votes WHERE contest_id=? AND target_id=?", (cid, uid))
        if p["msg_id"]:
            try:
                await bot.delete_message(c["channel_id"], p["msg_id"])
            except Exception:
                pass
        await mark_dirty(cid)
    await ui.edit(cb, "🚫 Participant हटा दिया गया।", reply_markup=menu_kb([("⬅️ Participants", f"ps:{cid}:0")]))
    await cb.answer()


# ------------------------------------------------------------------ analytics
@router.callback_query(F.data.startswith("an:"))
async def analytics(cb: CallbackQuery):
    user, c = await _load(cb, int(cb.data.split(":")[1]))
    if not c:
        return
    cid, now = c["id"], int(time.time())
    rows = await tally(cid)
    paid = sum(r["paid"] for r in rows)
    total_votes = await db.count_rows("votes", "WHERE contest_id=?", (cid,))
    h1 = await db.count_rows("votes", "WHERE contest_id=? AND created_at>=?", (cid, now - 3600))
    h24 = await db.count_rows("votes", "WHERE contest_id=? AND created_at>=?", (cid, now - 86400))
    lines = [
        f"📊 <b>Analytics: Vote #{cid}</b>\n",
        f"Active participants: {len(rows)}", f"Real votes (unique voters): {total_votes}", f"Paid votes: {paid}",
        f"पिछले 1 घंटे में: {h1}", f"पिछले 24 घंटे में: {h24}",
    ]
    owner = await db.get_user(c["owner_id"])
    if is_staff(user) or (owner and owner["plan"] == "ultimate"):
        ts = [r["created_at"] for r in await db.fetchall(
            "SELECT created_at FROM votes WHERE contest_id=? AND created_at>=?", (cid, now - 6 * 3600))]
        lines.append("\n⏱ पिछले 6 घंटे (votes/hour):")
        for i in range(6):
            lo, hi = now - (i + 1) * 3600, now - i * 3600
            lines.append(f"• {i + 1} घंटा पहले: {sum(1 for t in ts if lo <= t < hi)}")
    else:
        lines.append("\n🔒 Hourly breakdown सिर्फ़ Ultimate plan में।")
    await ui.edit(cb, "\n".join(lines), reply_markup=menu_kb([("⬅️ Manage", f"mc:{cid}")]))
    await cb.answer()
