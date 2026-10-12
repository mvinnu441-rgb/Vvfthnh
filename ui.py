from aiogram.exceptions import TelegramBadRequest


async def edit(cb, text, **kw):
    """Callback ke message ko edit karo. Agar wo photo/video message hai to
    delete karke naya text message bhejo (media ka text edit nahi hota)."""
    m = cb.message
    if m is None:
        return
    if getattr(m, "photo", None) or getattr(m, "video", None) or getattr(m, "animation", None):
        try:
            await m.delete()
        except Exception:
            pass
        return await m.answer(text, **kw)
    try:
        return await m.edit_text(text, **kw)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        raise
