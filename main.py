import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import channels
from config import BOT_NAME, BOT_TOKEN, PLANS, UPI_ID

router = Router()


def main_menu(user) -> InlineKeyboardMarkup:
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


BACK = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="🏠 Back to Menu", callback_data="menu")]]
)

WELCOME = (
    f"🗳 *{BOT_NAME}* में स्वागत है!\n\n"
    "यहाँ आप अपने channel पर giveaway और voting contest चला सकते हैं।\n"
    "नीचे से option चुनें 👇"
)


async def _user(obj):
    u = obj.from_user
    return await db.get_or_create_user(u.id, u.username, u.full_name)


@router.message(CommandStart())
async def start(msg: Message):
    user = await _user(msg)
    await msg.answer(WELCOME, reply_markup=main_menu(user), parse_mode="Markdown")


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery):
    user = await _user(cb)
    await cb.message.edit_text(WELCOME, reply_markup=main_menu(user), parse_mode="Markdown")
    await cb.answer()


@router.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    user = await _user(cb)
    plan = PLANS[user["plan"]]
    if user["plan"] == "free":
        expiry = "—"
    else:
        expiry = time.strftime("%d %b %Y", time.localtime(user["plan_expiry"]))
    n_ch = await db.count_rows("channels", "WHERE owner_id=?", (user["user_id"],))
    n_ct = await db.count_rows("contests", "WHERE owner_id=?", (user["user_id"],))
    text = (
        "👤 *My Profile*\n\n"
        f"Name: {user['full_name']}\n"
        f"ID: `{user['user_id']}`\n"
        f"Role: {user['role'].title()}\n"
        f"Plan: {plan['name']}\n"
        f"Valid till: {expiry}\n"
        f"Channels: {n_ch}/{plan['channels'] if plan['channels'] < 9999 else '∞'}\n"
        f"Giveaways: {n_ct}\n"
        f"Bonus quota left: {user['bonus_quota']}"
    )
    await cb.message.edit_text(text, reply_markup=BACK, parse_mode="Markdown")
    await cb.answer()


@router.callback_query(F.data == "premium")
async def premium(cb: CallbackQuery):
    lines = ["💎 *Premium Plans* (30 दिन)\n"]
    for key in ("basic", "pro", "ultimate"):
        p = PLANS[key]
        g = "Unlimited" if p["giveaways"] >= 9999 else p["giveaways"]
        c = "Unlimited" if p["channels"] >= 9999 else p["channels"]
        lines.append(
            f"*{p['name']} – ₹{p['price']}*\n"
            f"• Giveaways: {g}\n• Channels: {c}\n• Bonus votes: {p['bonus']}\n"
        )
    lines.append(f"Payment UPI: `{UPI_ID}`\n_Payment + approval flow अगले step में जुड़ेगा._")
    await cb.message.edit_text("\n".join(lines), reply_markup=BACK, parse_mode="Markdown")
    await cb.answer()


# Placeholders - ye agle steps me banenge
@router.callback_query(F.data.in_({"new_giveaway", "my_giveaways"}))
async def coming_soon(cb: CallbackQuery):
    await cb.answer("यह feature अगले step में आ रहा है 🚧", show_alert=True)


@router.callback_query(F.data == "owner_panel")
async def owner_panel(cb: CallbackQuery):
    user = await _user(cb)
    if user["role"] not in ("owner", "admin"):
        return await cb.answer("Access denied", show_alert=True)
    total = await db.count_rows("users")
    pending = await db.count_rows("payments", "WHERE status='pending'")
    text = (
        "🛠 *Owner Panel*\n\n"
        f"Total users: {total}\n"
        f"Pending payments: {pending}\n\n"
        "_Give Bonus Votes, payment approval और analytics अगले steps में जुड़ेंगे._"
    )
    await cb.message.edit_text(text, reply_markup=BACK, parse_mode="Markdown")
    await cb.answer()


async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN set nahi hai")
    await db.init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(channels.router)
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
