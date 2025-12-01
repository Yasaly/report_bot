import logging
import os
from dataclasses import dataclass
from typing import Optional, List

from dotenv import load_dotenv
from telegram.error import TelegramError
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from db import get_conn

load_dotenv()


class BotUserError(Exception):
    """
    Ошибка, которую можно безопасно показать пользователю.
    Например: "такой nickname уже существует" или "ты не админ".
    """
    pass


# ---------- базовые настройки ----------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

# состояния для ConversationHandler
SUBSCRIBE_NICKNAME = 1
SETROLE_CHOOSE_ROLE = 2
SETROLE_WAIT_NICKNAME = 3
UNSUB_USER_WAIT_NICKNAME = 4


@dataclass
class Recipient:
    nickname: str
    chat_id: int
    username: Optional[str]
    role: str


# ---------- работа с БД ----------

def init_db() -> None:
    """
    Создаём таблицу в PostgreSQL, если её ещё нет.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recipients (
                    nickname   TEXT PRIMARY KEY,
                    chat_id    BIGINT NOT NULL UNIQUE,
                    username   TEXT,
                    role       TEXT NOT NULL DEFAULT 'user'
                )
                """
            )
        conn.commit()
    logger.info("PostgreSQL DB initialized")


def save_recipient(nickname: str, chat_id: int, username: Optional[str]) -> None:
    """
    Регистрируем nickname за данным chat_id.

    Правила:
    - один chat_id может иметь только один nickname;
    - один nickname может принадлежать только одному chat_id;
    - если чат повторно подписывается с тем же nickname — просто обновляем username.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Смотрим, есть ли уже запись для этого чата
            cur.execute(
                "SELECT nickname, chat_id FROM recipients WHERE chat_id = %s",
                (chat_id,),
            )
            row_chat = cur.fetchone()

            # Смотрим, есть ли уже запись для этого nickname
            cur.execute(
                "SELECT nickname, chat_id FROM recipients WHERE nickname = %s",
                (nickname,),
            )
            row_nick = cur.fetchone()

            # Если и чат, и ник совпадают — обновляем только username
            if row_chat and row_nick and row_chat[0] == row_nick[0] == nickname:
                cur.execute(
                    "UPDATE recipients SET username = %s WHERE nickname = %s",
                    (username, nickname),
                )
                conn.commit()
                return

            # У этого чата уже есть другой nickname
            if row_chat and row_chat[0] != nickname:
                current_nick = row_chat[0]
                raise BotUserError(
                    f"У тебя уже есть nickname '{current_nick}'.\n"
                    f"Один чат может иметь только один nickname.\n"
                    f"Если хочешь его сменить — сначала сделай /unsubscribe."
                )

            # Этот nickname уже занят другим chat_id
            if row_nick and row_nick[1] != chat_id:
                raise BotUserError(
                    f"Никнейм '{nickname}' уже используется другим пользователем.\n"
                    f"Выбери другой nickname или попроси админа освободить его."
                )

            # Никнейм свободен, и у чата ещё нет ника — создаём запись
            cur.execute(
                """
                INSERT INTO recipients (nickname, chat_id, username, role)
                VALUES (%s, %s, %s, 'user')
                """,
                (nickname, chat_id, username),
            )
            conn.commit()



def _row_to_recipient(row) -> Recipient:
    nickname, chat_id, username, role = row
    return Recipient(
        nickname=nickname,
        chat_id=chat_id,
        username=username,
        role=role,
    )



def get_recipients_by_chat(chat_id: int) -> List[Recipient]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT nickname, chat_id, username, role
                FROM recipients
                WHERE chat_id = %s
                """,
                (chat_id,),
            )
            rows = cur.fetchall()
    return [_row_to_recipient(r) for r in rows]


def delete_by_nickname(nickname: str) -> int:
    """Удаляем всех получателей с данным nickname. Возвращаем число удалённых строк."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM recipients WHERE nickname = %s",
                (nickname,),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted


def unsubscribe_chat(chat_id: int) -> int:
    """Полностью удаляем всех получателей этого чата из БД."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM recipients WHERE chat_id = %s",
                (chat_id,),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted


def set_role(nickname: str, role: str) -> int:
    """Устанавливаем роль для пользователя. Возвращаем число обновлённых строк."""
    if role not in ("user", "admin"):
        raise ValueError("role must be 'user' or 'admin'")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE recipients SET role = %s WHERE nickname = %s",
                (role, nickname),
            )
            updated = cur.rowcount
        conn.commit()
    return updated


def is_admin_chat(chat_id: int) -> bool:
    """Считаем чат админским, если есть хотя бы один nickname с role='admin'."""
    recipients = get_recipients_by_chat(chat_id)
    return any(r.role == "admin" for r in recipients)


