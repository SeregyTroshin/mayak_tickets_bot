import json
import logging

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery

from bot.keyboards.inline import start_keyboard
from bot.db.models import get_orders

router = Router()
log = logging.getLogger(__name__)


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


@router.message(Command("debug"))
async def cmd_debug(message: Message):
    """Диагностика Playwright — показать что видит бот на странице."""
    await message.answer("Запускаю диагностику Playwright...")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        await message.answer("Playwright не установлен!")
        return

    report = []
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--single-process", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="ru-RU",
        )
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => false});
            window.chrome = {runtime: {}};
        """)
        page = await ctx.new_page()
        report.append("Browser OK")

        url = "https://sportvsegda.ru/mass_skating_tickets/?stadium=2&type=1&date=18.02.2026&time=08:00%20-%2009:00"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        report.append(f"Page loaded: {page.url[:80]}")

        has_form = await page.locator("#orderForm").count()
        report.append(f"#orderForm: {'YES' if has_form else 'NO'}")

        # Wait 5 sec for DDoS-Guard
        await page.wait_for_timeout(5000)
        report.append("Waited 5s for DDoS-Guard JS")

        # Check cookies
        cookies = await ctx.cookies()
        ddg_cookies = [c["name"] for c in cookies if "ddg" in c["name"].lower()]
        report.append(f"DDoS cookies: {ddg_cookies}")

        # Test AJAX from browser
        ajax = await page.evaluate("""async () => {
            try {
                const r = await fetch('/assets/ajax/get_ranges_hockey.php?stadium=2&date=18.02.2026&promo=');
                const d = await r.json();
                return {status: r.status, ranges: (d.Ranges||[]).length, prices: (d.TicketPrices||[]).length, raw: JSON.stringify(d).substring(0, 200)};
            } catch(e) { return {error: String(e)}; }
        }""")
        report.append(f"AJAX fetch: {json.dumps(ajax, ensure_ascii=False)}")

        # Check page's own JS state
        state = await page.evaluate("""() => {
            const items = document.querySelectorAll('.chek_ticket_item');
            const itemsHtml = [];
            items.forEach((el, i) => itemsHtml.push('ITEM' + i + ': ' + el.innerHTML.substring(0, 300)));
            return {
                hasRanges: typeof ranges !== 'undefined' ? ranges.length : -1,
                hasPrices: typeof prices !== 'undefined' ? prices.length : -1,
                ticketItems: items.length,
                webdriver: navigator.webdriver,
                itemsHtml: itemsHtml,
                orderFormHtml: document.querySelector('#orderForm') ? document.querySelector('#orderForm').innerHTML.substring(0, 500) : 'no orderForm',
            };
        }""")
        report.append(f"Page JS state: {json.dumps(state, ensure_ascii=False)}")

        # Check all inputs inside ticket items
        inputs_info = await page.evaluate("""() => {
            const items = document.querySelectorAll('.chek_ticket_item');
            const result = [];
            items.forEach((item, i) => {
                const inputs = item.querySelectorAll('input');
                inputs.forEach((inp, j) => {
                    result.push('item' + i + '_inp' + j + ': type=' + inp.type + ' name=' + inp.name + ' class=' + inp.className + ' value=' + inp.value);
                });
                // Also check for number div structure
                const numDiv = item.querySelector('.number');
                result.push('item' + i + '_numDiv: ' + (numDiv ? numDiv.innerHTML.substring(0, 200) : 'NONE'));
            });
            // Check contact fields
            const fName = document.querySelector('input[name="f_Name"]');
            const fPhone = document.querySelector('input[name="f_Phone"]');
            const fEmail = document.querySelector('input[name="f_Email"]');
            result.push('f_Name: ' + (fName ? 'YES' : 'NO'));
            result.push('f_Phone: ' + (fPhone ? 'YES' : 'NO'));
            result.push('f_Email: ' + (fEmail ? 'YES' : 'NO'));
            // Check promo
            const promo = document.querySelector('input[name="f_Promo"]') || document.querySelector('#promocode');
            result.push('promo: ' + (promo ? 'YES tag=' + promo.tagName + ' name=' + promo.name : 'NO'));
            // Check payment radio
            const pay2 = document.querySelector('#payment_2');
            result.push('payment_2: ' + (pay2 ? 'YES type=' + pay2.type : 'NO'));
            // Check submit
            const submit = document.querySelector('button[type="submit"], input[type="submit"], .order_submit');
            result.push('submit: ' + (submit ? 'YES tag=' + submit.tagName + ' text=' + (submit.textContent||'').substring(0, 50) : 'NO'));
            // Check total
            const total = document.querySelector('.summ_itog');
            result.push('summ_itog: ' + (total ? total.textContent : 'NO'));
            return result;
        }""")
        report.append(f"Inputs: {json.dumps(inputs_info, ensure_ascii=False)}")

        # Check select#order_range options
        range_info = await page.evaluate("""() => {
            const sel = document.querySelector('#order_range');
            if (!sel) return 'no #order_range';
            const opts = [];
            for (const o of sel.options) opts.push(o.value + ' (type=' + (o.dataset.type||'?') + ')');
            return 'options: ' + opts.join(', ');
        }""")
        report.append(f"order_range: {range_info}")

        await browser.close()
        await pw.stop()

    except Exception as e:
        report.append(f"ERROR: {e}")

    # Split into chunks of 4000 chars for Telegram limit
    full_text = "Диагностика:\n\n" + "\n".join(report)
    for i in range(0, len(full_text), 4000):
        await message.answer(full_text[i:i+4000])


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
