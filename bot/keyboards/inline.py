from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import config
from bot.services.sportvsegda import DateInfo, Session

DAY_SHORT = {
    "понедельник": "пн",
    "вторник": "вт",
    "среда": "ср",
    "четверг": "чт",
    "пятница": "пт",
    "суббота": "сб",
    "воскресенье": "вс",
}


def dates_keyboard(dates: list[DateInfo]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for d in dates:
        dow = DAY_SHORT.get(d.day_of_week, d.day_of_week)
        count = len(d.sessions)
        builder.button(
            text=f"{d.date} ({dow}) — {count} сеанс.",
            callback_data=f"date:{d.date}",
        )
    builder.adjust(1)
    return builder.as_markup()


def sessions_keyboard(sessions: list[Session], date: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s in sessions:
        builder.button(
            text=s.time_range,
            callback_data=f"session:{s.date}|{s.time_range}",
        )
    builder.button(text="<< Назад к датам", callback_data="back:dates")
    builder.adjust(2)
    return builder.as_markup()


def persons_keyboard(date: str, time_range: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, person in enumerate(config.persons):
        builder.button(
            text=person.name,
            callback_data=f"person:{i}|{date}|{time_range}",
        )
    builder.button(
        text="Все сразу",
        callback_data=f"person:all|{date}|{time_range}",
    )
    builder.button(text="<< Назад", callback_data=f"date:{date}")
    builder.adjust(2)
    return builder.as_markup()


def buy_link_keyboard(url: str, date: str, time_range: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть страницу покупки", url=url)
    builder.button(text="Следующий билет", callback_data=f"session:{date}|{time_range}")
    builder.button(text="<< В начало", callback_data="show:dates")
    builder.adjust(1)
    return builder.as_markup()


def start_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Посмотреть сеансы", callback_data="show:dates")
    builder.button(text="Мои заказы", callback_data="show:orders")
    builder.adjust(1)
    return builder.as_markup()
