import asyncio
import html
import logging
import sqlite3
import time

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "8896971747:AAFnplTJk_0Af0AbMCaGzVSO1bNq2qGBLp4"
ADMIN_IDS = {6833078143, 5023614806}  # ID админов (через запятую)
REQUESTS_CHANNEL_ID = -1004479601062  # канал для заявок (бот должен быть админом канала)

# Две ссылки, замаскированные под названия кнопок
BTN1_TEXT, BTN1_URL = "📢 Наш канал", "https://t.me/blendameuz"
BTN2_TEXT, BTN2_URL = "🎰 Хороший сайт (только с VPN)", "https://long-voyage.com/?i=1826695"

SUPPORT_URL = "https://t.me/jofanamirni"
INSTRUCTION_URL = "https://telegra.ph/InstrukciyaManual-10-19"  # ссылка на вашу статью в Telegraph

# Необязательно: проверка подписки на канал (ID канала или None)
CHECK_CHANNEL_ID = None  # например -1001111111111

# Антиспам
MIN_INTERVAL = 0.8      # сек между действиями
MAX_STRIKES = 5         # нарушений до блокировки
BLOCK_TIME = 60         # сек блокировки

# Реферальная система: (нужно друзей, награда в звёздах)
REF_TIERS = [(10, 100), (100, 1000)]
REF_SUPPORT_FROM = 1000  # от скольки друзей — обращаться в поддержку
# ===============================================

logging.basicConfig(level=logging.INFO)

# ---------- БАЗА ----------
db = sqlite3.connect("gift_bot.db")
db.execute(
    """CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        full_name TEXT,
        status TEXT DEFAULT 'pending',
        account_id TEXT,
        channel_msg_id INTEGER,
        created INTEGER
    )"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        referrer_id INTEGER,
        created INTEGER
    )"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS ref_rewards (
        user_id INTEGER,
        tier INTEGER,
        created INTEGER,
        PRIMARY KEY (user_id, tier)
    )"""
)
db.commit()


def active_request(user_id: int):
    return db.execute(
        "SELECT id FROM requests WHERE user_id=? AND status='pending'", (user_id,)
    ).fetchone()


def register_user(user, referrer_id: int | None = None) -> bool:
    """Регистрирует пользователя. Возвращает True, если он новый."""
    if db.execute("SELECT 1 FROM users WHERE user_id=?", (user.id,)).fetchone():
        return False
    if referrer_id == user.id or not db.execute(
        "SELECT 1 FROM users WHERE user_id=?", (referrer_id,)
    ).fetchone():
        referrer_id = None
    db.execute(
        "INSERT INTO users (user_id, username, full_name, referrer_id, created) VALUES (?,?,?,?,?)",
        (user.id, user.username, user.full_name, referrer_id, int(time.time())),
    )
    db.commit()
    return True


def ref_count(user_id: int) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM users WHERE referrer_id=?", (user_id,)
    ).fetchone()[0]


# ---------- АНТИСПАМ ----------
class AntiSpam(BaseMiddleware):
    def __init__(self):
        self.last: dict[int, float] = {}
        self.strikes: dict[int, int] = {}
        self.blocked: dict[int, float] = {}

    async def __call__(self, handler, event, data):
        user = event.from_user
        if user is None or user.id in ADMIN_IDS:
            return await handler(event, data)

        now = time.monotonic()
        uid = user.id

        if self.blocked.get(uid, 0) > now:
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Вы временно заблокированы за спам", show_alert=True)
            return

        if now - self.last.get(uid, 0) < MIN_INTERVAL:
            self.strikes[uid] = self.strikes.get(uid, 0) + 1
            if self.strikes[uid] >= MAX_STRIKES:
                self.blocked[uid] = now + BLOCK_TIME
                self.strikes[uid] = 0
                if isinstance(event, Message):
                    await event.answer(f"⛔ Слишком много запросов. Блокировка на {BLOCK_TIME} сек.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Не так быстро 🙂")
            return

        self.last[uid] = now
        return await handler(event, data)


