"""Двухфазная автоматизация покупки билетов через Playwright.

Фаза 1 — prepare_purchase:
  Открыть страницу → 1 билет → промокод → контакты → оплата картой →
  галочки → прочитать сумму → вернуть сумму, держать браузер открытым.

Фаза 2 — confirm_and_pay:
  Нажать «Оплатить» → перейти на страницу банка →
  заполнить номер карты, срок, CVV → отправить → проверить результат.

cancel_purchase — закрыть браузер, очистить сессию.
"""

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import quote

log = logging.getLogger(__name__)

BASE_URL = "https://sportvsegda.ru"
SESSION_TTL = 300  # 5 минут до автоочистки

# Активные сессии покупки: user_id → PurchaseSession
_sessions: dict[int, "PurchaseSession"] = {}


@dataclass
class PurchaseResult:
    success: bool
    payment_url: str | None = None
    error: str | None = None
    total_amount: str | None = None


class PurchaseSession:
    """Хранит состояние браузера между фазами prepare и confirm."""

    def __init__(self, pw, browser, page, total_amount: str | None):
        self.pw = pw
        self.browser = browser
        self.page = page
        self.total_amount = total_amount
        self._cleanup_task: asyncio.Task | None = asyncio.create_task(
            self._auto_cleanup()
        )

    async def _auto_cleanup(self):
        await asyncio.sleep(SESSION_TTL)
        log.info("Auto-cleaning purchase session")
        _sessions.pop(id(self), None)  # remove if still there
        await self.close()

    async def close(self):
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        try:
            await self.browser.close()
        except Exception:
            pass
        try:
            await self.pw.stop()
        except Exception:
            pass


# ─── Phase 1 ────────────────────────────────────────────────────────────────


async def prepare_purchase(
    user_id: int,
    stadium_id: int,
    date: str,
    time_range: str,
    promo: str | None,
    name: str,
    phone: str,
    email: str,
) -> PurchaseResult:
    """Заполнить форму, прочитать сумму.  Браузер остаётся открытым."""

    # Закрыть предыдущую сессию, если есть
    await cancel_purchase(user_id)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return PurchaseResult(success=False, error="Playwright не установлен")

    url = (
        f"{BASE_URL}/mass_skating_tickets/"
        f"?stadium={stadium_id}&type=1"
        f"&date={quote(date)}&time={quote(time_range)}"
    )

    log.info("prepare_purchase: %s %s promo=%s", date, time_range, promo)

    pw = await async_playwright().start()
    browser = None

    try:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        )

        # 1. Загрузить страницу
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        await page.wait_for_selector("#orderForm", timeout=10_000)
        log.info("Page loaded: %s", url)

        # 2. 1 билет
        ticket_input = await page.wait_for_selector(
            ".chek_ticket_item input[type=text]", timeout=15_000
        )
        if ticket_input:
            await ticket_input.fill("1")
            await ticket_input.dispatch_event("change")
            log.info("Ticket qty = 1")

        # 3. Промокод
        if promo:
            await page.locator("#promocode").fill(promo)
            apply_btn = page.locator(".apply_promo")
            if await apply_btn.count() > 0:
                await apply_btn.click()
                await page.wait_for_timeout(2500)
            log.info("Promo applied: %s", promo)

        # 4. Контактные данные
        await page.locator('input[name="f_Name"]').fill(name)
        await page.locator('input[name="f_Phone"]').fill(phone)
        await page.locator('input[name="f_Email"]').fill(email)
        log.info("Contact info filled")

        # 5. Оплата картой (value=2)
        await page.locator("#payment_2").check()
        log.info("Card payment selected")

        # 6. Три галочки
        await page.locator("#order_agree_oferta").check()
        await page.locator("#order_agree_policy").check()
        await page.locator("#order_agree_personal").check()
        log.info("Agreements checked")

        # 7. Считать итоговую сумму (подождать пересчёт JS)
        await page.wait_for_timeout(1500)
        total_amount = None
        total_el = page.locator(".summ_itog")
        if await total_el.count() > 0:
            total_amount = (await total_el.text_content() or "").strip()
        log.info("Total: %s", total_amount)

        # Сохранить сессию
        session = PurchaseSession(pw, browser, page, total_amount)
        _sessions[user_id] = session

        return PurchaseResult(success=True, total_amount=total_amount)

    except Exception as e:
        log.exception("prepare_purchase failed")
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await pw.stop()
        except Exception:
            pass
        return PurchaseResult(success=False, error=str(e))


