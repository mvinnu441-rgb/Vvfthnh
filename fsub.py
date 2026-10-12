import logging
import time

from aiogram import Bot
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import FSUB_IDS, OWNER_ID

_links = {}   # channel_id -> (title, link)
_seen = {}    # (channel_id, user_id) -> last verified time


async def init(bot: Bot):
    for cid in FSUB_IDS:
        try:
            chat = await bot.get_chat(cid)
            link = (
                f"https://t.me/{chat.username}" if chat.username
                else (chat.invite_link or await bot.export_chat_invite_link(cid))
            )
            _links[cid] = (chat.title or str(cid), link)
        except Exception as e:
            logging.warning("fsub channel %s skipped (is the bot an admin? invite-link permission?): %s", cid, e)


async def missing(bot: Bot, uid: int):
    out = []
    now = time.time()
    for cid, (title, link) in _links.items():
        if _seen.get((cid, uid), 0) > now - 60:
            continue
        try:
            m = await bot.get_chat_member(cid, uid)
        except Exception:
            continue
        gone = m.status in ("left", "kicked") or (m.status == "restricted" and not getattr(m, "is_member", True))
        if gone:
            out.append((title, link))
            continue
        _seen[(cid, uid)] = now
    return out


class FsubMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not _links:
            return await handler(event, data)
        user = event.from_user
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        if user is None or chat is None or chat.type != "private" or user.id == OWNER_ID:
            return await handler(event, data)
        miss = await missing(data["bot"], user.id)
        if not miss:
            return await handler(event, data)
        kb = InlineKeyboardBuilder()
        for title, link in miss:
            if not link:
                continue
            kb.button(text=f"📢 Join {title}", url=link)
        kb.adjust(1)
        text = "🔒 Please join the channel(s) below to use this bot, then send /start."
        if isinstance(event, Message):
            await event.answer(text, reply_markup=kb.as_markup())
            return
        await event.answer("Join the channel(s) first 🔒", show_alert=True)
        await event.message.answer(text, reply_markup=kb.as_markup())
        return
