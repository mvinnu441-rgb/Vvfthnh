import time

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
from config import DB_PATH, PLANS

router = Router()


class AddChannel(StatesGroup):
    waiting = State()


def _limit_text(n):
    return "∞" if n >= 9999 else str(n)


async def user_channels(user_id):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM channels WHERE owner_id=?", (user_id,))
        return [dict(r) for r in await cur.fetchall()]


async def channels_menu(cb: CallbackQuery):
    user = await db.get_or_create_user(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)
    chans = await user_channels(user["user_id"])
    limit = PLANS[user["plan"]]["channels"]
    kb = InlineKeyboardBuilder()
    for c in chans:
        kb.button(text=f"❌ Remove: {c['title'][:25]}", callback_data=f"ch_rm:{c['channel_id']}")
    kb.button(text="➕ Add Channel", callback_data="ch_add")
    kb.button(text="🏠 Back to Menu", callback_data="menu")
    kb.adjust(1)
    text = f"🔗 *Your Channels* ({len(chans)}/{_limit_text(limit)})\n\n"
    text += "\n".join(f"• {c['title']}" for c in chans) if chans else "अभी कोई channel connect नहीं है।"
    await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data == "channels")
async def open_channels(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await channels_menu(cb)
    await cb.answer()


@router.callback_query(F.data == "ch_add")
async def ch_add(cb: CallbackQuery, state: FSMContext):
    user = await db.get_or_create_user(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)
    limit = PLANS[user["plan"]]["channels"]
    if len(await user_channels(user["user_id"])) >= limit:
        return await cb.answer("Channel limit पूरी हो गई। Premium लें 💎", show_alert=True)
    await state.set_state(AddChannel.waiting)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Cancel", callback_data="channels")
    await cb.message.edit_text(
        "➕ *Channel जोड़ने के steps*\n\n"
        "1️⃣ इस bot को अपने channel का *Admin* बनाओ (Post Messages permission के साथ)\n"
        "2️⃣ अब channel का कोई message यहाँ *forward* करो\n"
        "    या channel का username भेजो (जैसे `@mychannel`)",
        reply_markup=kb.as_markup(), parse_mode="Markdown",
    )
    await cb.answer()


@router.message(StateFilter(AddChannel.waiting))
async def ch_receive(msg: Message, state: FSMContext, bot: Bot):
    chat = None
    origin = getattr(msg, "forward_origin", None)
    if origin is not None and getattr(origin, "type", "") == "channel":
        chat = origin.chat
    elif msg.text and msg.text.strip().startswith("@"):
        try:
            chat = await bot.get_chat(msg.text.strip())
        except Exception:
            return await msg.answer("❌ Channel नहीं मिला। Username जाँचकर दोबारा भेजो।")
    if chat is None or chat.type != "channel":
        return await msg.answer("❌ Channel का message forward करो या @username भेजो।")

    # verify 1: bot admin hai?
    try:
        me = await bot.get_me()
        bm = await bot.get_chat_member(chat.id, me.id)
    except Exception:
        return await msg.answer("❌ Bot channel में नहीं है। पहले bot को Admin बनाओ।")
    if bm.status != "administrator" or not getattr(bm, "can_post_messages", False):
        return await msg.answer("❌ Bot को Admin बनाओ और *Post Messages* permission दो।", parse_mode="Markdown")

    # verify 2: user khud channel ka admin/owner hai?
    try:
        um = await bot.get_chat_member(chat.id, msg.from_user.id)
    except Exception:
        return await msg.answer("❌ तुम्हारा admin status verify नहीं हो पाया।")
    if um.status not in ("administrator", "creator"):
        return await msg.answer("❌ तुम इस channel के Admin नहीं हो।")

    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT owner_id FROM channels WHERE channel_id=?", (chat.id,))
        row = await cur.fetchone()
        if row and row[0] != msg.from_user.id:
            return await msg.answer("❌ यह channel पहले से किसी और account से जुड़ा है।")
        await conn.execute(
            "INSERT OR REPLACE INTO channels(channel_id,owner_id,title,username,added_at) VALUES(?,?,?,?,?)",
            (chat.id, msg.from_user.id, chat.title, chat.username, int(time.time())),
        )
        await conn.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 My Channels", callback_data="channels")
    await msg.answer(f"✅ *{chat.title}* connect हो गया!", reply_markup=kb.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("ch_rm:"))
async def ch_remove(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM channels WHERE channel_id=? AND owner_id=?", (cid, cb.from_user.id))
        await conn.commit()
    await cb.answer("Channel हटा दिया")
    await channels_menu(cb)
