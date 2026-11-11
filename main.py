import logging
import os
from datetime import datetime, timezone
from collections import Counter

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, delete, func, or_, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from telegram import BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gift_market_dating")

AGE, NAME, CITY, GENDER, LOOKING_FOR, BIO, PHOTO, SHARE_USERNAME = range(8)
ADMIN_IDS = {int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip().isdigit()}


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profiles"

    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(40))
    age: Mapped[int] = mapped_column(Integer)
    city: Mapped[str] = mapped_column(String(60))
    gender: Mapped[str] = mapped_column(String(12), default="other")
    looking_for: Mapped[str] = mapped_column(String(12), default="any")
    bio: Mapped[str] = mapped_column(Text)
    photo_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    share_username: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SearchPreference(Base):
    __tablename__ = "search_preferences"

    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city_only: Mapped[bool] = mapped_column(Boolean, default=False)
    age_filter: Mapped[str | None] = mapped_column(String(16), nullable=True)


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("from_id", "to_id", name="uq_vote_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_id: Mapped[int] = mapped_column(ForeignKey("profiles.telegram_id", ondelete="CASCADE"))
    to_id: Mapped[int] = mapped_column(ForeignKey("profiles.telegram_id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(12))


class Block(Base):
    __tablename__ = "blocks"
    __table_args__ = (UniqueConstraint("from_id", "to_id", name="uq_block_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_id: Mapped[int] = mapped_column(ForeignKey("profiles.telegram_id", ondelete="CASCADE"))
    to_id: Mapped[int] = mapped_column(ForeignKey("profiles.telegram_id", ondelete="CASCADE"))


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_id: Mapped[int] = mapped_column(Integer)
    to_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


def make_database():
    url = os.getenv("DATABASE_URL", "sqlite:///gift_market_dating.sqlite3")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+" not in url.split(":", 1)[0]:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    options = {"pool_pre_ping": True}
    if url.startswith("sqlite:"):
        options["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **options)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


Session = make_database()


def load_search_preferences(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    with Session() as db:
        preferences = db.get(SearchPreference, user_id)
        if preferences is None:
            context.user_data.setdefault("city_only", False)
            context.user_data.setdefault("age_filter", None)
        else:
            context.user_data["city_only"] = preferences.city_only
            context.user_data["age_filter"] = preferences.age_filter


def save_search_preferences(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    with Session.begin() as db:
        preferences = db.get(SearchPreference, user_id)
        if preferences is None:
            preferences = SearchPreference(telegram_id=user_id)
            db.add(preferences)
        preferences.city_only = bool(context.user_data.get("city_only", False))
        preferences.age_filter = context.user_data.get("age_filter")


AGE_FILTER_LABELS = {
    "18-25": "18–25",
    "26-35": "26–35",
    "36-49": "36–49",
    "50plus": "50+",
}


def age_filter_label(age_filter):
    return AGE_FILTER_LABELS.get(age_filter, "любой")


def menu_markup(city_only=False, age_filter=None, is_admin=False):
    city_label = "📍 Поиск: мой город ✓" if city_only else "🌍 Поиск: все города"
    age_label = f"🎂 Возраст: {age_filter_label(age_filter)}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Смотреть анкеты", callback_data="menu:next")],
        [InlineKeyboardButton("👤 Моя анкета", callback_data="menu:profile"),
         InlineKeyboardButton("💞 Взаимные симпатии", callback_data="menu:matches")],
        [InlineKeyboardButton(city_label, callback_data="menu:city")],
        [InlineKeyboardButton(age_label, callback_data="menu:age")],
        [InlineKeyboardButton("❤️ Кто лайкнул меня", callback_data="menu:likes")],
        [InlineKeyboardButton("📝 Создать / обновить анкету", callback_data="menu:create")],
        *([[InlineKeyboardButton("👥 Пользователи", callback_data="admin:users:0")]] if is_admin else []),
    ])


def cancel_markup(button_rows=None):
    rows = list(button_rows or [])
    rows.append([InlineKeyboardButton("❌ Отменить создание", callback_data="profile:cancel")])
    return InlineKeyboardMarkup(rows)


async def home_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    load_search_preferences(update.effective_user.id, context)
    context.user_data.pop("likes_inbox", None)
    await query.message.reply_text(
        "Главное меню — выбери действие:",
        reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), update.effective_user.id in ADMIN_IDS),
    )


async def age_filter_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    load_search_preferences(update.effective_user.id, context)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("18–25", callback_data="age:set:18-25"),
            InlineKeyboardButton("26–35", callback_data="age:set:26-35"),
        ],
        [
            InlineKeyboardButton("36–49", callback_data="age:set:36-49"),
            InlineKeyboardButton("50+", callback_data="age:set:50plus"),
        ],
        [InlineKeyboardButton("Любой возраст", callback_data="age:set:any")],
        [InlineKeyboardButton("↩️ Главное меню", callback_data="menu:home")],
    ])
    await query.message.reply_text(
        f"Выбери возраст анкет. Сейчас: {age_filter_label(context.user_data.get('age_filter'))}.",
        reply_markup=keyboard,
    )


