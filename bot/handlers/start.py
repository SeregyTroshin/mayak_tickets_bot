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

        # Test clicking "+" and see what happens
        click_test = await page.evaluate("""async () => {
            const result = [];

            // Before click
            const inp = document.querySelector('.chek_ticket_item input');
            result.push('BEFORE: value=' + (inp ? inp.value : 'N/A'));
            const total1 = document.querySelector('.summ_itog');
            result.push('BEFORE total: ' + (total1 ? total1.textContent : 'N/A'));

            // Find plus button
            const plus = document.querySelector('.chek_ticket_item .number .plus');
            result.push('Plus btn: ' + (plus ? 'FOUND tag=' + plus.tagName : 'NOT FOUND'));

            // Check event listeners - look at onclick
            if (plus) {
                result.push('Plus onclick: ' + (plus.onclick ? String(plus.onclick).substring(0, 100) : 'null'));
                result.push('Plus parent: ' + plus.parentElement.className);

                // Try clicking
                plus.click();
                await new Promise(r => setTimeout(r, 500));
                result.push('AFTER click: value=' + (inp ? inp.value : 'N/A'));
            }

            // Check for jQuery events on plus
            if (typeof jQuery !== 'undefined' || typeof $ !== 'undefined') {
                result.push('jQuery: YES');
                try {
                    const jq = jQuery || $;
                    const events = jq._data(plus, 'events') || {};
                    result.push('Plus jQuery events: ' + Object.keys(events).join(','));
                } catch(e) {
                    result.push('jQuery events error: ' + e);
                }
            } else {
                result.push('jQuery: NO');
            }

            // Check for common calc functions
            const fns = ['calc_summ', 'calcSumm', 'calculate', 'calculateSum',
                         'update_total', 'updateTotal', 'calc_total', 'calcTotal',
                         'recalc', 'get_prices', 'getPrices', 'calc_order',
                         'check_summ', 'checkSumm'];
            const found = fns.filter(f => typeof window[f] === 'function');
            result.push('Global functions: ' + (found.length ? found.join(', ') : 'NONE'));

            // Try calling found functions
            for (const f of found) {
                try {
                    window[f]();
                    result.push('Called ' + f + '()');
                } catch(e) {
                    result.push(f + '() error: ' + e);
                }
            }
            await new Promise(r => setTimeout(r, 1000));

            const total2 = document.querySelector('.summ_itog');
            result.push('AFTER funcs total: ' + (total2 ? total2.textContent : 'N/A'));

            // Try setting value directly and triggering events
            if (inp && inp.value === '0') {
                inp.value = '1';
                inp.dispatchEvent(new Event('input', {bubbles: true}));
                inp.dispatchEvent(new Event('change', {bubbles: true}));
                inp.dispatchEvent(new Event('keyup', {bubbles: true}));
                inp.dispatchEvent(new Event('blur', {bubbles: true}));

                // Try calc functions again
                for (const f of found) {
                    try { window[f](); } catch(e) {}
                }
                await new Promise(r => setTimeout(r, 1000));
                const total3 = document.querySelector('.summ_itog');
                result.push('AFTER direct set total: ' + (total3 ? total3.textContent : 'N/A'));
                result.push('Input value now: ' + inp.value);
            }

            // Dump all script src
            const scripts = document.querySelectorAll('script[src]');
            const srcs = [];
            scripts.forEach(s => srcs.push(s.src.split('/').pop()));
            result.push('Scripts: ' + srcs.join(', '));

            return result;
        }""")
        report.append(f"Click test: {json.dumps(click_test, ensure_ascii=False)}")

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
