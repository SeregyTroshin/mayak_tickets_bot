import logging
from urllib.parse import quote

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import config
from bot.services.sportvsegda import SportVsegdaClient
from bot.keyboards.inline import (
    dates_keyboard,
    sessions_keyboard,
    persons_keyboard,
    buy_link_keyboard,
    start_keyboard,
)
from bot.db.models import save_order

router = Router()
client = SportVsegdaClient(stadium_id=config.stadium_id)
log = logging.getLogger(__name__)

BASE_URL = "https://sportvsegda.ru"

# Кэш расписания
_schedule_cache: dict = {}


def _buy_url(date: str, time_range: str) -> str:
    return (
        f"{BASE_URL}/mass_skating_tickets/"
        f"?stadium={config.stadium_id}&type=1"
        f"&date={quote(date)}&time={quote(time_range)}"
    )


def _has_playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


async def _load_schedule():
    global _schedule_cache
    dates = await client.get_schedule()
    _schedule_cache = {d.date: d for d in dates}
    return dates


@router.message(Command("sessions"))
async def cmd_sessions(message: Message):
    await message.answer("Загружаю расписание с сайта...")
    dates = await _load_schedule()
    if not dates:
        await message.answer(
            "Нет доступных сеансов на данный момент.",
            reply_markup=start_keyboard(),
        )
        return
    await message.answer(
        "Каток Маяк — массовое катание\nВыберите дату:",
        reply_markup=dates_keyboard(dates),
    )


@router.callback_query(F.data == "show:dates")
async def show_dates(callback: CallbackQuery):
    await callback.message.edit_text("Загружаю расписание с сайта...")
    dates = await _load_schedule()
    if not dates:
        await callback.message.edit_text(
            "Нет доступных сеансов на данный момент.",
            reply_markup=start_keyboard(),
        )
    else:
        await callback.message.edit_text(
            "Каток Маяк — массовое катание\nВыберите дату:",
            reply_markup=dates_keyboard(dates),
        )
    await callback.answer()


@router.callback_query(F.data == "back:dates")
async def back_to_dates(callback: CallbackQuery):
    dates = list(_schedule_cache.values()) if _schedule_cache else await _load_schedule()
    if not dates:
        await callback.message.edit_text(
            "Нет доступных сеансов.", reply_markup=start_keyboard()
        )
    else:
        await callback.message.edit_text(
            "Каток Маяк — массовое катание\nВыберите дату:",
            reply_markup=dates_keyboard(dates),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("date:"))
async def select_date(callback: CallbackQuery):
    date = callback.data.split(":", 1)[1]
    date_info = _schedule_cache.get(date)
    if not date_info or not date_info.sessions:
        await callback.message.edit_text(
            f"На {date} нет сеансов массового катания.",
            reply_markup=start_keyboard(),
        )
    else:
        await callback.message.edit_text(
            f"Каток Маяк — {date} ({date_info.day_of_week})\nВыберите сеанс:",
            reply_markup=sessions_keyboard(date_info.sessions, date),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("session:"))
async def select_session(callback: CallbackQuery):
    parts = callback.data.split(":", 1)[1]
    date, time_range = parts.split("|", 1)
    await callback.message.edit_text(
        f"Сеанс: {date} {time_range}\n\n"
        "Для кого покупаем билет?",
        reply_markup=persons_keyboard(date, time_range),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("person:"))
async def select_person(callback: CallbackQuery):
    parts = callback.data.split(":", 1)[1]
    person_idx, date, time_range = parts.split("|")

    if person_idx == "all":
        # Список всех — показываем сводку и предлагаем покупать по одному
        lines = [f"Сеанс: {date} {time_range}\n"]
        for i, p in enumerate(config.persons):
            promo = f"промо `{p.promo}`" if p.promo else "полная цена"
            lines.append(f"  {i+1}. {p.name} — {promo}")
        lines.append(f"\nВсего {len(config.persons)} билетов.")
        lines.append("Покупаем по одному — выберите первого:")

        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=persons_keyboard(date, time_range),
            parse_mode="Markdown",
        )
        await callback.answer()
        return

    person = config.persons[int(person_idx)]
    promo_line = f"Промокод: `{person.promo}`" if person.promo else "Без промокода (полная цена)"

    if _has_playwright():
        # Автоматический режим — заполняем форму через браузер
        await callback.message.edit_text(
            f"Сеанс: {date} {time_range}\n"
            f"Билет для: {person.name}\n"
            f"{promo_line}\n\n"
            "Заполняю форму на сайте...",
            parse_mode="Markdown",
        )
        await callback.answer()

        from bot.services.purchase import purchase_ticket

        result = await purchase_ticket(
            stadium_id=config.stadium_id,
            date=date,
            time_range=time_range,
            promo=person.promo,
            name=config.customer_name,
            phone=config.customer_phone,
            email=config.customer_email,
        )

        if result.success and result.payment_url:
            total = result.total_amount or "см. на странице"
            builder = InlineKeyboardBuilder()
            builder.button(text=f"Оплатить ({total})", url=result.payment_url)
            builder.button(text="Следующий билет", callback_data=f"session:{date}|{time_range}")
            builder.button(text="<< В начало", callback_data="show:dates")
            builder.adjust(1)

            await callback.message.edit_text(
                f"Билет для: {person.name}\n"
                f"Сеанс: {date} {time_range}\n"
                f"{promo_line}\n"
                f"Сумма: {total}\n\n"
                "Нажмите кнопку ниже для оплаты картой:",
                reply_markup=builder.as_markup(),
                parse_mode="Markdown",
            )
        else:
            # Fallback на ссылку
            url = _buy_url(date, time_range)
            error = result.error or "Неизвестная ошибка"
            await callback.message.edit_text(
                f"Билет для: {person.name}\n"
                f"{promo_line}\n\n"
                f"Не удалось автозаполнить: {error}\n"
                "Откройте ссылку и заполните вручную:",
                reply_markup=buy_link_keyboard(url, date, time_range),
                parse_mode="Markdown",
            )

        await save_order(
            user_id=callback.from_user.id,
            date=date,
            time_range=time_range,
            person_name=person.name,
            promo=person.promo,
            status="payment_link" if result.success else "error",
        )
    else:
        # Ручной режим — ссылка + подсказки
        url = _buy_url(date, time_range)
        text = (
            f"Сеанс: {date} {time_range}\n"
            f"Билет для: {person.name}\n"
            f"{promo_line}\n\n"
            f"Имя: `{config.customer_name}`\n"
            f"Тел: `{config.customer_phone}`\n"
            f"Email: `{config.customer_email}`\n\n"
            "Откройте ссылку, введите данные, отметьте галочки и оплатите картой."
        )

        await callback.message.edit_text(
            text,
            reply_markup=buy_link_keyboard(url, date, time_range),
            parse_mode="Markdown",
        )

        await save_order(
            user_id=callback.from_user.id,
            date=date,
            time_range=time_range,
            person_name=person.name,
            promo=person.promo,
            status="link_sent",
        )

    await callback.answer()