async def set_age_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    load_search_preferences(update.effective_user.id, context)
    selected = query.data.split(":", 2)[2]
    context.user_data["age_filter"] = None if selected == "any" else selected
    save_search_preferences(update.effective_user.id, context)
    label = age_filter_label(context.user_data.get("age_filter"))
    await query.message.reply_text(
        f"Возрастной фильтр: {label}.",
        reply_markup=menu_markup(
            context.user_data.get("city_only", False),
            context.user_data.get("age_filter"),
            update.effective_user.id in ADMIN_IDS,
        ),
    )


async def toggle_city_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    load_search_preferences(update.effective_user.id, context)
    city_only = not context.user_data.get("city_only", False)
    context.user_data["city_only"] = city_only
    save_search_preferences(update.effective_user.id, context)
    label = "только в твоём городе" if city_only else "по всем городам"
    await query.message.reply_text(
        f"Поиск анкет настроен: {label}.",
        reply_markup=menu_markup(city_only, context.user_data.get("age_filter"), update.effective_user.id in ADMIN_IDS),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    load_search_preferences(update.effective_user.id, context)
    context.user_data.pop("likes_inbox", None)
    message = update.effective_message
    await message.reply_text(
        "Привет! Это новый бот знакомств — тестовая версия для совершеннолетних.\n\n"
        "Создай анкету, а затем смотри анкеты и отмечай симпатию. При взаимном лайке бот сообщит об этом обоим.\n\n"
        "Анкеты видны другим пользователям бота. Telegram username показывается при совпадении только если ты отдельно разрешишь это. Используй /delete, чтобы удалить анкету.\n\n"
        "Продолжая, подтверди, что тебе уже исполнилось 18 лет.",
        reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), update.effective_user.id in ADMIN_IDS),
    )


async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            "Сколько тебе лет? Анкеты доступны только с 18 лет.",
            reply_markup=cancel_markup(),
        )
    else:
        await update.effective_message.reply_text(
            "Сколько тебе лет? Анкеты доступны только с 18 лет.",
            reply_markup=cancel_markup(),
        )
    return AGE


async def age_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.effective_message.text or "").strip()
    if not value.isdigit():
        await update.effective_message.reply_text("Напиши возраст числом, например 24.", reply_markup=cancel_markup())
        return AGE
    age = int(value)
    if age < 18:
        await update.effective_message.reply_text("Создавать анкету можно только с 18 лет. Диалог остановлен.")
        return ConversationHandler.END
    if age > 99:
        await update.effective_message.reply_text("Проверь возраст и введи число от 18 до 99.", reply_markup=cancel_markup())
        return AGE
    context.user_data["profile"] = {"age": age}
    await update.effective_message.reply_text("Как к тебе обращаться? Напиши имя или псевдоним (до 40 символов).", reply_markup=cancel_markup())
    return NAME


async def name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.effective_message.text or "").strip()
    if not 2 <= len(name) <= 40:
        await update.effective_message.reply_text("Имя должно быть от 2 до 40 символов. Попробуй ещё раз.", reply_markup=cancel_markup())
        return NAME
    context.user_data["profile"]["display_name"] = name
    await update.effective_message.reply_text("В каком ты городе?", reply_markup=cancel_markup())
    return CITY


async def city_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = (update.effective_message.text or "").strip()
    if not 2 <= len(city) <= 60:
        await update.effective_message.reply_text("Напиши город (от 2 до 60 символов).", reply_markup=cancel_markup())
        return CITY
    context.user_data["profile"]["city"] = city
    await update.effective_message.reply_text("Как указать пол в анкете?", reply_markup=cancel_markup([
        [InlineKeyboardButton("Мужчина", callback_data="gender:man"), InlineKeyboardButton("Женщина", callback_data="gender:woman")],
        [InlineKeyboardButton("Другое / не указывать", callback_data="gender:other")],
    ]))
    return GENDER


async def gender_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["profile"]["gender"] = query.data.split(":", 1)[1]
    await query.edit_message_text("Кого ты хочешь видеть в анкетах?", reply_markup=cancel_markup([
        [InlineKeyboardButton("Мужчин", callback_data="looking_for:man"), InlineKeyboardButton("Женщин", callback_data="looking_for:woman")],
        [InlineKeyboardButton("Всех", callback_data="looking_for:any")],
    ]))
    return LOOKING_FOR


async def looking_for_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["profile"]["looking_for"] = query.data.split(":", 1)[1]
    await query.edit_message_text(
        "Коротко расскажи о себе (до 500 символов). Не указывай адрес или другие личные данные.",
        reply_markup=cancel_markup(),
    )
    return BIO


async def bio_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bio = (update.effective_message.text or "").strip()
    if not 2 <= len(bio) <= 500:
        await update.effective_message.reply_text("Текст должен быть от 2 до 500 символов. Попробуй ещё раз.", reply_markup=cancel_markup())
        return BIO
    context.user_data["profile"]["bio"] = bio
    await update.effective_message.reply_text(
        "Можно отправить одну фотографию или пропустить этот шаг.",
        reply_markup=cancel_markup([[InlineKeyboardButton("Пропустить фото", callback_data="photo:skip")]]),
    )
    return PHOTO