# ---------- КЛАВИАТУРЫ ----------
def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN1_TEXT, url=BTN1_URL)],
            [InlineKeyboardButton(text=BTN2_TEXT, url=BTN2_URL)],
            [InlineKeyboardButton(text="📖 Инструкция", url=INSTRUCTION_URL)],
            [InlineKeyboardButton(text="✅ Я выполнил условия", callback_data="submit")],
            [InlineKeyboardButton(text="👥 Реферальная система", callback_data="ref")],
            [InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)],
        ]
    )


def support_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)]]
    )


def done_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done:{req_id}")]]
    )


# ---------- БОТ ----------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
spam = AntiSpam()
dp.message.middleware(spam)
dp.callback_query.middleware(spam)

# админ -> ID заявки, для которой ждём ID аккаунта
pending_admin: dict[int, int] = {}


async def credit_referral(referrer_id: int, new_user):
    """Уведомляет пригласившего и проверяет достижение наград."""
    count = ref_count(referrer_id)
    try:
        await bot.send_message(
            referrer_id,
            f"🎉 По вашей ссылке зарегистрировался новый друг! Всего приглашено: <b>{count}</b>",
        )
    except TelegramAPIError:
        pass

    for need, stars in REF_TIERS:
        if count < need:
            continue
        cur = db.execute(
            "INSERT OR IGNORE INTO ref_rewards (user_id, tier, created) VALUES (?,?,?)",
            (referrer_id, need, int(time.time())),
        )
        db.commit()
        if cur.rowcount == 0:
            continue  # награда за этот уровень уже была

        try:
            await bot.send_message(
                referrer_id,
                f"🏆 Вы пригласили {need} друзей и получаете подарок: <b>{stars} ⭐</b>!\n"
                f"Админ скоро свяжется с вами. Если долго нет ответа — напишите в поддержку.",
                reply_markup=support_kb(),
            )
        except TelegramAPIError:
            pass
        try:
            row = db.execute(
                "SELECT username, full_name FROM users WHERE user_id=?", (referrer_id,)
            ).fetchone()
            uname = f"@{row[0]}" if row and row[0] else "нет username"
            await bot.send_message(
                REQUESTS_CHANNEL_ID,
                f"🏆 <b>Реферальная награда</b>\n\n"
                f"👤 {html.escape(row[1] if row else '—')} ({html.escape(uname)})\n"
                f"🆔 <code>{referrer_id}</code>\n"
                f"👥 Приглашено: {count}\n"
                f"🎁 Выдать: <b>{stars} ⭐</b>",
            )
        except TelegramAPIError:
            logging.exception("Не удалось отправить реф-награду в канал")


@dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(m: Message, command: CommandObject):
    user = m.from_user
    ref_id = None
    if command.args and command.args.startswith("ref_") and command.args[4:].isdigit():
        ref_id = int(command.args[4:])

    is_new = register_user(user, ref_id)
    if is_new and ref_id and ref_id != user.id:
        await credit_referral(ref_id, user)

    await m.answer(
        "Привет! Ты пришел за подарком? 🎁\n"
        "Выполни эти условия:\n\n"
        "1️⃣ Перейди по первой кнопке\n"
        "2️⃣ Перейди по второй кнопке\n"
        "⚠️ <b>Внимание:</b> на второй сайт заходи <b>только с включённым VPN</b>, "
        "иначе он может не открыться!\n\n"
        "После этого нажми «✅ Я выполнил условия».",
        reply_markup=main_kb(),
    )


@dp.callback_query(F.data == "ref")
async def ref_menu(c: CallbackQuery):
    user = c.from_user
    register_user(user)  # на случай, если пользователь пришёл до обновления
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user.id}"
    count = ref_count(user.id)

    next_tier = next(((n, s) for n, s in REF_TIERS if count < n), None)
    if next_tier:
        progress = f"📈 До награды <b>{next_tier[1]} ⭐</b>: осталось {next_tier[0] - count} друзей"
    else:
        progress = (
            f"🏆 Вы выполнили все уровни! Если пригласили {REF_SUPPORT_FROM}+ друзей — "
            f"обратитесь в поддержку."
        )

    tiers_text = "\n".join(f"• {n} друзей — <b>{s} ⭐</b>" for n, s in REF_TIERS)
    await c.answer()
    await c.message.answer(
        "👥 <b>Реферальная система</b>\n\n"
        "Приглашай друзей и получай звёзды:\n"
        f"{tiers_text}\n"
        f"• {REF_SUPPORT_FROM}+ друзей — обращайся в поддержку\n\n"
        f"🔗 Твоя ссылка:\n<code>{link}</code>\n\n"
        f"👤 Приглашено: <b>{count}</b>\n{progress}\n\n"
        "Друг засчитывается, если он впервые запустил бота по твоей ссылке.",
        reply_markup=support_kb(),
    )


