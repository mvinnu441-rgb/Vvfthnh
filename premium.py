import html
import time

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import ui
from config import PLANS, PLAN_DAYS, UPI_ID

router = Router()
esc = html.escape


class Pay(StatesGroup):
    utr = State()
    shot = State()


def _lim(n):
    return "Unlimited" if n >= 9999 else str(n)


def _back(extra=None):
    kb = InlineKeyboardBuilder()
    for t, d in (extra or []):
        kb.button(text=t, callback_data=d)
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "premium")
async def premium(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    lines = [f"💎 <b>Premium Plans</b> ({PLAN_DAYS} दिन)\n"]
    for key in ("basic", "pro", "ultimate"):
        p = PLANS[key]
        lines.append(
            f"<b>{p['name']} – ₹{p['price']}</b>\n• Giveaways: {_lim(p['giveaways'])}\n"
            f"• Channels: {_lim(p['channels'])}\n• Paid-votes quota: {p['bonus']}\n"
        )
        kb.button(text=f"{p['name']} – ₹{p['price']}", callback_data=f"pl:{key}")
    lines.append("• Pro/Ultimate: पूरा content moderation\n• Ultimate: hourly analytics")
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(3, 1)
    await ui.edit(cb, "\n".join(lines), reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("pl:"))
async def plan_detail(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    p = PLANS[key]
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ मैंने payment कर दिया", callback_data=f"paid:{key}")
    kb.button(text="⬅️ वापस", callback_data="premium")
    kb.adjust(1)
    await ui.edit(cb, 
        f"💳 <b>{p['name']} Plan – ₹{p['price']}</b>\n\n"
        f"1️⃣ किसी भी UPI app से ₹{p['price']} भेजो:\n<code>{UPI_ID}</code>\n\n"
        "2️⃣ Payment के बाद नीचे बटन दबाओ और UTR/Transaction ID और screenshot भेजो।\n\n"
        "Verify होते ही plan चालू हो जाएगा।",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("paid:"))
async def paid(cb: CallbackQuery, state: FSMContext):
    user = await db.get_or_create_user(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)
    if user["banned"]:
        return await cb.answer("आप ban हैं।", show_alert=True)
    key = cb.data.split(":")[1]
    pending = await db.count_rows("payments", "WHERE user_id=? AND status='pending'", (user["user_id"],))
    if pending >= 2:
        return await cb.answer("आपके 2 payments पहले से verify होने बाकी हैं।", show_alert=True)
    await state.set_state(Pay.utr)
    await state.update_data(plan=key)
    await ui.edit(cb, "अपना <b>UTR / Transaction ID</b> भेजो (UPI app में payment details में मिलेगा)।",
                               reply_markup=_back())
    await cb.answer()


@router.message(StateFilter(Pay.utr))
async def get_utr(msg: Message, state: FSMContext):
    utr = (msg.text or "").strip()
    if not (8 <= len(utr) <= 24 and utr.isalnum()):
        return await msg.answer("❌ सही UTR भेजो (8-24 अक्षर/अंक, बिना space)।")
    if await db.fetchone("SELECT id FROM payments WHERE utr=? AND status!='rejected'", (utr,)):
        return await msg.answer("❌ यह UTR पहले ही use हो चुका है।")
    await state.update_data(utr=utr)
    await state.set_state(Pay.shot)
    await msg.answer("अब payment का <b>screenshot</b> भेजो (photo के रूप में)।", reply_markup=_back())


@router.message(StateFilter(Pay.shot), F.photo)
async def get_shot(msg: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    plan = PLANS[d["plan"]]
    pid = await db.execute(
        "INSERT INTO payments(user_id,plan,amount,utr,screenshot_file_id,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
        (msg.from_user.id, d["plan"], plan["price"], d["utr"], msg.photo[-1].file_id, int(time.time())),
    )
    await state.clear()
    await msg.answer("✅ Payment request भेज दी गई। Verify होने पर आपको message मिलेगा।", reply_markup=_back())
    for sid in await db.staff_ids():
        try:
            await send_review(bot, sid, pid)
        except Exception:
            pass


@router.message(StateFilter(Pay.shot))
async def need_photo(msg: Message):
    await msg.answer("❌ Screenshot photo के रूप में भेजो।")


async def send_review(bot: Bot, chat_id, pid):
    p = await db.fetchone("SELECT * FROM payments WHERE id=?", (pid,))
    u = await db.get_user(p["user_id"])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Approve", callback_data=f"pa:{pid}")
    kb.button(text="❌ Reject", callback_data=f"pr:{pid}")
    kb.adjust(2)
    uname = f"@{u['username']}" if u["username"] else "-"
    cap = (f"💳 <b>Payment #{pid}</b>\nUser: {esc(u['full_name'] or '')} {esc(uname)} (<code>{u['user_id']}</code>)\n"
           f"Plan: {PLANS[p['plan']]['name']} – ₹{p['amount']}\nUTR: <code>{esc(p['utr'])}</code>")
    if p["screenshot_file_id"]:
        await bot.send_photo(chat_id, p["screenshot_file_id"], caption=cap, reply_markup=kb.as_markup())
    else:
        await bot.send_message(chat_id, cap, reply_markup=kb.as_markup())


async def _staff(cb):
    u = await db.get_user(cb.from_user.id)
    return bool(u and u["role"] in ("owner", "admin"))


@router.callback_query(F.data.startswith(("pa:", "pr:")))
async def review(cb: CallbackQuery, bot: Bot):
    if not await _staff(cb):
        return await cb.answer("Access नहीं है।", show_alert=True)
    action, pid = cb.data.split(":")
    pid = int(pid)
    p = await db.fetchone("SELECT * FROM payments WHERE id=?", (pid,))
    if not p or p["status"] != "pending":
        return await cb.answer("यह payment पहले ही process हो चुका है।", show_alert=True)
    now = int(time.time())
    if action == "pa":
        expiry = await db.activate_plan(p["user_id"], p["plan"])
        await db.execute("UPDATE payments SET status='approved',reviewed_by=?,reviewed_at=? WHERE id=?",
                         (cb.from_user.id, now, pid))
        until = time.strftime("%d %b %Y", time.localtime(expiry))
        try:
            await bot.send_message(
                p["user_id"],
                f"🎉 <b>{PLANS[p['plan']]['name']}</b> plan चालू हो गया! Valid till: {until}\n"
                f"Bonus quota +{PLANS[p['plan']]['bonus']} जुड़ गया।")
        except Exception:
            pass
        status = "✅ Approved"
    else:
        await db.execute("UPDATE payments SET status='rejected',reviewed_by=?,reviewed_at=? WHERE id=?",
                         (cb.from_user.id, now, pid))
        try:
            await bot.send_message(p["user_id"], "❌ आपका payment verify नहीं हो पाया। सही UTR/screenshot के साथ दोबारा कोशिश करो।")
        except Exception:
            pass
        status = "❌ Rejected"
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.reply(f"Payment #{pid}: {status}")
    await cb.answer(status)
