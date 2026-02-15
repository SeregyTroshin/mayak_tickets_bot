"""Автоматизация покупки билетов через headless-браузер Playwright.

Flow:
1. Открыть страницу с нужным сеансом (дата + время предзаполнены через URL)
2. Выбрать 1 билет
3. Ввести промокод (если есть)
4. Заполнить ФИО, телефон, email
5. Выбрать оплату картой
6. Отметить 3 галочки
7. Нажать "Оплатить"
8. Перехватить URL редиректа на платёжную систему
9. Вернуть этот URL пользователю
"""

import logging
from dataclasses import dataclass
from urllib.parse import quote

log = logging.getLogger(__name__)

BASE_URL = "https://sportvsegda.ru"


@dataclass
class PurchaseResult:
    success: bool
    payment_url: str | None = None
    error: str | None = None
    total_amount: str | None = None


async def purchase_ticket(
    stadium_id: int,
    date: str,
    time_range: str,
    promo: str | None,
    name: str,
    phone: str,
    email: str,
) -> PurchaseResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return PurchaseResult(
            success=False,
            error="Playwright не установлен. Установите: pip install playwright && playwright install chromium",
        )

    url = (
        f"{BASE_URL}/mass_skating_tickets/"
        f"?stadium={stadium_id}&type=1"
        f"&date={quote(date)}&time={quote(time_range)}"
    )

    log.info("Starting purchase: %s %s for %s", date, time_range, name)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            # 1. Открыть страницу с предзаполненным сеансом
            await page.goto(url, wait_until="networkidle", timeout=30000)
            log.info("Page loaded")

            # Подождать загрузки формы
            await page.wait_for_selector("#orderForm", timeout=10000)

            # 2. Дождаться загрузки сеансов и выбрать 1 билет
            # Страница должна автоматически подставить дату и время из URL
            # Ждём появления поля количества билетов
            ticket_input = await page.wait_for_selector(
                ".chek_ticket_item input[type=text]", timeout=15000
            )
            if ticket_input:
                await ticket_input.fill("1")
                await ticket_input.dispatch_event("change")
                log.info("Ticket quantity set to 1")

            # 3. Ввести промокод (если есть)
            if promo:
                promo_input = page.locator("#promocode")
                await promo_input.fill(promo)
                # Нажать кнопку применения промокода
                apply_btn = page.locator(".apply_promo")
                if await apply_btn.count() > 0:
                    await apply_btn.click()
                    await page.wait_for_timeout(2000)  # Ждём пересчёт цены
                log.info("Promo code applied: %s", promo)

            # 4. Заполнить контактные данные
            await page.locator('input[name="f_Name"]').fill(name)
            await page.locator('input[name="f_Phone"]').fill(phone)
            await page.locator('input[name="f_Email"]').fill(email)
            log.info("Contact info filled")

            # 5. Выбрать оплату картой (value="2")
            await page.locator("#payment_2").check()
            log.info("Card payment selected")

            # 6. Отметить 3 галочки
            await page.locator("#order_agree_oferta").check()
            await page.locator("#order_agree_policy").check()
            await page.locator("#order_agree_personal").check()
            log.info("Agreements checked")

            # Считать итоговую сумму
            total_el = page.locator(".summ_itog")
            total_amount = await total_el.text_content() if await total_el.count() > 0 else None
            log.info("Total: %s", total_amount)

            # 7. Перехватить редирект после submit
            payment_url = None

            # Слушаем навигацию — после submit должен быть редирект на банк
            async with page.expect_navigation(
                url="**", wait_until="commit", timeout=30000
            ) as nav_info:
                # Нажать "Оплатить"
                await page.locator('button[type="submit"]').click()
                log.info("Submit clicked")

            response = await nav_info.value
            payment_url = page.url
            log.info("Redirected to: %s", payment_url)

            # Если URL изменился и это не sportvsegda.ru — это страница банка
            if payment_url and "sportvsegda.ru" not in payment_url:
                return PurchaseResult(
                    success=True,
                    payment_url=payment_url,
                    total_amount=total_amount,
                )

            # Если остались на том же сайте — возможно AJAX submit
            # Проверим, нет ли результата в DOM
            await page.wait_for_timeout(5000)
            payment_url = page.url
            if "sportvsegda.ru" not in payment_url:
                return PurchaseResult(
                    success=True,
                    payment_url=payment_url,
                    total_amount=total_amount,
                )

            # Попробуем найти ссылку на оплату в DOM
            pay_link = await page.locator("a[href*='pay'], a[href*='tinkoff'], a[href*='sber']").first.get_attribute("href")
            if pay_link:
                return PurchaseResult(
                    success=True,
                    payment_url=pay_link,
                    total_amount=total_amount,
                )

            return PurchaseResult(
                success=False,
                error="Не удалось получить ссылку на оплату. Возможно, форма не отправилась.",
                total_amount=total_amount,
            )

        except Exception as e:
            log.exception("Purchase failed")
            return PurchaseResult(success=False, error=str(e))

        finally:
            await browser.close()
