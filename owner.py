import asyncio
import html
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import ui
import premium
from config import OWNER_ID, PLANS

router = Router()
esc = html.escape


class Broadcast(StatesGroup):
    text = State()


async def staff(obj):
    u = await db.get_user(obj.from_user.id)
    return u if u and u["role"] in ("owner", "admin") else None


def kb_back(extra=None):
    kb = InlineKeyboardBuilder()
    for t, d in (extra or []):
        kb.button(text=t, callback_data=d)
    kb.button(text="⬅️ Owner Panel", callback_data="owner_panel")
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "owner_panel")
async def panel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await staff(cb):
        return await cb.answer("Access नहीं है।", show_alert=True)
    kb = InlineKeyboardBuilder()
    for t, d in (("📊 Stats", "os"), ("💳 Pending Payments", "pp"), ("🎁 All Contests", "all_contests"),
                 ("📜 Paid Votes Log", "blog"), ("📢 Broadcast", "bc"), ("ℹ️ Commands", "ohelp")):
        kb.button(text=t, callback_data=d)
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(2, 2, 2, 1)
    await ui.edit(cb, "🛠 <b>Owner Panel</b>", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "os")
async def stats(cb: CallbackQuery):
    if not await staff(cb):
        return await cb.answer("Access नहीं है।", show_alert=True)
    g = db.count_rows
    rev = (await db.fetchone("SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE status='approved'"))["s"]
    bonus = (await db.fetchone("SELECT COALESCE(SUM(amount),0) AS s FROM bonus_votes"))["s"]
    text = (
        "📊 <b>Stats</b>\n\n"
        f"Users: {await g('users')}\n"
        f"Premium users: {await g('users', 'WHERE plan!=\'free\'')}\n"
        f"Banned: {await g('users', 'WHERE banned=1')}\n"
        f"Channels: {await g('channels')}\n"
        f"Participants: {await g('participants')}\n"
        f"Contests: {await g('contests')} (active {await g('contests', 'WHERE status=\'active\'')})\n"
        f"Real votes: {await g('votes')}\n"
        f"Paid votes (net): {bonus}\n"
        f"Pending payments: {await g('payments', 'WHERE status=\'pending\'')}\n"
        f"Revenue (approved): ₹{rev}"
    )
    await ui.edit(cb, text, reply_markup=kb_back())
    await cb.answer()


@router.callback_query(F.data == "pp")
async def pending(cb: CallbackQuery, bot: Bot):
    if not await staff(cb):
        return await cb.answer("Access नहीं है।", show_alert=True)
    rows = await db.fetchall("SELECT id FROM payments WHERE status='pending' ORDER BY id LIMIT 15")
    if not rows:
        await ui.edit(cb, "कोई pending payment नहीं है।", reply_markup=kb_back())
        return await cb.answer()
    await cb.answer(f"{len(rows)} pending payments भेज रहा हूँ")
    for r in rows:
        await premium.send_review(bot, cb.from_user.id, r["id"])


@router.callback_query(F.data == "all_contests")
async def all_contests(cb: CallbackQuery):
    if not await staff(cb):
        return await cb.answer("Access नहीं है।", show_alert=True)
    rows = await db.fetchall("SELECT id,title,status FROM contests ORDER BY id DESC LIMIT 20")
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"{'🟢' if r['status'] == 'active' else '⚫'} #{r['id']} {r['title'][:24]}", callback_data=f"mc:{r['id']}")
    kb.button(text="⬅️ Owner Panel", callback_data="owner_panel")
    kb.adjust(1)
    await ui.edit(cb, "🎁 <b>All Contests</b>" if rows else "कोई contest नहीं है।", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "blog")
async def bonus_log(cb: CallbackQuery):
    if not await staff(cb):
        return await cb.answer("Access नहीं है।", show_alert=True)
    rows = await db.fetchall(
        """SELECT b.*, p.first_name AS cname, t.title AS ctitle FROM bonus_votes b
           LEFT JOIN participants p ON p.contest_id=b.contest_id AND p.user_id=b.target_id
           LEFT JOIN contests t ON t.id=b.contest_id ORDER BY b.id DESC LIMIT 20""")
    lines = ["📜 <b>Paid Votes Log</b> (last 20)\n"]
    for r in rows:
        when = time.strftime("%d %b %H:%M", time.localtime(r["created_at"]))
        lines.append(f"{when} | {esc(str(r['ctitle'] or '#' + str(r['contest_id']))[:15])} → {esc(str(r['cname']))}: {r['amount']:+d} (by {r['given_by']})")
    if not rows:
        lines.append("अभी कोई entry नहीं।")
    await ui.edit(cb, "\n".join(lines), reply_markup=kb_back())
    await cb.answer()


