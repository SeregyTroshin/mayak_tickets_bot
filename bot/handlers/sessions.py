import logging
from urllib.parse import quote

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
    cancel_keyboard,
)
from bot.db.models import save_order

router = Router()
client = SportVsegdaClient(stadium_id=config.stadium_id)
log = logging.getLogger(__name__)

BASE_URL = "https://sportvsegda.ru"

# Кэш расписания
_schedule_cache: dict = {}


class PurchaseStates(StatesGroup):
    waiting_cvc = State()
    waiting_sms = State()


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


# ─── Расписание ──────────────────────────────────────────────────────────────


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
async def show_dates(callback: CallbackQuery, state: FSMContext):
    await state.clear()
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
async def back_to_dates(callback: CallbackQuery, state: FSMContext):
    await state.clear()
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


# ─── Выбор человека → подготовка покупки → самари ────────────────────────────


@router.callback_query(F.data.startswith("person:"))
async def select_person(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":", 1)[1]
    person_idx, date, time_range = parts.split("|")

    if person_idx == "all":
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

    if not _has_playwright():
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
        return

    # ── Автоматический режим: Фаза 1 — заполняем форму, узнаём сумму ──

    await callback.message.edit_text(
        f"Сеанс: {date} {time_range}\n"
        f"Билет для: {person.name}\n"
        f"{promo_line}\n\n"
        "Заполняю форму на сайте...",
        parse_mode="Markdown",
    )
    await callback.answer()

    from bot.services.purchase import prepare_purchase

    result = await prepare_purchase(
        user_id=callback.from_user.id,
        stadium_id=config.stadium_id,
        date=date,
        time_range=time_range,
        promo=person.promo,
        person_name=person.name,
        name=config.customer_name,
        phone=config.customer_phone,
        email=config.customer_email,
    )

    if not result.success:
        # Fallback на ссылку
        url = _buy_url(date, time_range)
        error = result.error or "Неизвестная ошибка"
        await callback.message.edit_text(
            f"Билет для: {person.name}\n"
            f"{promo_line}\n\n"
            f"Не удалось заполнить форму: {error}\n"
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
            status="error",
        )
        return

    # ── Показать самари и запросить CVC для подтверждения ──

    total = result.total_amount or "не удалось определить"
    card_masked = f"****{config.card_number[-4:]}"

    summary = (
        f"Билет для: {person.name}\n"
        f"Сеанс: {date} {time_range}\n"
        f"{promo_line}\n"
        f"Сумма: {total}\n"
        f"Карта: {card_masked}\n\n"
        "Для подтверждения покупки введите CVC код карты.\n"
        "Для отмены нажмите кнопку ниже."
    )

    await callback.message.edit_text(
        summary,
        reply_markup=cancel_keyboard(date, time_range),
        parse_mode="Markdown",
    )

    # Сохранить контекст покупки в FSM
    await state.set_state(PurchaseStates.waiting_cvc)
    await state.update_data(
        person_idx=int(person_idx),
        date=date,
        time_range=time_range,
    )


# ─── Ввод CVC → подтверждение и оплата ──────────────────────────────────────


@router.message(PurchaseStates.waiting_cvc)
async def process_cvc(message: Message, state: FSMContext):
    cvc = message.text.strip()

    if not cvc.isdigit() or len(cvc) != 3:
        await message.answer(
            "CVC должен быть 3 цифры. Попробуйте ещё раз, или нажмите Отмена."
        )
        return

    data = await state.get_data()
    person_idx = data["person_idx"]
    date = data["date"]
    time_range = data["time_range"]
    person = config.persons[person_idx]

    await state.clear()
    await message.answer("Оплачиваю...")

    from bot.services.purchase import confirm_and_pay

    result = await confirm_and_pay(
        user_id=message.from_user.id,
        card_number=config.card_number,
        card_expiry=config.card_expiry,
        card_cvv=cvc,
    )

    await _handle_payment_result(message, state, result, person, date, time_range, person_idx)


# ─── Ввод SMS-кода 3D-Secure ────────────────────────────────────────────────