# ─── Phase 2 ────────────────────────────────────────────────────────────────


async def confirm_and_pay(
    user_id: int,
    card_number: str,
    card_expiry: str,
    card_cvv: str,
) -> PurchaseResult:
    """Нажать «Оплатить», перейти на страницу банка, заполнить карту."""

    session = _sessions.pop(user_id, None)
    if not session:
        return PurchaseResult(
            success=False, error="Сессия истекла. Выберите сеанс заново."
        )

    page = session.page
    total = session.total_amount

    try:
        # ── Отправить форму на sportvsegda.ru ──
        original_url = page.url
        submit = page.locator(
            'button[type="submit"], input[type="submit"], .order_submit'
        )
        await submit.first.click()
        log.info("Submit clicked")

        # Ждём перехода на другой URL (прямой redirect или JS)
        for _ in range(60):  # 30 сек
            await page.wait_for_timeout(500)
            if page.url != original_url:
                break

        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        bank_url = page.url
        log.info("After submit: %s", bank_url)

        # Если остались на sportvsegda.ru — форма могла вернуть ошибку
        if "sportvsegda.ru" in bank_url:
            # Попробуем найти сообщение об ошибке на странице
            content = await page.content()
            if "ошибк" in content.lower() or "error" in content.lower():
                return PurchaseResult(
                    success=False,
                    error="Сайт вернул ошибку при оформлении заказа.",
                    total_amount=total,
                )
            # Возможно, ещё перенаправляется
            await page.wait_for_timeout(5000)
            bank_url = page.url
            if "sportvsegda.ru" in bank_url:
                return PurchaseResult(
                    success=False,
                    error="Не удалось перейти на страницу банка.",
                    total_amount=total,
                )

        # ── Заполнить карту на странице банка ──
        log.info("Bank page: %s", bank_url)
        await page.wait_for_timeout(3000)  # дать банку загрузить JS

        filled = await _fill_card(page, card_number, card_expiry, card_cvv)
        if not filled:
            # Не удалось заполнить — вернуть ссылку для ручной оплаты
            return PurchaseResult(
                success=False,
                payment_url=bank_url,
                error="Не удалось заполнить карту автоматически. Откройте ссылку.",
                total_amount=total,
            )

        # ── Нажать «Оплатить» на банковской странице ──
        await _submit_payment(page)
        log.info("Payment submitted")

        # Ждём результата
        for _ in range(40):  # 20 сек
            await page.wait_for_timeout(500)
            current = page.url
            body = await page.content()
            low = (current + body).lower()

            # Успех
            if any(w in low for w in ("success", "ok", "paid", "успешно", "оплачен")):
                log.info("Payment SUCCESS")
                return PurchaseResult(success=True, total_amount=total)

            # 3D-Secure — нужен SMS-код, автоматизировать нельзя
            if any(
                w in low for w in ("3ds", "3-d secure", "введите код", "sms")
            ):
                log.info("3DS detected, returning URL")
                return PurchaseResult(
                    success=False,
                    payment_url=current,
                    error="Требуется 3D-Secure (SMS). Откройте ссылку и введите код.",
                    total_amount=total,
                )

        # Не дождались явного результата
        final_url = page.url
        return PurchaseResult(
            success=False,
            payment_url=final_url,
            error="Статус оплаты неизвестен. Проверьте по ссылке.",
            total_amount=total,
        )

    except Exception as e:
        log.exception("confirm_and_pay failed")
        return PurchaseResult(success=False, error=str(e), total_amount=total)
    finally:
        await session.close()


# ─── Cancel ──────────────────────────────────────────────────────────────────