@router.callback_query(F.data == "ohelp")
async def ohelp(cb: CallbackQuery):
    if not await staff(cb):
        return await cb.answer("Access नहीं है।", show_alert=True)
    await ui.edit(cb, 
        "ℹ️ <b>Commands</b>\n\n"
        "/ban USER_ID\n/unban USER_ID\n"
        "/addadmin USER_ID (सिर्फ़ owner)\n/removeadmin USER_ID (सिर्फ़ owner)\n"
        "/grant USER_ID PLAN DAYS — जैसे: /grant 12345 pro 30 (सिर्फ़ owner)\n\n"
        "Paid votes: All Contests → contest चुनो → 👥 Participants → participant चुनो",
        reply_markup=kb_back())
    await cb.answer()


# ---- broadcast
@router.callback_query(F.data == "bc")
async def bc_start(cb: CallbackQuery, state: FSMContext):
    if not await staff(cb):
        return await cb.answer("Access नहीं है।", show_alert=True)
    await state.set_state(Broadcast.text)
    await ui.edit(cb, "सभी users को भेजने वाला message लिखो:", reply_markup=kb_back())
    await cb.answer()


@router.message(StateFilter(Broadcast.text))
async def bc_send(msg: Message, state: FSMContext, bot: Bot):
    if not await staff(msg):
        return
    await state.clear()
    users = await db.fetchall("SELECT user_id FROM users WHERE banned=0")
    ok = 0
    for u in users:
        try:
            await msg.copy_to(u["user_id"])
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await msg.answer(f"📢 {ok}/{len(users)} users को भेजा गया।", reply_markup=kb_back())


# ---- commands
def _arg_id(msg: Message):
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return None
    return int(parts[1])


@router.message(Command("ban"))
async def ban(msg: Message):
    if not await staff(msg):
        return
    uid = _arg_id(msg)
    if uid is None or uid == OWNER_ID:
        return await msg.answer("Usage: /ban USER_ID")
    await db.execute("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    await msg.answer(f"🚫 {uid} ban हो गया।")


@router.message(Command("unban"))
async def unban(msg: Message):
    if not await staff(msg):
        return
    uid = _arg_id(msg)
    if uid is None:
        return await msg.answer("Usage: /unban USER_ID")
    await db.execute("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    await msg.answer(f"✅ {uid} unban हो गया।")


@router.message(Command("addadmin"))
async def addadmin(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    uid = _arg_id(msg)
    if uid is None or not await db.get_user(uid):
        return await msg.answer("Usage: /addadmin USER_ID (user ने bot /start किया होना चाहिए)")
    await db.execute("UPDATE users SET role='admin' WHERE user_id=?", (uid,))
    await msg.answer(f"✅ {uid} अब admin है।")


@router.message(Command("removeadmin"))
async def removeadmin(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    uid = _arg_id(msg)
    if uid is None or uid == OWNER_ID:
        return await msg.answer("Usage: /removeadmin USER_ID")
    await db.execute("UPDATE users SET role='user' WHERE user_id=? AND role='admin'", (uid,))
    await msg.answer(f"✅ {uid} अब admin नहीं है।")


@router.message(Command("grant"))
async def grant(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    p = (msg.text or "").split()
    if len(p) != 4 or not p[1].isdigit() or p[2] not in PLANS or p[2] == "free" or not p[3].isdigit():
        return await msg.answer("Usage: /grant USER_ID basic|pro|ultimate DAYS")
    uid = int(p[1])
    if not await db.get_user(uid):
        return await msg.answer("User नहीं मिला (उसने /start नहीं किया)।")
    await db.activate_plan(uid, p[2], int(p[3]))
    await msg.answer(f"✅ {uid} को {p[2]} plan {p[3]} दिन के लिए मिल गया।")
