import asyncio
import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import moderation
from channels import user_channels
from config import PLANS

router = Router()
esc = html.escape


class NewContest(StatesGroup):
    title = State()
    prize = State()
    rules = State()
    candidates = State()


class BonusInput(StatesGroup):
    amount = State()


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


# ------------------------------------------------------------------ board
async def tally(contest_id):
    rows = await db.fetchall(
        """SELECT c.id, c.name,
           (SELECT COUNT(*) FROM votes v WHERE v.candidate_id=c.id) AS real,
           (SELECT COALESCE(SUM(amount),0) FROM bonus_votes b WHERE b.candidate_id=c.id) AS bonus
           FROM candidates c WHERE c.contest_id=? AND c.approved=1""",
        (contest_id,),
    )
    for r in rows:
        r["bonus"] = max(0, r["bonus"])
        r["total"] = r["real"] + r["bonus"]
    rows.sort(key=lambda r: (-r["total"], -r["real"], r["id"]))
    return rows


def fmt_left(end_at):
    s = max(0, end_at - int(time.time()))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} दिन {h} घंटे"
    return f"{h} घंटे {m} मिनट"


def board_text(contest, rows):
    lines = [f"🏆 <b>{esc(contest['title'])}</b>"]
    if contest["prize"]:
        lines.append(f"🎁 Prize: {esc(contest['prize'])}")
    if contest["status"] == "active":
        lines.append(f"⏳ बचा समय: {fmt_left(contest['end_at'])}")
    else:
        lines.append("🔒 Contest समाप्त")
    lines.append("")
    medals = ["🥇", "🥈", "🥉"]
    any_bonus = False
    for i, r in enumerate(rows):
        mark = medals[i] if i < 3 else f"{i + 1}."
        if r["bonus"]:
            any_bonus = True
            score = f"{r['total']} ({r['real']} real + {r['bonus']} bonus)"
        else:
            score = f"{r['total']}"
        lines.append(f"{mark} {esc(r['name'])} — {score}")
    lines += ["", f"🗳 कुल real votes: {sum(r['real'] for r in rows)}",
              "ℹ️ हर व्यक्ति सिर्फ़ 1 vote दे सकता है।"]
    if any_bonus:
        lines.append("ℹ️ Bonus = host द्वारा दिए गए extra votes।")
    if contest["rules"]:
        lines += ["", f"📜 Rules: {esc(contest['rules'])}"]
    return "\n".join(lines)


def vote_kb(contest_id, rows):
    kb = InlineKeyboardBuilder()
    for r in sorted(rows, key=lambda r: r["id"]):
        kb.button(text=f"🗳 {r['name'][:30]}", callback_data=f"v:{contest_id}:{r['id']}")
    kb.adjust(2)
    return kb.as_markup()


async def push_board(bot: Bot, cid):
    contest = await db.fetchone("SELECT * FROM contests WHERE id=?", (cid,))
    if not contest or not contest["message_id"]:
        return
    rows = await tally(cid)
    kb = vote_kb(cid, rows) if contest["status"] == "active" else None
    try:
        await bot.edit_message_text(
            text=board_text(contest, rows), chat_id=contest["channel_id"],
            message_id=contest["message_id"], reply_markup=kb,
        )
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            logging.warning("push_board %s: %s", cid, e)
    await db.execute("UPDATE contests SET dirty=0,last_edit=? WHERE id=?", (int(time.time()), cid))