def get_recipient_by_nickname(nickname: str) -> Optional[Recipient]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT nickname, chat_id, username, role
                FROM recipients
                WHERE nickname = %s
                """,
                (nickname,),
            )
            row = cur.fetchone()

    if row is None:
        return None

    return _row_to_recipient(row)



def get_all_recipients() -> List[Recipient]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nickname, chat_id, username, role FROM recipients"
            )
            rows = cur.fetchall()
    return [_row_to_recipient(r) for r in rows]



# ---------- хендлеры бота ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я бот для получения уведомлений.\n\n"
        "Чтобы начать:\n"
        "1) Выполни команду /subscribe.\n"
        "2) Отправь мне свой nickname одним сообщением (например, user1).\n\n"
        "Потом в любом Python-коде можно будет вызвать:\n"
        "message('<твой_nickname>', 'Вычисления закончены!')\n\n"
        "Полезные команды:\n"
        "• /whoami — посмотреть свой chat_id и привязанные nickname\n"
        "• /unsubscribe — отписаться от рассылки\n"
        "• /cancel — отменить текущее действие\n\n"
        "Команды для администраторов:\n"
        "• /list_users — список всех пользователей\n"
        "• /setrole — назначить пользователю роль\n"
        "• /unsubscribe_user — удалить пользователя из базы\n"
    )
    await update.message.reply_text(text)


async def cancel_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, действие отменили.")
    return ConversationHandler.END

async def subscribe_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Старт подписки: просим пользователя отправить nickname отдельным сообщением."""
    await update.message.reply_text(
        "Ок, давай привяжем твой nickname.\n"
        "Отправь мне *одно слово* или строку — это и будет твой nickname.\n"
        "Пример: `user1`\n\n"
        "Если передумал — отправь /cancel.",
        parse_mode="Markdown",
    )
    return SUBSCRIBE_NICKNAME


async def subscribe_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nickname = update.message.text.strip()
    if not nickname:
        raise BotUserError("nickname не должен быть пустым. Попробуй ещё раз или отправь /cancel.")

    chat_id = update.effective_chat.id
    username = update.effective_user.username

    # save_recipient сам выбросит BotUserError, если что-то не так
    save_recipient(nickname, chat_id, username)

    await update.message.reply_text(
        f"Готово! nickname '{nickname}' теперь привязан к этому чату.\n"
        f"Можешь использовать его в вызове message('{nickname}', '...')."
    )
    return ConversationHandler.END


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда отписки."""
    chat_id = update.effective_chat.id
    deleted = unsubscribe_chat(chat_id)
    if deleted:
        await update.message.reply_text(
            "Ты отписался от уведомлений. Чтобы снова подписаться, выполни /subscribe."
        )
    else:
        await update.message.reply_text("Активных подписок для этого чата не найдено.")


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает chat_id, username и привязанные nickname с ролями."""
    chat_id = update.effective_chat.id
    username = update.effective_user.username
    recipients = get_recipients_by_chat(chat_id)

    lines = [
        f"Твой chat_id: {chat_id}",
        f"username: @{username if username else '—'}",
    ]

    if not recipients:
        lines.append("Привязанных nickname нет. Используй /subscribe.")
    else:
        lines.append("\nnickname:")
        for r in recipients:
            lines.append(f"- {r.nickname} (роль: {r.role})")
    await update.message.reply_text("\n".join(lines))