@router.message(PurchaseStates.waiting_sms)
async def process_sms(message: Message, state: FSMContext):
    sms_code = message.text.strip()

    if not sms_code.isdigit() or len(sms_code) < 4 or len(sms_code) > 8:
        await message.answer(
            "Введите код из SMS (4-8 цифр), или нажмите Отмена."
        )
        return

    data = await state.get_data()
    person_idx = data["person_idx"]
    date = data["date"]
    time_range = data["time_range"]
    person = config.persons[person_idx]

    await message.answer("Отправляю код подтверждения...")

    from bot.services.purchase import complete_3ds

    result = await complete_3ds(
        user_id=message.from_user.id,
        sms_code=sms_code,
    )

    await _handle_payment_result(message, state, result, person, date, time_range, person_idx)


# ─── Общая обработка результата оплаты ───────────────────────────────────────


async def _handle_payment_result(
    message: Message,
    state: FSMContext,
    result,
    person,
    date: str,
    time_range: str,
    person_idx: int,
):
    """Обработать результат confirm_and_pay или complete_3ds."""

    if result.success:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="Следующий билет",
            callback_data=f"session:{date}|{time_range}",
        )
        builder.button(text="<< В начало", callback_data="show:dates")
        builder.adjust(1)

        total = result.total_amount or "—"
        await message.answer(
            f"Оплата прошла успешно!\n\n"
            f"Билет: {person.name}\n"
            f"Сеанс: {date} {time_range}\n"
            f"Сумма: {total}",
            reply_markup=builder.as_markup(),
        )

        await save_order(
            user_id=message.from_user.id,
            date=date,
            time_range=time_range,
            person_name=person.name,
            promo=person.promo,
            status="paid",
        )
        return

    if result.needs_sms:
        # 3D-Secure — браузер жив, просим SMS-код
        error = result.error or "Банк запросил код из SMS."
        await message.answer(
            f"{error}\n\n"
            "Введите код из SMS для подтверждения оплаты:",
            reply_markup=cancel_keyboard(date, time_range),
        )
        await state.set_state(PurchaseStates.waiting_sms)
        await state.update_data(
            person_idx=person_idx,
            date=date,
            time_range=time_range,
        )
        return

    if result.payment_url:
        builder = InlineKeyboardBuilder()
        builder.button(text="Открыть для оплаты", url=result.payment_url)
        builder.button(
            text="Следующий билет",
            callback_data=f"session:{date}|{time_range}",
        )
        builder.button(text="<< В начало", callback_data="show:dates")
        builder.adjust(1)

        error = result.error or ""
        await message.answer(
            f"{error}\n\n"
            f"Билет: {person.name}\n"
            f"Сеанс: {date} {time_range}\n"
            "Откройте ссылку для завершения оплаты:",
            reply_markup=builder.as_markup(),
        )

        await save_order(
            user_id=message.from_user.id,
            date=date,
            time_range=time_range,
            person_name=person.name,
            promo=person.promo,
            status="payment_link",
        )
        return

    # Ошибка
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Попробовать снова",
        callback_data=f"session:{date}|{time_range}",
    )
    builder.button(text="<< В начало", callback_data="show:dates")
    builder.adjust(1)

    error = result.error or "Неизвестная ошибка"
    await message.answer(
        f"Ошибка оплаты: {error}",
        reply_markup=builder.as_markup(),
    )

    await save_order(
        user_id=message.from_user.id,
        date=date,
        time_range=time_range,
        person_name=person.name,
        promo=person.promo,
        status="error",
    )


# ─── Кнопка «Отмена» во время ожидания CVC ──────────────────────────────────


@router.callback_query(F.data.startswith("cancel_purchase"))
async def cancel_purchase_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()

    from bot.services.purchase import cancel_purchase
    await cancel_purchase(callback.from_user.id)

    # Извлечь date|time_range если переданы
    parts = callback.data.split("|")
    if len(parts) == 3:
        _, date, time_range = parts
        await callback.message.edit_text(
            "Покупка отменена.",
            reply_markup=persons_keyboard(date, time_range),
        )
    else:
        await callback.message.edit_text(
            "Покупка отменена.",
            reply_markup=start_keyboard(),
        )
    await callback.answer()