async def finish_contest(bot: Bot, cid):
    contest = await db.fetchone("SELECT * FROM contests WHERE id=?", (cid,))
    if not contest or contest["status"] != "active":
        return
    await db.execute("UPDATE contests SET status='ended' WHERE id=?", (cid,))
    await push_board(bot, cid)
    rows = await tally(cid)
    if rows and rows[0]["total"] > 0:
        top = rows[0]
        tied = [r for r in rows if r["total"] == top["total"]]
        if len(tied) > 1:
            names = ", ".join(esc(r["name"]) for r in tied)
            text = f"🏁 <b>{esc(contest['title'])}</b> समाप्त!\n\nबराबरी: {names} ({top['total']} votes)"
        else:
            text = (f"🏁 <b>{esc(contest['title'])}</b> समाप्त!\n\n"
                    f"🎉 विजेता: <b>{esc(top['name'])}</b> — {top['total']} votes")
    else:
        text = f"🏁 <b>{esc(contest['title'])}</b> समाप्त! किसी को vote नहीं मिला।"
    try:
        await bot.send_message(contest["channel_id"], text)
    except Exception as e:
        logging.warning("announce failed: %s", e)
    try:
        await bot.send_message(contest["owner_id"], "✅ आपका contest समाप्त हुआ।\n\n" + text)
    except Exception:
        pass


async def background(bot: Bot):
    """Har 20 sec: expire hue contests band karo, naye votes ka board update karo."""
    while True:
        try:
            now = int(time.time())
            for c in await db.fetchall("SELECT id FROM contests WHERE status='active' AND end_at<=?", (now,)):
                await finish_contest(bot, c["id"])
            for c in await db.fetchall(
                "SELECT id FROM contests WHERE status='active' AND (dirty=1 OR last_edit<?)", (now - 600,)
            ):
                await push_board(bot, c["id"])
                await asyncio.sleep(1)
        except Exception:
            logging.exception("background loop")
        await asyncio.sleep(20)


# ------------------------------------------------------------------ voting
@router.callback_query(F.data.startswith("v:"))
async def vote(cb: CallbackQuery, bot: Bot):
    _, cid, kid = cb.data.split(":")
    cid, kid = int(cid), int(kid)
    contest = await db.fetchone("SELECT * FROM contests WHERE id=?", (cid,))
    if not contest or contest["status"] != "active" or contest["end_at"] <= int(time.time()):
        return await cb.answer("यह contest समाप्त हो चुका है।", show_alert=True)
    if cb.from_user.is_bot:
        return await cb.answer("Bots vote नहीं दे सकते।", show_alert=True)
    user = await _user(cb)
    if user["banned"]:
        return await cb.answer("आप ban हैं।", show_alert=True)
    cand = await db.fetchone("SELECT * FROM candidates WHERE id=? AND contest_id=? AND approved=1", (kid, cid))
    if not cand:
        return await cb.answer("Candidate नहीं मिला।", show_alert=True)
    # channel join check
    try:
        m = await bot.get_chat_member(contest["channel_id"], cb.from_user.id)
        if m.status in ("left", "kicked"):
            return await cb.answer("पहले channel join करो, फिर vote दो।", show_alert=True)
    except Exception:
        pass
    try:
        await db.execute(
            "INSERT INTO votes(contest_id,candidate_id,voter_id,created_at) VALUES(?,?,?,?)",
            (cid, kid, cb.from_user.id, int(time.time())),
        )
    except db.IntegrityError:
        return await cb.answer("आप इस contest में पहले ही vote दे चुके हैं।", show_alert=True)
    await db.execute("UPDATE contests SET dirty=1 WHERE id=?", (cid,))
    await cb.answer(f"✅ आपका vote {cand['name']} को दर्ज हो गया!", show_alert=True)


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
            return await cb.answer("Active giveaway limit पूरी हो गई। Premium लें 💎", show_alert=True)
    chans = await user_channels(user["user_id"])
    if not chans:
        await cb.message.edit_text("पहले कोई channel connect करो।", reply_markup=menu_kb([("🔗 Channels", "channels")]))
        return await cb.answer()
    kb = InlineKeyboardBuilder()
    for c in chans:
        kb.button(text=c["title"][:30], callback_data=f"cp:{c['channel_id']}")
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(1)
    await cb.message.edit_text("🎁 <b>New Giveaway</b>\n\nकिस channel पर contest चलाना है?", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("cp:"))