async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        context.user_data["profile"]["photo_file_id"] = None
        await query.edit_message_text(
            "Разрешаешь показать свой Telegram username взаимной симпатии? Выбери ниже.",
            reply_markup=cancel_markup([
                [InlineKeyboardButton("Разрешаю показать", callback_data="share:yes")],
                [InlineKeyboardButton("Не показывать", callback_data="share:no")],
            ]),
        )
        return SHARE_USERNAME
    if update.effective_message.photo:
        context.user_data["profile"]["photo_file_id"] = update.effective_message.photo[-1].file_id
        await update.effective_message.reply_text("Разрешаешь показать свой Telegram username взаимной симпатии? Выбери ниже.", reply_markup=cancel_markup([
            [InlineKeyboardButton("Разрешаю показать", callback_data="share:yes")],
            [InlineKeyboardButton("Не показывать", callback_data="share:no")],
        ]))
        return SHARE_USERNAME
    if (update.effective_message.text or "").strip().lower() in {"пропустить", "нет", "skip"}:
        context.user_data["profile"]["photo_file_id"] = None
        await update.effective_message.reply_text("Разрешаешь показать свой Telegram username взаимной симпатии? Выбери ниже.", reply_markup=cancel_markup([
            [InlineKeyboardButton("Разрешаю показать", callback_data="share:yes")],
            [InlineKeyboardButton("Не показывать", callback_data="share:no")],
        ]))
        return SHARE_USERNAME
    await update.effective_message.reply_text(
        "Отправь фотографию или нажми «Пропустить фото».",
        reply_markup=cancel_markup([[InlineKeyboardButton("Пропустить фото", callback_data="photo:skip")]]),
    )
    return PHOTO


async def share_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    allow = query.data == "share:yes"
    user = update.effective_user
    values = context.user_data.pop("profile", {})
    with Session.begin() as db:
        profile = db.get(Profile, user.id)
        if profile is None:
            profile = Profile(telegram_id=user.id, **values)
            db.add(profile)
        else:
            for key, value in values.items():
                setattr(profile, key, value)
        profile.share_username = allow
        profile.telegram_username = user.username if allow else None
    await query.edit_message_text("Анкета сохранена! Её увидят другие пользователи бота. Используй /next, чтобы начать просмотр.\n\nДля удаления анкеты — /delete.")
    return ConversationHandler.END


def card_text(profile: Profile):
    gender_text = {"man": "мужчина", "woman": "женщина", "other": ""}.get(profile.gender, "")
    details = [f"💫 {profile.display_name}, {profile.age}"]
    if gender_text:
        details.append(f"👤 {gender_text}")
    details.append(f"📍 {profile.city}")
    details.extend(["", f"💬 {profile.bio}"])
    return "\n".join(details)


def profile_keyboard(username_shared):
    username_label = (
        "🔓 Скрыть username при совпадении"
        if username_shared else
        "🔒 Разрешить username при совпадении"
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Изменить анкету", callback_data="menu:create")],
        [InlineKeyboardButton(username_label, callback_data="menu:username")],
        [InlineKeyboardButton("🔎 Смотреть анкеты", callback_data="menu:next")],
        [InlineKeyboardButton("💞 Взаимные симпатии", callback_data="menu:matches")],
        [InlineKeyboardButton("🗑 Удалить анкету", callback_data="menu:delete")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:home")],
    ])


async def toggle_username_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    with Session.begin() as db:
        profile = db.get(Profile, user.id)
        if profile is None:
            shared = None
        elif not profile.share_username and not user.username:
            shared = "missing_username"
        else:
            profile.share_username = not profile.share_username
            profile.telegram_username = user.username if profile.share_username else None
            shared = profile.share_username
    if shared is None:
        await query.answer("Сначала создай анкету.", show_alert=True)
        return
    if shared == "missing_username":
        await query.answer("Сначала добавь username в настройках Telegram.", show_alert=True)
        return
    label = (
        "Username будет виден только после взаимного лайка."
        if shared else
        "Username скрыт и удалён из анкеты."
    )
    await query.answer(label, show_alert=False)
    await query.message.edit_reply_markup(reply_markup=profile_keyboard(shared))