# ---------- команды админа ----------

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список всех пользователей (только для админов)."""
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        raise BotUserError("Эта команда доступна только администраторам.")

    recipients = get_all_recipients()
    if not recipients:
        await update.message.reply_text("В базе ещё нет пользователей.")
        return

    lines = []
    for r in recipients:
        lines.append(
            f"{r.nickname}: chat_id={r.chat_id}, "
            f"username=@{r.username or '—'}, role={r.role}"
        )

    await update.message.reply_text("\n".join(lines))

async def setrole_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Шаг 1: админ пишет /setrole,
    бот показывает кнопки с выбором роли (admin/user).
    """
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        raise BotUserError("Эта команда доступна только администраторам.")

    keyboard = [
        [
            InlineKeyboardButton("admin", callback_data="role:admin"),
            InlineKeyboardButton("user", callback_data="role:user"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Выбери роль, которую нужно назначить пользователю:",
        reply_markup=reply_markup,
    )
    return SETROLE_CHOOSE_ROLE


async def setrole_choose_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Шаг 2: обработка нажатия кнопки с ролью.
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("role:"):
        raise BotUserError("Неверный выбор роли. Попробуй ещё раз: /setrole.")

    role = data.split(":", 1)[1]
    if role not in ("admin", "user"):
        raise BotUserError("Роль должна быть 'admin' или 'user'.")

    context.user_data["target_role"] = role

    await query.edit_message_text(
        f"Выбрана роль: {role}.\n"
        f"Теперь отправь nickname пользователя отдельным сообщением.\n"
        f"Если передумал — /cancel."
    )
    return SETROLE_WAIT_NICKNAME


async def setrole_receive_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Шаг 3: принимаем nickname для уже выбранной роли.
    """
    nickname = update.message.text.strip()
    if not nickname:
        raise BotUserError("nickname не должен быть пустым. Попробуй ещё раз или отправь /cancel.")

    role = context.user_data.get("target_role")
    if role not in ("admin", "user"):
        context.user_data.pop("target_role", None)
        raise BotUserError("Что-то пошло не так с выбором роли. Начни заново: /setrole.")

    # Проверяем, есть ли такой пользователь
    recipient = get_recipient_by_nickname(nickname)
    if recipient is None:
        context.user_data.pop("target_role", None)
        raise BotUserError(
            f"Пользователь с nickname '{nickname}' не найден.\n"
            f"Сначала он должен выполнить /subscribe и выбрать этот nickname."
        )

    old_role = recipient.role

    # Если роль уже такая же — просто сообщаем об этом
    if old_role == role:
        await update.message.reply_text(
            f"У пользователя '{nickname}' уже роль {role} — ничего менять не нужно."
        )
        context.user_data.pop("target_role", None)
        return ConversationHandler.END

    # Меняем роль
    updated = set_role(nickname, role)
    if not updated:
        context.user_data.pop("target_role", None)
        raise BotUserError(
            f"Не удалось изменить роль пользователя '{nickname}'. Попробуй ещё раз."
        )

    await update.message.reply_text(
        f"Роль пользователя '{nickname}' изменена: {old_role} → {role}."
    )
    context.user_data.pop("target_role", None)
    return ConversationHandler.END

async def unsubscribe_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Шаг 1: админ пишет /unsubscribe_user,
    бот просит отправить nickname, которого нужно удалить из БД.
    """
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        raise BotUserError("Эта команда доступна только администраторам.")

    await update.message.reply_text(
        "Ок, кого выпиливаем? 🙂\n"
        "Отправь мне nickname пользователя, которого нужно полностью удалить из базы.\n"
        "Если передумал — /cancel."
    )
    return UNSUB_USER_WAIT_NICKNAME


async def unsubscribe_user_receive_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Шаг 2: принимаем nickname и удаляем его из БД.
    """
    nickname = update.message.text.strip()
    if not nickname:
        raise BotUserError("nickname не должен быть пустым. Попробуй ещё раз или отправь /cancel.")

    deleted = delete_by_nickname(nickname)
    if not deleted:
        raise BotUserError(f"Пользователь с nickname='{nickname}' не найден.")

    await update.message.reply_text(
        f"Пользователь с nickname='{nickname}' удалён из базы."
    )
    return ConversationHandler.END


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Реакция на любое текстовое сообщение, которое не является командой
    и не обрабатывается активными ConversationHandler'ами.
    """
    await update.message.reply_text(
        "Я понимаю только команды 🙂\n"
        "Посмотри меню в Telegram или набери /start.\n\n"
        "Основные команды:\n"
        "/subscribe – подписаться и привязать nickname\n"
        "/unsubscribe – отписаться от уведомлений\n"
        "/whoami – показать твой профиль"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Логируем всё с трейсбеком
    logger.exception("Exception while handling an update:", exc_info=context.error)

    # Текст для пользователя
    if isinstance(context.error, BotUserError):
        # "Ожидаемая" ошибка - показываем текст как есть
        text = str(context.error)
    else:
        # Любая другая - говорим, что что-то сломалось
        text = "⚠️ Что-то пошло не так. Попробуй ещё раз чуть позже."

    # Пытаемся отправить ответ в тот же чат
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message(text)
        except TelegramError:
            logger.exception("Failed to send error message to user", exc_info=True)


# ---------- запуск приложения ----------

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Переменная окружения TELEGRAM_BOT_TOKEN не установлена")

    init_db()

    app = Application.builder().token(token).build()

    # conversation для /subscribe
    subscribe_conv = ConversationHandler(
        entry_points=[CommandHandler("subscribe", subscribe_start)],
        states={
            SUBSCRIBE_NICKNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, subscribe_receive)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_all)],
    )

    # conversation для /setrole
    setrole_conv = ConversationHandler(
        entry_points=[CommandHandler("setrole", setrole_start)],
        states={
            SETROLE_CHOOSE_ROLE: [
                CallbackQueryHandler(
                    setrole_choose_role,
                    pattern=r"^role:(admin|user)$",
                )
            ],
            SETROLE_WAIT_NICKNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, setrole_receive_nickname)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_all)],
    )

    # conversation для /unsubscribe_user
    unsub_user_conv = ConversationHandler(
        entry_points=[CommandHandler("unsubscribe_user", unsubscribe_user_start)],
        states={
            UNSUB_USER_WAIT_NICKNAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, unsubscribe_user_receive_nickname
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_all)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(unsub_user_conv)
    app.add_handler(subscribe_conv)
    app.add_handler(setrole_conv)
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("whoami", whoami))

    # админская команда (list_users — обычный handler)
    app.add_handler(CommandHandler("list_users", list_users))

    # неожиданное текстовое сообщение
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    app.add_error_handler(error_handler)

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()