async def pick_channel(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    user = await _user(cb)
    ch = await db.fetchone("SELECT * FROM channels WHERE channel_id=?", (cid,))
    if not ch or (ch["owner_id"] != user["user_id"] and not is_staff(user)):
        return await cb.answer("Channel नहीं मिला।", show_alert=True)
    await state.update_data(channel_id=cid, channel_title=ch["title"])
    await state.set_state(NewContest.title)
    await cb.message.edit_text("1️⃣ Contest का <b>title</b> भेजो (जैसे: Best Singer 2026)", reply_markup=menu_kb())
    await cb.answer()


@router.message(StateFilter(NewContest.title))
async def get_title(msg: Message, state: FSMContext):
    if not msg.text:
        return await msg.answer("Text में title भेजो।")
    user = await _user(msg)
    ok, why = moderation.check(msg.text, moderation.is_full(user))
    if not ok or len(msg.text) > 80:
        return await msg.answer(f"❌ Title मंज़ूर नहीं: {why or 'बहुत लंबा (max 80)'}")
    await state.update_data(title=msg.text.strip())
    await state.set_state(NewContest.prize)
    await msg.answer("2️⃣ <b>Prize</b> क्या है? (नहीं है तो - भेजो)", reply_markup=menu_kb())


@router.message(StateFilter(NewContest.prize))
async def get_prize(msg: Message, state: FSMContext):
    if not msg.text:
        return await msg.answer("Text भेजो।")
    user = await _user(msg)
    prize = "" if msg.text.strip() == "-" else msg.text.strip()
    if prize:
        ok, why = moderation.check(prize, moderation.is_full(user))
        if not ok or len(prize) > 100:
            return await msg.answer(f"❌ Prize text मंज़ूर नहीं: {why or 'बहुत लंबा'}")
    await state.update_data(prize=prize)
    await state.set_state(NewContest.rules)
    await msg.answer("3️⃣ <b>Rules</b> भेजो (छोटे में). Default के लिए - भेजो।", reply_markup=menu_kb())


@router.message(StateFilter(NewContest.rules))
async def get_rules(msg: Message, state: FSMContext):
    if not msg.text:
        return await msg.answer("Text भेजो।")
    user = await _user(msg)
    rules = "" if msg.text.strip() == "-" else msg.text.strip()
    if rules:
        ok, why = moderation.check(rules, moderation.is_full(user))
        if not ok or len(rules) > 300:
            return await msg.answer(f"❌ Rules मंज़ूर नहीं: {why or 'बहुत लंबे (max 300)'}")
    await state.update_data(rules=rules)
    kb = InlineKeyboardBuilder()
    for label, h in (("1 घंटा", 1), ("6 घंटे", 6), ("24 घंटे", 24), ("3 दिन", 72), ("7 दिन", 168)):
        kb.button(text=label, callback_data=f"dur:{h}")
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(3, 2, 1)
    await msg.answer("4️⃣ Contest कितने समय चलेगा?", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("dur:"))
async def get_duration(cb: CallbackQuery, state: FSMContext):
    if await state.get_state() != NewContest.rules.state:
        return await cb.answer("Session खत्म हो गया, दोबारा शुरू करो।", show_alert=True)
    await state.update_data(hours=int(cb.data.split(":")[1]))
    await state.set_state(NewContest.candidates)
    await cb.message.edit_text(
        "5️⃣ <b>Candidates</b> के नाम भेजो — हर नाम नई line में (कम से कम 2, ज़्यादा से ज़्यादा 20)।",
        reply_markup=menu_kb(),
    )
    await cb.answer()


@router.message(StateFilter(NewContest.candidates))
async def get_candidates(msg: Message, state: FSMContext):
    if not msg.text:
        return await msg.answer("Text भेजो।")
    user = await _user(msg)
    full = moderation.is_full(user)
    names, seen = [], set()
    for line in msg.text.splitlines():
        n = line.strip()
        if not n or n.lower() in seen:
            continue
        if len(n) > 40:
            return await msg.answer(f"❌ '{esc(n[:20])}...' बहुत लंबा नाम है (max 40)।")
        ok, why = moderation.check(n, full)
        if not ok:
            return await msg.answer(f"❌ '{esc(n)}' मंज़ूर नहीं: {why}")
        seen.add(n.lower())
        names.append(n)
    if not 2 <= len(names) <= 20:
        return await msg.answer("❌ 2 से 20 candidates चाहिए। दोबारा भेजो।")
    await state.update_data(names=names)
    d = await state.get_data()
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Publish", callback_data="pub")
    kb.button(text="❌ Cancel", callback_data="menu")
    kb.adjust(2)
    preview = (
        "📝 <b>Preview</b>\n\n"
        f"Channel: {esc(d['channel_title'])}\nTitle: {esc(d['title'])}\n"
        f"Prize: {esc(d['prize'] or '-')}\nRules: {esc(d['rules'] or '-')}\n"
        f"Duration: {d['hours']} घंटे\n\nCandidates:\n" + "\n".join(f"• {esc(n)}" for n in names)
    )
    await msg.answer(preview, reply_markup=kb.as_markup())


@router.callback_query(F.data == "pub")
async def publish(cb: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    if await state.get_state() != NewContest.candidates.state or "names" not in d:
        return await cb.answer("Session खत्म हो गया, दोबारा शुरू करो।", show_alert=True)
    user = await _user(cb)
    now = int(time.time())
    cid = await db.execute(
        "INSERT INTO contests(owner_id,channel_id,title,prize,rules,status,start_at,end_at,created_at) "
        "VALUES(?,?,?,?,?,'active',?,?,?)",
        (user["user_id"], d["channel_id"], d["title"], d["prize"], d["rules"], now, now + d["hours"] * 3600, now),
    )
    for n in d["names"]:
        await db.execute("INSERT INTO candidates(contest_id,name) VALUES(?,?)", (cid, n))
    contest = await db.fetchone("SELECT * FROM contests WHERE id=?", (cid,))
    rows = await tally(cid)
    try:
        sent = await bot.send_message(d["channel_id"], board_text(contest, rows), reply_markup=vote_kb(cid, rows))
    except Exception as e:
        await db.execute("DELETE FROM candidates WHERE contest_id=?", (cid,))
        await db.execute("DELETE FROM contests WHERE id=?", (cid,))
        await state.clear()
        await cb.message.edit_text(
            f"❌ Channel पर post नहीं हो पाया। Bot अभी भी Admin है?\n\n{esc(str(e))}",
            reply_markup=menu_kb(),
        )
        return await cb.answer()
    await db.execute("UPDATE contests SET message_id=?, last_edit=? WHERE id=?", (sent.message_id, now, cid))
    await state.clear()
    await cb.message.edit_text(
        "🎉 <b>Contest live हो गया!</b>\nChannel पर post हो चुका है।",
        reply_markup=menu_kb([("⚙️ Manage Contest", f"mc:{cid}")]),
    )
    await cb.answer()


# ------------------------------------------------------------------ my giveaways / manage
async def _load(cb, cid):
    user = await _user(cb)
    contest = await db.fetchone("SELECT * FROM contests WHERE id=?", (cid,))
    if not contest or (contest["owner_id"] != user["user_id"] and not is_staff(user)):
        await cb.answer("Access नहीं है।", show_alert=True)
        return None, None
    return user, contest


@router.callback_query(F.data == "my_giveaways")
async def my_giveaways(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await db.fetchall(
        "SELECT id,title,status FROM contests WHERE owner_id=? ORDER BY id DESC LIMIT 20", (cb.from_user.id,)
    )
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"{'🟢' if r['status'] == 'active' else '⚫'} {r['title'][:28]}", callback_data=f"mc:{r['id']}")
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(1)
    await cb.message.edit_text(
        "📁 <b>My Giveaways</b>" if rows else "अभी कोई giveaway नहीं है।", reply_markup=kb.as_markup()
    )
    await cb.answer()


@router.callback_query(F.data.startswith("mc:"))
async def manage(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user, contest = await _load(cb, int(cb.data.split(":")[1]))
    if not contest:
        return
    rows = await tally(contest["id"])
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Refresh", callback_data=f"mc:{contest['id']}")
    kb.button(text="📊 Analytics", callback_data=f"an:{contest['id']}")
    kb.button(text="➕ Bonus Votes", callback_data=f"bn:{contest['id']}")
    if contest["status"] == "active":
        kb.button(text="⏹ End Now", callback_data=f"en:{contest['id']}")
    kb.button(text="⬅️ My Giveaways", callback_data="my_giveaways")
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(2, 1, 1, 1, 1)
    try:
        await cb.message.edit_text(board_text(contest, rows), reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await cb.answer()


@router.callback_query(F.data.startswith("en:"))
async def end_ask(cb: CallbackQuery):
    user, contest = await _load(cb, int(cb.data.split(":")[1]))
    if not contest:
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ हाँ, खत्म करो", callback_data=f"enc:{contest['id']}")
    kb.button(text="⬅️ वापस", callback_data=f"mc:{contest['id']}")
    kb.adjust(2)
    await cb.message.edit_text("क्या आप contest अभी खत्म करना चाहते हैं? विजेता घोषित हो जाएगा।", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("enc:"))
async def end_now(cb: CallbackQuery, bot: Bot):
    user, contest = await _load(cb, int(cb.data.split(":")[1]))
    if not contest:
        return
    await finish_contest(bot, contest["id"])
    await cb.message.edit_text("⏹ Contest खत्म हो गया।", reply_markup=menu_kb([("⚙️ Manage", f"mc:{contest['id']}")]))
    await cb.answer()


# ------------------------------------------------------------------ analytics
@router.callback_query(F.data.startswith("an:"))
async def analytics(cb: CallbackQuery):
    user, contest = await _load(cb, int(cb.data.split(":")[1]))
    if not contest:
        return
    cid, now = contest["id"], int(time.time())
    rows = await tally(cid)
    real = sum(r["real"] for r in rows)
    bonus = sum(r["bonus"] for r in rows)
    h1 = await db.count_rows("votes", "WHERE contest_id=? AND created_at>=?", (cid, now - 3600))
    h24 = await db.count_rows("votes", "WHERE contest_id=? AND created_at>=?", (cid, now - 86400))
    lines = [
        f"📊 <b>Analytics: {esc(contest['title'])}</b>\n",
        f"Real votes: {real}", f"Bonus votes: {bonus}", f"Unique voters: {real}",
        f"पिछले 1 घंटे में: {h1}", f"पिछले 24 घंटे में: {h24}",
    ]
    owner = await db.get_user(contest["owner_id"])
    if is_staff(user) or (owner and owner["plan"] == "ultimate"):
        ts = [r["created_at"] for r in await db.fetchall(
            "SELECT created_at FROM votes WHERE contest_id=? AND created_at>=?", (cid, now - 6 * 3600))]
        lines.append("\n⏱ पिछले 6 घंटे (votes/hour):")
        for i in range(6):
            lo, hi = now - (i + 1) * 3600, now - i * 3600
            n = sum(1 for t in ts if lo <= t < hi)
            lines.append(f"• {i + 1} घंटा पहले: {n}")
    else:
        lines.append("\n🔒 Hourly breakdown सिर्फ़ Ultimate plan में।")
    await cb.message.edit_text("\n".join(lines), reply_markup=menu_kb([("⬅️ Manage", f"mc:{cid}")]))
    await cb.answer()


# ------------------------------------------------------------------ bonus votes
@router.callback_query(F.data.startswith("bn:"))
async def bonus_pick(cb: CallbackQuery):
    user, contest = await _load(cb, int(cb.data.split(":")[1]))
    if not contest:
        return
    rows = await tally(contest["id"])
    kb = InlineKeyboardBuilder()
    for r in sorted(rows, key=lambda r: r["id"]):
        kb.button(text=f"{r['name'][:25]} ({r['real']}+{r['bonus']})", callback_data=f"bk:{r['id']}")
    kb.button(text="⬅️ वापस", callback_data=f"mc:{contest['id']}")
    kb.adjust(1)
    quota = "Unlimited" if is_staff(user) else str(user["bonus_quota"])
    await cb.message.edit_text(
        f"➕ <b>Bonus Votes</b>\nआपका bonus quota: <b>{quota}</b>\n\nकिस candidate को देना है?",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("bk:"))
async def bonus_candidate(cb: CallbackQuery, state: FSMContext):
    kid = int(cb.data.split(":")[1])
    cand = await db.fetchone("SELECT * FROM candidates WHERE id=?", (kid,))
    if not cand:
        return await cb.answer("Candidate नहीं मिला।", show_alert=True)
    user, contest = await _load(cb, cand["contest_id"])
    if not contest:
        return
    await state.set_state(BonusInput.amount)
    await state.update_data(candidate_id=kid, contest_id=contest["id"])
    await cb.message.edit_text(
        f"<b>{esc(cand['name'])}</b> को कितने bonus votes?\n\nसंख्या भेजो (जैसे 50). घटाने के लिए minus लगाओ (जैसे -20)।",
        reply_markup=menu_kb([("⬅️ वापस", f"bn:{contest['id']}")]),
    )
    await cb.answer()


@router.message(StateFilter(BonusInput.amount))
async def bonus_amount(msg: Message, state: FSMContext):
    try:
        amount = int((msg.text or "").strip())
    except ValueError:
        return await msg.answer("❌ सिर्फ़ संख्या भेजो (जैसे 50 या -20)।")
    if amount == 0 or abs(amount) > 1_000_000:
        return await msg.answer("❌ संख्या 0 नहीं हो सकती और 10,00,000 से ज़्यादा नहीं।")
    d = await state.get_data()
    user = await _user(msg)
    contest = await db.fetchone("SELECT * FROM contests WHERE id=?", (d["contest_id"],))
    cand = await db.fetchone("SELECT * FROM candidates WHERE id=?", (d["candidate_id"],))
    if not contest or not cand or (contest["owner_id"] != user["user_id"] and not is_staff(user)):
        await state.clear()
        return await msg.answer("Access नहीं है।")
    if not is_staff(user):
        if contest["status"] != "active":
            await state.clear()
            return await msg.answer("Contest समाप्त हो चुका है।")
        if amount > 0:
            if user["bonus_quota"] < amount:
                return await msg.answer(f"❌ आपका quota सिर्फ़ {user['bonus_quota']} है। Premium लें 💎")
            await db.execute("UPDATE users SET bonus_quota=bonus_quota-? WHERE user_id=?", (amount, user["user_id"]))
        else:
            given = (await db.fetchone(
                "SELECT COALESCE(SUM(amount),0) AS s FROM bonus_votes WHERE candidate_id=? AND given_by=?",
                (cand["id"], user["user_id"])))["s"]
            if given < -amount:
                return await msg.answer(f"❌ आप इस candidate से सिर्फ़ {given} bonus हटा सकते हो।")
            await db.execute("UPDATE users SET bonus_quota=bonus_quota+? WHERE user_id=?", (-amount, user["user_id"]))
    await db.execute(
        "INSERT INTO bonus_votes(contest_id,candidate_id,amount,given_by,note,created_at) VALUES(?,?,?,?,?,?)",
        (contest["id"], cand["id"], amount, user["user_id"], "owner" if is_staff(user) else "plan", int(time.time())),
    )
    await db.execute("UPDATE contests SET dirty=1 WHERE id=?", (contest["id"],))
    await state.clear()
    r = next(x for x in await tally(contest["id"]) if x["id"] == cand["id"])
    await msg.answer(
        f"✅ {esc(cand['name'])}: अब {r['total']} ({r['real']} real + {r['bonus']} bonus)\nLeaderboard 20 sec में update होगा।",
        reply_markup=menu_kb([("➕ और Bonus", f"bn:{contest['id']}"), ("⚙️ Manage", f"mc:{contest['id']}")]),
    )