async def cancel_purchase(user_id: int) -> None:
    session = _sessions.pop(user_id, None)
    if session:
        await session.close()
        log.info("Purchase session cancelled for user %d", user_id)


# ─── Helpers: заполнение карты ───────────────────────────────────────────────

_CARD_SELECTORS = [
    'input[name="pan"]',
    'input[autocomplete="cc-number"]',
    'input[name*="cardNumber"]',
    'input[name*="card_number"]',
    'input[name*="card-number"]',
    'input[placeholder*="Номер карты"]',
    'input[placeholder*="Card number"]',
    'input[data-field="pan"]',
    "#pan",
    ".card-number-input input",
]

_EXPIRY_SELECTORS = [
    'input[name="expiry"]',
    'input[name="exp"]',
    'input[name*="expDate"]',
    'input[name*="expire"]',
    'input[autocomplete="cc-exp"]',
    'input[placeholder*="MM"]',
    'input[placeholder*="ММ"]',
    'input[data-field="expiry"]',
]

_CVV_SELECTORS = [
    'input[name="cvv"]',
    'input[name="cvc"]',
    'input[name*="cvv"]',
    'input[name*="cvc"]',
    'input[autocomplete="cc-csc"]',
    'input[placeholder*="CVV"]',
    'input[placeholder*="CVC"]',
    'input[data-field="cvv"]',
    "#cvv",
    "#cvc",
]


async def _try_fill(target, selectors: list[str], value: str) -> bool:
    """Попробовать заполнить поле по списку селекторов."""
    for sel in selectors:
        try:
            el = target.locator(sel)
            if await el.count() > 0:
                await el.first.click()
                await el.first.type(value, delay=50)
                return True
        except Exception:
            continue
    return False


async def _fill_card(page, card_number: str, card_expiry: str, card_cvv: str) -> bool:
    """Заполнить карту на странице банка. Поддержка нескольких шлюзов."""

    # Проверить, есть ли iframe с платёжной формой
    target = page
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            count = await frame.locator("input").count()
            if count >= 2:
                target = frame
                log.info("Using iframe for card input")
                break
        except Exception:
            continue

    # Номер карты
    card_ok = await _try_fill(target, _CARD_SELECTORS, card_number)
    if not card_ok:
        log.warning("Card number field not found")
        return False

    await page.wait_for_timeout(500)

    # Срок действия — сначала единое поле, потом раздельные month/year
    expiry_ok = await _try_fill(target, _EXPIRY_SELECTORS, card_expiry.replace("/", ""))

    if not expiry_ok:
        parts = card_expiry.split("/")
        if len(parts) == 2:
            month, year = parts
            for m_sel, y_sel in [
                ('input[name="month"]', 'input[name="year"]'),
                ('input[name*="month"]', 'input[name*="year"]'),
            ]:
                try:
                    m_el = target.locator(m_sel)
                    y_el = target.locator(y_sel)
                    if await m_el.count() > 0 and await y_el.count() > 0:
                        await m_el.first.type(month, delay=50)
                        await y_el.first.type(year, delay=50)
                        expiry_ok = True
                        break
                except Exception:
                    continue

    await page.wait_for_timeout(500)

    # CVV
    cvv_ok = await _try_fill(target, _CVV_SELECTORS, card_cvv)

    log.info(
        "Card fill: number=%s expiry=%s cvv=%s", card_ok, expiry_ok, cvv_ok
    )
    return card_ok and cvv_ok


async def _submit_payment(page) -> None:
    """Нажать кнопку оплаты на банковской странице."""
    for sel in [
        'button[type="submit"]',
        'input[type="submit"]',
        "button:has-text('Оплатить')",
        "button:has-text('Подтвердить')",
        "button:has-text('Pay')",
        "button:has-text('Далее')",
        ".payment-submit",
        "#submit-button",
        "#payButton",
    ]:
        try:
            el = page.locator(sel)
            if await el.count() > 0:
                await el.first.click()
                log.info("Clicked payment submit: %s", sel)
                return
        except Exception:
            continue
    log.warning("Payment submit button not found")
