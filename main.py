import asyncio
import html
import logging
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import channels
import contests
import db
import owner
import premium
from config import BOT_NAME, BOT_TOKEN, PLANS

router = Router()
esc = html.escape

BACK = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Back to Menu", callback_data="menu")]])

WELCOME = (
    f"🗳 <b>{BOT_NAME}</b> में स्वागत है!\n\n"
    "यहाँ आप अपने channel पर giveaway और voting contest चला सकते हैं।\n"
    "नीचे से option चुनें 👇"
)


def main_menu(user):
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 Go Premium", callback_data="premium")
    kb.button(text="👤 My Profile", callback_data="profile")
    kb.button(text="🔗 Channels", callback_data="channels")
    kb.button(text="🎁 New Giveaway", callback_data="new_giveaway")
    kb.button(text="📁 My Giveaways", callback_data="my_giveaways")
    if user["role"] in ("owner", "admin"):
        kb.button(text="🛠 Owner Panel", callback_data="owner_panel")
    kb.adjust(1, 2, 2, 1)
    return kb.as_markup()


async def _user(obj):
    u = obj.from_user
    return await db.get_or_create_user(u.id, u.username, u.full_name)


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(msg: Message, state: FSMContext):
    await state.clear()
    user = await _user(msg)
    if user["banned"]:
        return await msg.answer("🚫 आप इस bot से ban हैं।")
    await msg.answer(WELCOME, reply_markup=main_menu(user))


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _user(cb)
    await cb.message.edit_text(WELCOME, reply_markup=main_menu(user))
    await cb.answer()


@router.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    user = await _user(cb)
    plan = PLANS[user["plan"]]
    expiry = "—" if user["plan"] == "free" else time.strftime("%d %b %Y", time.localtime(user["plan_expiry"]))
    uid = user["user_id"]
    n_ch = await db.count_rows("channels", "WHERE owner_id=?", (uid,))
    n_ct = await db.count_rows("contests", "WHERE owner_id=?", (uid,))
    n_act = await db.count_rows("contests", "WHERE owner_id=? AND status='active'", (uid,))
    lim = lambda n: "∞" if n >= 9999 else n
    text = (
        "👤 <b>My Profile</b>\n\n"
        f"Name: {esc(user['full_name'] or '')}\nID: <code>{uid}</code>\nRole: {user['role'].title()}\n"
        f"Plan: {plan['name']}\nValid till: {expiry}\n"
        f"Channels: {n_ch}/{lim(plan['channels'])}\n"
        f"Active giveaways: {n_act}/{lim(plan['giveaways'])} (कुल {n_ct})\n"
        f"Bonus quota left: {user['bonus_quota']}"
    )
    await cb.message.edit_text(text, reply_markup=BACK)
    await cb.answer()


async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN set nahi hai")
    await db.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(channels.router)
    dp.include_router(contests.router)
    dp.include_router(premium.router)
    dp.include_router(owner.router)
    asyncio.create_task(contests.background(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