async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    user_id = update.effective_user.id
    with Session() as db:
        profile = db.get(Profile, user_id)
        if profile is None:
            await update.effective_message.reply_text("Анкеты пока нет. Создай её: /create")
            return
        text = "Твоя анкета:\n\n" + card_text(profile)
        photo_id = profile.photo_file_id
        username_shared = profile.share_username
    keyboard = profile_keyboard(username_shared)
    if photo_id:
        await update.effective_message.reply_photo(photo=photo_id, caption=text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def matches_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    load_search_preferences(update.effective_user.id, context)
    if update.callback_query:
        await update.callback_query.answer()
    user_id = update.effective_user.id
    with Session() as db:
        own = db.get(Profile, user_id)
        if own is None:
            await update.effective_message.reply_text("Сначала создай анкету: /create", reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), update.effective_user.id in ADMIN_IDS))
            return
        liked_by_me = set(db.scalars(
            select(Vote.to_id).where(Vote.from_id == user_id, Vote.action == "like")
        ).all())
        liked_me = set(db.scalars(
            select(Vote.from_id).where(Vote.to_id == user_id, Vote.action == "like")
        ).all())
        matches = db.scalars(
            select(Profile).where(Profile.telegram_id.in_(liked_by_me & liked_me)).order_by(Profile.created_at.desc())
        ).all()
        lines = []
        for profile in matches:
            if profile.share_username and profile.telegram_username:
                lines.append(f"• {profile.display_name} — @{profile.telegram_username}")
            else:
                lines.append(f"• {profile.display_name} — контакт не открыт")
    if not lines:
        await update.effective_message.reply_text("Пока нет взаимных симпатий. Продолжай смотреть анкеты: /next", reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), update.effective_user.id in ADMIN_IDS))
        return
    await update.effective_message.reply_text("Твои взаимные симпатии:\n\n" + "\n".join(lines), reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), update.effective_user.id in ADMIN_IDS))


async def send_next(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, reply_to=None):
    load_search_preferences(user_id, context)
    with Session() as db:
        me = db.get(Profile, user_id)
        if me is None:
            msg = "Сначала создай анкету: /create"
            if reply_to:
                await reply_to.reply_text(msg)
            else:
                await context.bot.send_message(chat_id=chat_id, text=msg)
            return
        already_seen = select(Vote.to_id).where(Vote.from_id == user_id)
        blocked = select(Block.to_id).where(Block.from_id == user_id).union(select(Block.from_id).where(Block.to_id == user_id))
        gender_match = True if me.looking_for == "any" else Profile.gender == me.looking_for
        preference_match = or_(Profile.looking_for == "any", Profile.looking_for == me.gender)
        candidate_query = select(Profile).where(
            Profile.telegram_id != user_id,
            Profile.telegram_id.not_in(already_seen),
            Profile.telegram_id.not_in(blocked),
            gender_match,
            preference_match,
        )
        city_only = context.user_data.get("city_only", False)
        age_filter = context.user_data.get("age_filter")
        age_bounds = {
            "18-25": (18, 25),
            "26-35": (26, 35),
            "36-49": (36, 49),
            "50plus": (50, 99),
        }.get(age_filter)
        if age_bounds:
            candidate_query = candidate_query.where(Profile.age.between(*age_bounds))
        if city_only:
            candidate_query = candidate_query.where(
                func.lower(func.trim(Profile.city)) == me.city.strip().lower()
            )
        candidate = db.scalar(candidate_query.order_by(Profile.created_at.desc()).limit(1))
        fallback_used = False
        if candidate is None and not city_only:
            fallback_query = select(Profile).where(
                Profile.telegram_id != user_id,
                Profile.telegram_id.not_in(already_seen),
                Profile.telegram_id.not_in(blocked),
            )
            if age_bounds:
                fallback_query = fallback_query.where(Profile.age.between(*age_bounds))
            candidate = db.scalar(
                fallback_query.order_by(Profile.created_at.desc()).limit(1)
            )
            fallback_used = candidate is not None
        if candidate is None:
            if city_only and age_bounds:
                msg = "В твоём городе и выбранном возрасте новых анкет пока нет. Измени фильтры в главном меню."
            elif city_only:
                msg = "В твоём городе новых анкет пока нет. Можно отключить фильтр в главном меню."
            elif age_bounds:
                msg = "Новых анкет в выбранном возрасте пока нет. Измени фильтр в главном меню."
            else:
                msg = "Пока больше нет новых анкет. Загляни позже."
            if reply_to:
                await reply_to.reply_text(msg, reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), user_id in ADMIN_IDS))
            else:
                await context.bot.send_message(chat_id=chat_id, text=msg, reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), user_id in ADMIN_IDS))
            return
        text = card_text(candidate)
        if fallback_used:
            fallback_note = (
                "Подходящих по полу и другим настройкам нет — показываю анкеты выбранного возраста."
                if age_bounds else
                "Подходящих по выбранным настройкам пока нет — показываю другие анкеты."
            )
            text = fallback_note + chr(10) + chr(10) + text
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("❤️ Нравится", callback_data=f"like:{candidate.telegram_id}"),
            InlineKeyboardButton("👎 Пропустить", callback_data=f"pass:{candidate.telegram_id}"),
        ], [
            InlineKeyboardButton("🚩 Жалоба", callback_data=f"report:{candidate.telegram_id}"),
            InlineKeyboardButton("⛔ Блокировать", callback_data=f"block:{candidate.telegram_id}"),
        ], [
            InlineKeyboardButton("↩️ Вернуть пропущенную", callback_data="card:back"),
        ], [
            InlineKeyboardButton("🏠 Главное меню", callback_data="menu:home"),
        ]])
        photo_id = candidate.photo_file_id
    if photo_id:
        await context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text, reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)

