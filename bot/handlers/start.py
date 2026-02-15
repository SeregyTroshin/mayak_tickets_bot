from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery

from bot.keyboards.inline import start_keyboard
from bot.db.models import get_orders

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для покупки билетов на каток Маяк.\n\n"
        "Выберите действие:",
        reply_markup=start_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Команды:\n"
        "/start — главное меню\n"
        "/sessions — доступные сеансы\n"
        "/orders — мои заказы\n"
        "/help — помощь",
    )


@router.callback_query(F.data == "show:orders")
async def show_orders(callback: CallbackQuery):
    orders = await get_orders(callback.from_user.id)
    if not orders:
        await callback.message.edit_text(
            "У вас пока нет заказов.",
            reply_markup=start_keyboard(),
        )
    else:
        lines = []
        for o in orders:
            promo_text = f" (промо: {o['promo']})" if o["promo"] else ""
            lines.append(
                f"- {o['date']} {o['time_range']} — {o['person_name']}{promo_text} [{o['status']}]"
            )
        await callback.message.edit_text(
            "Ваши заказы:\n\n" + "\n".join(lines),
            reply_markup=start_keyboard(),
        )
    await callback.answer()