@dp.callback_query(F.data == "submit")
async def submit(c: CallbackQuery):
    user = c.from_user

    if active_request(user.id):
        await c.answer("Ваша заявка уже на рассмотрении ⏳", show_alert=True)
        return

    if CHECK_CHANNEL_ID:
        try:
            member = await bot.get_chat_member(CHECK_CHANNEL_ID, user.id)
            if member.status in ("left", "kicked"):
                await c.answer("Сначала подпишитесь на канал!", show_alert=True)
                return
        except TelegramAPIError:
            logging.exception("Не удалось проверить подписку")

    cur = db.execute(
        "INSERT INTO requests (user_id, username, full_name, created) VALUES (?,?,?,?)",
        (user.id, user.username, user.full_name, int(time.time())),
    )
    db.commit()
    req_id = cur.lastrowid

    uname = f"@{user.username}" if user.username else "нет username"
    text = (
        f"📨 <b>Новая заявка #{req_id}</b>\n\n"
        f"👤 {html.escape(user.full_name)}\n"
        f"🔗 {html.escape(uname)}\n"
        f"🆔 <code>{user.id}</code>\n"
        f"📌 Статус: ⏳ на рассмотрении"
    )
    try:
        msg = await bot.send_message(REQUESTS_CHANNEL_ID, text, reply_markup=done_kb(req_id))
        db.execute("UPDATE requests SET channel_msg_id=? WHERE id=?", (msg.message_id, req_id))
        db.commit()
    except TelegramAPIError:
        logging.exception("Не удалось отправить заявку в канал")
        db.execute("DELETE FROM requests WHERE id=?", (req_id,))
        db.commit()
        await c.answer("Ошибка, попробуйте позже или напишите в поддержку", show_alert=True)
        return

    await c.answer()
    await c.message.answer(f"✅ Заявка #{req_id} создана и отправлена на рассмотрение. Ожидайте!")