async def previous_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    load_search_preferences(update.effective_user.id, context)
    context.user_data.pop("likes_inbox", None)
    user_id = update.effective_user.id
    with Session() as db:
        me = db.get(Profile, user_id)
        if me is None:
            await query.message.reply_text("Сначала создай анкету: /create")
            return
        blocked = select(Block.to_id).where(Block.from_id == user_id).union(
            select(Block.from_id).where(Block.to_id == user_id)
        )
        reported = select(Report.to_id).where(Report.from_id == user_id)
        previous_query = (
            select(Profile)
            .join(Vote, Vote.to_id == Profile.telegram_id)
            .where(
                Vote.from_id == user_id,
                Vote.action == "pass",
                Profile.telegram_id.not_in(blocked),
                Profile.telegram_id.not_in(reported),
            )
        )
        if context.user_data.get("city_only", False):
            previous_query = previous_query.where(
                func.lower(func.trim(Profile.city)) == me.city.strip().lower()
            )
        candidate = db.scalar(previous_query.order_by(Vote.id.desc()).limit(1))
        photo_id = candidate.photo_file_id if candidate else None
        text = "↩️ Последняя пропущенная анкета\n\n" + card_text(candidate) if candidate else None
        target_id = candidate.telegram_id if candidate else None

    if candidate is None:
        await query.message.reply_text(
            "Пока нет пропущенных анкет, к которым можно вернуться.",
            reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), update.effective_user.id in ADMIN_IDS),
        )
        return

    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❤️ Нравится", callback_data=f"like:{target_id}"),
            InlineKeyboardButton("👎 Пропустить", callback_data=f"pass:{target_id}"),
        ],
        [
            InlineKeyboardButton("🚩 Жалоба", callback_data=f"report:{target_id}"),
            InlineKeyboardButton("⛔ Блокировать", callback_data=f"block:{target_id}"),
        ],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:home")],
    ])
    if photo_id:
        await context.bot.send_photo(chat_id=query.message.chat_id, photo=photo_id, caption=text, reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=keyboard)


async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("likes_inbox", None)
    await send_next(update.effective_chat.id, update.effective_user.id, context, update.effective_message)


async def send_next_incoming(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    load_search_preferences(user_id, context)
    context.user_data["likes_inbox"] = True
    with Session() as db:
        me = db.get(Profile, user_id)
        if me is None:
            context.user_data.pop("likes_inbox", None)
            await context.bot.send_message(
                chat_id=chat_id,
                text="Сначала создай анкету, чтобы смотреть входящие симпатии: /create",
                reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), user_id in ADMIN_IDS),
            )
            return
        already_voted = select(Vote.to_id).where(Vote.from_id == user_id)
        blocked = select(Block.to_id).where(Block.from_id == user_id).union(
            select(Block.from_id).where(Block.to_id == user_id)
        )
        liked_me = select(Vote.from_id).where(Vote.to_id == user_id, Vote.action == "like")
        candidate = db.scalar(
            select(Profile)
            .where(
                Profile.telegram_id != user_id,
                Profile.telegram_id.in_(liked_me),
                Profile.telegram_id.not_in(already_voted),
                Profile.telegram_id.not_in(blocked),
            )
            .order_by(Profile.created_at.desc())
            .limit(1)
        )
        photo_id = candidate.photo_file_id if candidate else None
        text = card_text(candidate) if candidate else None
        target_id = candidate.telegram_id if candidate else None

    if candidate is None:
        context.user_data.pop("likes_inbox", None)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Новых лайков пока нет. Когда кто-то отметит тебя, анкета появится здесь.",
            reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), user_id in ADMIN_IDS),
        )
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❤️ Ответить", callback_data=f"like:{target_id}"),
            InlineKeyboardButton("👎 Пропустить", callback_data=f"pass:{target_id}"),
        ],
        [
            InlineKeyboardButton("🚩 Жалоба", callback_data=f"report:{target_id}"),
            InlineKeyboardButton("⛔ Блокировать", callback_data=f"block:{target_id}"),
        ],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:home")],
    ])
    if photo_id:
        await context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text, reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


async def incoming_likes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_next_incoming(query.message.chat_id, update.effective_user.id, context)


async def card_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, target_raw = query.data.split(":", 1)
    user_id = update.effective_user.id
    target_id = int(target_raw)
    matched = False
    target_name = ""
    target_username = None
    target_share = False
    own_name = update.effective_user.first_name
    own_share = False
    with Session.begin() as db:
        own_profile = db.get(Profile, user_id)
        if own_profile is None:
            await query.message.reply_text("Сначала создай анкету: /create")
            return
        own_name = own_profile.display_name
        own_share = own_profile.share_username
        target = db.get(Profile, target_id)
        if target is None:
            await query.message.reply_text("Эта анкета больше недоступна.")
            return
        target_name = target.display_name
        target_username = target.telegram_username
        target_share = target.share_username
        if action == "report":
            db.add(Report(from_id=user_id, to_id=target_id))
            db.execute(delete(Vote).where(Vote.from_id == user_id, Vote.to_id == target_id))
            db.add(Vote(from_id=user_id, to_id=target_id, action="pass"))
        elif action == "block":
            db.add(Block(from_id=user_id, to_id=target_id))
            db.execute(delete(Vote).where(Vote.from_id == user_id, Vote.to_id == target_id))
            db.add(Vote(from_id=user_id, to_id=target_id, action="pass"))
        else:
            vote = db.scalar(select(Vote).where(Vote.from_id == user_id, Vote.to_id == target_id))
            if vote is None:
                db.add(Vote(from_id=user_id, to_id=target_id, action="like" if action == "like" else "pass"))
            else:
                vote.action = "like" if action == "like" else "pass"
            if action == "like":
                reverse = db.scalar(select(Vote).where(Vote.from_id == target_id, Vote.to_id == user_id, Vote.action == "like"))
                matched = reverse is not None
    if action == "like" and matched:
        own = update.effective_user
        own_handle = own.username
        other_contact = f"@{target_username}" if target_share and target_username else "Пользователь не разрешил показывать username или не указал его."
        own_contact = f"@{own_handle}" if own_share and own_handle else "Пользователь не разрешил показывать username или не указал его."
        await context.bot.send_message(user_id, f"🎉 Взаимная симпатия с {target_name}!\nКонтакт: {other_contact}")
        await context.bot.send_message(target_id, f"🎉 Взаимная симпатия с {own_name}!\nКонтакт: {own_contact}")
    elif action == "like":
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="💌 Кто-то поставил лайк твоей анкете. Открой входящие симпатии, чтобы посмотреть анкету.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❤️ Посмотреть лайк", callback_data="menu:likes")
                ]]),
            )
        except Exception as exc:
            logger.info("Could not notify user %s about an incoming like: %s", target_id, exc)
    elif action == "report":
        await query.message.reply_text("Жалоба принята. Анкета пропущена.")
    elif action == "block":
        await query.message.reply_text("Пользователь заблокирован и больше не появится в твоём просмотре.")
    try:
        await query.message.delete()
    except Exception:
        pass
    if context.user_data.get("likes_inbox"):
        await send_next_incoming(query.message.chat_id, user_id, context)
    else:
        await send_next(query.message.chat_id, user_id, context)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


async def reports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if update.effective_chat.type != "private":
        await message.reply_text("Открой личный чат с ботом, чтобы просмотреть жалобы.")
        return
    user = update.effective_user
    if not ADMIN_IDS:
        await message.reply_text("Модерация ещё не настроена. Добавь свой ID в переменную ADMIN_IDS на сервере.")
        return
    if user is None or user.id not in ADMIN_IDS:
        await message.reply_text("Эта команда доступна только модераторам.")
        return
    with Session() as db:
        reported_ids = db.scalars(select(Report.to_id).order_by(Report.created_at.desc()).limit(200)).all()
        counts = Counter(reported_ids)
        cards = []
        buttons = []
        for target_id, count in counts.most_common(10):
            profile = db.get(Profile, target_id)
            if profile is not None:
                cards.append(f"• ID {target_id}: {profile.display_name}, {profile.age}, {profile.city} — жалоб: {count}")
                buttons.append([InlineKeyboardButton(f"Удалить анкету {target_id}", callback_data=f"mod:ask_remove:{target_id}")])
    if not cards:
        await message.reply_text("Новых жалоб нет.")
        return
    text = "Жалобы на анкеты (до 10, по числу обращений):" + chr(10) + chr(10) + chr(10).join(cards)
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def remove_profile_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if update.effective_chat.type != "private" or user is None or user.id not in ADMIN_IDS:
        await message.reply_text("Команда доступна только модераторам в личном чате с ботом.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await message.reply_text("Формат команды: /removeprofile ID")
        return
    target_id = int(context.args[0])
    with Session() as db:
        profile = db.get(Profile, target_id)
        display_name = profile.display_name if profile is not None else None
    if display_name is None:
        await message.reply_text("Анкета не найдена.")
        return
    context.user_data["pending_admin_remove"] = target_id
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Подтвердить удаление", callback_data=f"mod:confirm_remove:{target_id}"),
        InlineKeyboardButton("Отмена", callback_data=f"mod:cancel:{target_id}"),
    ]])
    await message.reply_text(f"Удалить анкету «{display_name}» (ID {target_id}) и связанные отметки?", reply_markup=keyboard)