@dp.callback_query(F.data.startswith("done:"))
async def admin_done(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Только для админов", show_alert=True)
        return

    req_id = int(c.data.split(":")[1])
    row = db.execute("SELECT status FROM requests WHERE id=?", (req_id,)).fetchone()
    if not row or row[0] != "pending":
        await c.answer("Заявка уже обработана", show_alert=True)
        return

    pending_admin[c.from_user.id] = req_id
    try:
        await bot.send_message(
            c.from_user.id,
            f"Заявка #{req_id}: назовите ID аккаунта, на который отправлен подарок.\n"
            f"Отмена: /cancel",
        )
        await c.answer("Бот написал вам в личные сообщения")
    except TelegramForbiddenError:
        pending_admin.pop(c.from_user.id, None)
        await c.answer("Сначала откройте бота и нажмите /start", show_alert=True)


@dp.message(Command("cancel"), F.from_user.id.in_(ADMIN_IDS))
async def admin_cancel(m: Message):
    pending_admin.pop(m.from_user.id, None)
    await m.answer("Отменено.")


@dp.message(Command("pending"), F.from_user.id.in_(ADMIN_IDS))
async def admin_pending(m: Message):
    rows = db.execute(
        "SELECT id, full_name, user_id FROM requests WHERE status='pending'"
    ).fetchall()
    if not rows:
        await m.answer("Нет активных заявок.")
        return
    await m.answer(
        "⏳ Активные заявки:\n"
        + "\n".join(f"#{r[0]} — {html.escape(r[1])} (<code>{r[2]}</code>)" for r in rows)
    )


# ---------- АДМИНКА: статистика рефералов ----------
@dp.message(Command("refstats"), F.from_user.id.in_(ADMIN_IDS))
async def admin_refstats(m: Message, command: CommandObject):
    # /refstats <id или @username> — по одному пользователю
    if command.args:
        arg = command.args.strip().lstrip("@")
        if arg.isdigit():
            row = db.execute(
                "SELECT user_id, username, full_name FROM users WHERE user_id=?", (int(arg),)
            ).fetchone()
        else:
            row = db.execute(
                "SELECT user_id, username, full_name FROM users WHERE username=?", (arg,)
            ).fetchone()
        if not row:
            await m.answer("Пользователь не найден.")
            return
        uid, uname, fname = row
        invited = db.execute(
            "SELECT user_id, username, full_name FROM users WHERE referrer_id=? "
            "ORDER BY created DESC LIMIT 30",
            (uid,),
        ).fetchall()
        lines = [f"• {html.escape(r[2])} (<code>{r[0]}</code>)" for r in invited]
        await m.answer(
            f"👤 {html.escape(fname)} (<code>{uid}</code>)\n"
            f"👥 Приглашено: <b>{ref_count(uid)}</b>\n\n"
            + ("Последние:\n" + "\n".join(lines) if lines else "Рефералов нет.")
        )
        return

    # общий топ
    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_refs = db.execute(
        "SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL"
    ).fetchone()[0]
    top = db.execute(
        """SELECT u.user_id, u.full_name, u.username, COUNT(r.user_id) AS cnt
           FROM users u JOIN users r ON r.referrer_id = u.user_id
           GROUP BY u.user_id ORDER BY cnt DESC LIMIT 20"""
    ).fetchall()
    text = (
        f"📊 <b>Статистика рефералов</b>\n"
        f"Всего пользователей: {total_users}\n"
        f"Пришло по рефералкам: {total_refs}\n\n"
    )
    if top:
        text += "🏆 Топ приглашающих:\n" + "\n".join(
            f"{i}. {html.escape(t[1])} (<code>{t[0]}</code>) — <b>{t[3]}</b>"
            for i, t in enumerate(top, 1)
        )
    else:
        text += "Пока никто никого не пригласил."
    text += "\n\nПо одному пользователю: <code>/refstats ID</code> или <code>/refstats @username</code>"
    await m.answer(text)


@dp.message(
    F.chat.type == ChatType.PRIVATE,
    F.from_user.id.in_(ADMIN_IDS),
    F.text,
    ~F.text.startswith("/"),
)
async def admin_account_id(m: Message):
    req_id = pending_admin.get(m.from_user.id)
    if req_id is None:
        return

    account_id = m.text.strip()
    row = db.execute(
        "SELECT user_id, full_name, channel_msg_id, status FROM requests WHERE id=?", (req_id,)
    ).fetchone()
    pending_admin.pop(m.from_user.id, None)

    if not row or row[3] != "pending":
        await m.answer("Заявка уже обработана.")
        return

    user_id, full_name, channel_msg_id, _ = row
    db.execute(
        "UPDATE requests SET status='done', account_id=? WHERE id=?", (account_id, req_id)
    )
    db.commit()

    try:
        await bot.send_message(
            user_id,
            f"🎁 Подарок отправлен на аккаунт <code>{html.escape(account_id)}</code>!\n"
            f"Спасибо, что с нами!",
        )
        notified = "Пользователю отправлено уведомление ✅"
    except TelegramAPIError:
        notified = "⚠️ Не удалось написать пользователю (возможно, он заблокировал бота)"

    if channel_msg_id:
        try:
            await bot.edit_message_text(
                f"📨 <b>Заявка #{req_id}</b>\n\n"
                f"👤 {html.escape(full_name)}\n"
                f"🆔 <code>{user_id}</code>\n"
                f"🎁 Аккаунт: <code>{html.escape(account_id)}</code>\n"
                f"📌 Статус: ✅ выполнено",
                chat_id=REQUESTS_CHANNEL_ID,
                message_id=channel_msg_id,
            )
        except TelegramAPIError:
            pass

    await m.answer(f"Заявка #{req_id} закрыта. {notified}")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