async def moderation_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if update.effective_chat.type != "private" or user is None or user.id not in ADMIN_IDS:
        await query.answer("Нет доступа.", show_alert=True)
        return
    await query.answer()
    _, action, target_raw = query.data.split(":", 2)
    target_id = int(target_raw)
    if action == "ask_remove":
        with Session() as db:
            profile = db.get(Profile, target_id)
            display_name = profile.display_name if profile is not None else None
        if display_name is None:
            await query.edit_message_text("Анкета уже удалена.")
            return
        context.user_data["pending_admin_remove"] = target_id
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Подтвердить удаление", callback_data=f"mod:confirm_remove:{target_id}"),
            InlineKeyboardButton("Отмена", callback_data=f"mod:cancel:{target_id}"),
        ]])
        await query.edit_message_text(f"Удалить анкету «{display_name}» (ID {target_id}) и связанные отметки?", reply_markup=keyboard)
        return
    if action == "cancel":
        if context.user_data.get("pending_admin_remove") == target_id:
            context.user_data.pop("pending_admin_remove", None)
        await query.edit_message_text("Удаление отменено.")
        return
    if context.user_data.get("pending_admin_remove") != target_id:
        await query.edit_message_text("Подтверждение устарело. Начни снова через /removeprofile ID.")
        return
    with Session.begin() as db:
        db.execute(delete(Vote).where(or_(Vote.from_id == target_id, Vote.to_id == target_id)))
        db.execute(delete(Block).where(or_(Block.from_id == target_id, Block.to_id == target_id)))
        db.execute(delete(Report).where(or_(Report.from_id == target_id, Report.to_id == target_id)))
        db.execute(delete(Profile).where(Profile.telegram_id == target_id))
    context.user_data.pop("pending_admin_remove", None)
    await query.edit_message_text("Анкета и связанные отметки удалены.")



ADMIN_USERS_PAGE_SIZE = 8


def admin_only_private(update: Update) -> bool:
    return (
        update.effective_chat is not None
        and update.effective_chat.type == "private"
        and update.effective_user is not None
        and update.effective_user.id in ADMIN_IDS
    )


async def admin_users_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not admin_only_private(update):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await query.answer()
    page = max(0, int(query.data.rsplit(":", 1)[1]))
    with Session() as db:
        total = db.scalar(select(func.count()).select_from(Profile)) or 0
        last_page = max(0, (total - 1) // ADMIN_USERS_PAGE_SIZE)
        page = min(page, last_page)
        profiles = db.scalars(
            select(Profile)
            .order_by(Profile.created_at.desc(), Profile.telegram_id.desc())
            .offset(page * ADMIN_USERS_PAGE_SIZE)
            .limit(ADMIN_USERS_PAGE_SIZE)
        ).all()
        users = [
            (p.telegram_id, p.display_name, p.age, p.city)
            for p in profiles
        ]
    rows = [
        [InlineKeyboardButton(
            f"{name[:24]}, {age} — {city[:20]}",
            callback_data=f"admin:user:{telegram_id}:{page}",
        )]
        for telegram_id, name, age, city in users
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:users:{page - 1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton("Дальше ➡️", callback_data=f"admin:users:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:home")])
    text = f"👥 Пользователи: {total}\nСтраница {page + 1} из {last_page + 1}"
    if total == 0:
        text += "\nПока никто не создал анкету."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def admin_user_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not admin_only_private(update):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await query.answer()
    _, _, target_raw, page_raw = query.data.split(":")
    target_id = int(target_raw)
    page = max(0, int(page_raw))
    with Session() as db:
        profile = db.get(Profile, target_id)
        if profile is None:
            details = None
        else:
            details = {
                "id": profile.telegram_id,
                "name": profile.display_name,
                "age": profile.age,
                "city": profile.city,
                "gender": profile.gender,
                "looking_for": profile.looking_for,
                "bio": profile.bio,
                "photo": profile.photo_file_id,
                "username": profile.telegram_username if profile.share_username else None,
                "created_at": profile.created_at,
            }
    if details is None:
        await query.edit_message_text(
            "Анкета не найдена.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ К списку", callback_data=f"admin:users:{page}")
            ]]),
        )
        return
    gender_labels = {"man": "мужчина", "woman": "женщина", "other": "другое"}
    looking_labels = {"man": "мужчин", "woman": "женщин", "any": "всех"}
    username_line = f"Telegram: @{details['username']}" if details["username"] else "Telegram: скрыт пользователем"
    created = details["created_at"]
    created_text = created.strftime("%d.%m.%Y") if created else "неизвестно"
    caption = (
        f"👤 {details['name']}, {details['age']}\n"
        f"ID: {details['id']}\n"
        f"Пол: {gender_labels.get(details['gender'], details['gender'])}\n"
        f"Ищет: {looking_labels.get(details['looking_for'], details['looking_for'])}\n"
        f"Город: {details['city']}\n"
        f"{username_line}\n"
        f"В боте с: {created_text}\n\n"
        f"{details['bio'] or 'Описание не заполнено.'}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ К списку", callback_data=f"admin:users:{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:home")],
    ])
    if details["photo"]:
        await query.message.reply_photo(
            photo=details["photo"],
            caption=caption[:1024],
            reply_markup=keyboard,
        )
    else:
        await query.edit_message_text(caption[:4000], reply_markup=keyboard)


async def delete_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    load_search_preferences(update.effective_user.id, context)
    if update.callback_query:
        await update.callback_query.answer()
    user_id = update.effective_user.id
    with Session() as db:
        exists = db.get(Profile, user_id) is not None
    if not exists:
        await update.effective_message.reply_text(
            "У тебя пока нет анкеты.",
            reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), update.effective_user.id in ADMIN_IDS),
        )
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить", callback_data="delete:confirm")],
        [InlineKeyboardButton("↩️ Оставить анкету", callback_data="delete:cancel")],
    ])
    await update.effective_message.reply_text(
        "Удалить анкету? Вместе с ней будут удалены лайки, жалобы и блокировки, связанные с твоим профилем.",
        reply_markup=keyboard,
    )


async def delete_profile_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    load_search_preferences(update.effective_user.id, context)
    user_id = update.effective_user.id
    with Session.begin() as db:
        db.execute(delete(Vote).where(or_(Vote.from_id == user_id, Vote.to_id == user_id)))
        db.execute(delete(Block).where(or_(Block.from_id == user_id, Block.to_id == user_id)))
        db.execute(delete(Report).where(or_(Report.from_id == user_id, Report.to_id == user_id)))
        db.execute(delete(Profile).where(Profile.telegram_id == user_id))
    context.user_data.pop("likes_inbox", None)
    await query.message.reply_text(
        "Анкета и связанные с ней лайки удалены.",
        reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), update.effective_user.id in ADMIN_IDS),
    )


async def delete_profile_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    load_search_preferences(update.effective_user.id, context)
    await query.message.reply_text(
        "Анкета сохранена.",
        reply_markup=menu_markup(context.user_data.get("city_only", False), context.user_data.get("age_filter"), update.effective_user.id in ADMIN_IDS),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("profile", None)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("Создание анкеты отменено.")
    else:
        await update.effective_message.reply_text("Создание анкеты отменено.")
    return ConversationHandler.END


async def post_init(application: Application):
    public_commands = [
        ("start", "Открыть меню"),
        ("create", "Создать или обновить анкету"),
        ("next", "Смотреть анкеты"),
        ("myprofile", "Моя анкета"),
        ("matches", "Взаимные симпатии"),
        ("delete", "Удалить свою анкету"),
        ("cancel", "Отменить создание анкеты"),
        ("myid", "Показать свой Telegram ID"),
    ]
    admin_commands = public_commands + [
        ("reports", "Просмотреть жалобы"),
        ("removeprofile", "Удалить анкету по ID"),
    ]
    await application.bot.set_my_commands(public_commands)
    for admin_id in ADMIN_IDS:
        await application.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in the environment before starting the bot.")
    app = Application.builder().token(token).post_init(post_init).build()
    profile_flow = ConversationHandler(
        entry_points=[CommandHandler("create", create_start), CallbackQueryHandler(create_start, pattern="^menu:create$")],
        states={
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_received)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_received)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, city_received)],
            GENDER: [CallbackQueryHandler(gender_received, pattern="^gender:(man|woman|other)$")],
            LOOKING_FOR: [CallbackQueryHandler(looking_for_received, pattern="^looking_for:(man|woman|any)$")],
            BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, bio_received)],
            PHOTO: [
                CallbackQueryHandler(photo_received, pattern="^photo:skip$"),
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), photo_received),
            ],
            SHARE_USERNAME: [CallbackQueryHandler(share_received, pattern="^share:(yes|no)$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^profile:cancel$"),
        ],
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(profile_flow)
    app.add_handler(CommandHandler("next", next_command))
    app.add_handler(CommandHandler("myprofile", my_profile))
    app.add_handler(CommandHandler("matches", matches_command))
    app.add_handler(CommandHandler("delete", delete_profile))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CommandHandler("reports", reports_command))
    app.add_handler(CommandHandler("removeprofile", remove_profile_admin))
    app.add_handler(CallbackQueryHandler(moderation_action, pattern="^mod:(ask_remove|confirm_remove|cancel):\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_users_page, pattern="^admin:users:\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_user_detail, pattern="^admin:user:\\d+:\\d+$"))
    app.add_handler(CallbackQueryHandler(next_command, pattern="^menu:next$"))
    app.add_handler(CallbackQueryHandler(my_profile, pattern="^menu:profile$"))
    app.add_handler(CallbackQueryHandler(toggle_username_visibility, pattern="^menu:username$"))
    app.add_handler(CallbackQueryHandler(matches_command, pattern="^menu:matches$"))
    app.add_handler(CallbackQueryHandler(incoming_likes, pattern="^menu:likes$"))
    app.add_handler(CallbackQueryHandler(home_menu, pattern="^menu:home$"))
    app.add_handler(CallbackQueryHandler(delete_profile, pattern="^menu:delete$"))
    app.add_handler(CallbackQueryHandler(delete_profile_confirm, pattern="^delete:confirm$"))
    app.add_handler(CallbackQueryHandler(delete_profile_cancel, pattern="^delete:cancel$"))
    app.add_handler(CallbackQueryHandler(toggle_city_search, pattern="^menu:city$"))
    app.add_handler(CallbackQueryHandler(age_filter_menu, pattern="^menu:age$"))
    app.add_handler(CallbackQueryHandler(set_age_filter, pattern="^age:set:(18-25|26-35|36-49|50plus|any)$"))
    app.add_handler(CallbackQueryHandler(previous_profile, pattern="^card:back$"))
    app.add_handler(CallbackQueryHandler(card_action, pattern="^(like|pass|report|block):\\d+$"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
