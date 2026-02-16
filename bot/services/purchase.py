"""Трёхфазная автоматизация покупки билетов через Playwright.

Фаза 1 — prepare_purchase:
  Открыть страницу → 1 билет → промокод → контакты → оплата картой →
  галочки → прочитать сумму.  Браузер остаётся открытым.

Фаза 2 — confirm_and_pay:
  Нажать «Оплатить» → перейти на страницу банка →
  заполнить карту → отправить.
  Если 3D-Secure — браузер остаётся открытым, needs_sms=True.

Фаза 3 — complete_3ds:
  Ввести SMS-код на странице 3D-Secure → дождаться результата.

cancel_purchase — закрыть браузер, очистить сессию.
"""

import asyncio
import logging
from dataclasses import dataclass, field
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
    needs_sms: bool = False


class PurchaseSession:
    """Хранит состояние браузера между фазами."""

    def __init__(self, pw, browser, page, total_amount: str | None, user_id: int):
        self.pw = pw
        self.browser = browser
        self.page = page
        self.total_amount = total_amount
        self.user_id = user_id
        self._cleanup_task: asyncio.Task | None = asyncio.create_task(
            self._auto_cleanup()
        )

    async def _auto_cleanup(self):
        await asyncio.sleep(SESSION_TTL)
        log.info("Auto-cleaning purchase session for user %d", self.user_id)
        _sessions.pop(self.user_id, None)
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


# ─── Phase 1: подготовка ────────────────────────────────────────────────────


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
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        )

        log.info("Browser launched, loading page...")
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        log.info("DOM loaded, waiting for form...")
        await page.wait_for_selector("#orderForm", timeout=20_000)
        log.info("Page loaded: %s", url)

        # 1 билет
        ticket_input = await page.wait_for_selector(
            ".chek_ticket_item input[type=text]", timeout=15_000
        )
        if ticket_input:
            await ticket_input.fill("1")
            await ticket_input.dispatch_event("change")
            log.info("Ticket qty = 1")

        # Промокод
        if promo:
            await page.locator("#promocode").fill(promo)
            apply_btn = page.locator(".apply_promo")
            if await apply_btn.count() > 0:
                await apply_btn.click()
                await page.wait_for_timeout(2500)
            log.info("Promo applied: %s", promo)

        # Контактные данные
        await page.locator('input[name="f_Name"]').fill(name)
        await page.locator('input[name="f_Phone"]').fill(phone)
        await page.locator('input[name="f_Email"]').fill(email)
        log.info("Contact info filled")

        # Оплата картой
        await page.locator("#payment_2").check()
        log.info("Card payment selected")

        # Три галочки
        await page.locator("#order_agree_oferta").check()
        await page.locator("#order_agree_policy").check()
        await page.locator("#order_agree_personal").check()
        log.info("Agreements checked")

        # Считать сумму
        await page.wait_for_timeout(1500)
        total_amount = None
        total_el = page.locator(".summ_itog")
        if await total_el.count() > 0:
            total_amount = (await total_el.text_content() or "").strip()
        log.info("Total: %s", total_amount)

        session = PurchaseSession(pw, browser, page, total_amount, user_id)
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


# ─── Phase 2: оплата картой ─────────────────────────────────────────────────


async def confirm_and_pay(
    user_id: int,
    card_number: str,
    card_expiry: str,
    card_cvv: str,
) -> PurchaseResult:
    """Нажать «Оплатить», заполнить карту на странице банка.

    Если 3D-Secure — браузер остаётся открытым, needs_sms=True.
    """

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

        # Ждём перехода на другой URL
        for _ in range(60):
            await page.wait_for_timeout(500)
            if page.url != original_url:
                break

        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        bank_url = page.url
        log.info("After submit: %s", bank_url)

        # Если остались на sportvsegda.ru — ошибка
        if "sportvsegda.ru" in bank_url:
            content = await page.content()
            if "ошибк" in content.lower() or "error" in content.lower():
                return PurchaseResult(
                    success=False,
                    error="Сайт вернул ошибку при оформлении заказа.",
                    total_amount=total,
                )
            await page.wait_for_timeout(5000)
            bank_url = page.url
            if "sportvsegda.ru" in bank_url:
                return PurchaseResult(
                    success=False,
                    error="Не удалось перейти на страницу банка.",
                    total_amount=total,
                )

        # ── Заполнить карту ──
        log.info("Bank page: %s", bank_url)
        await page.wait_for_timeout(3000)

        filled = await _fill_card(page, card_number, card_expiry, card_cvv)
        if not filled:
            return PurchaseResult(
                success=False,
                payment_url=bank_url,
                error="Не удалось заполнить карту автоматически. Откройте ссылку.",
                total_amount=total,
            )

        # ── Нажать «Оплатить» ──
        await _submit_payment(page)
        log.info("Payment submitted on bank page")

        # ── Ждём результат: успех, 3DS, или ошибка ──
        for _ in range(40):  # 20 сек
            await page.wait_for_timeout(500)
            current = page.url
            body = await page.content()
            low = (current + body).lower()

            # Успех
            if any(w in low for w in ("success", "paid", "успешно", "оплачен")):
                log.info("Payment SUCCESS")
                return PurchaseResult(success=True, total_amount=total)

            # 3D-Secure — держим браузер, просим SMS
            if _is_3ds_page(low):
                log.info("3DS detected — keeping browser for SMS")
                _sessions[user_id] = session
                return PurchaseResult(
                    success=False,
                    total_amount=total,
                    needs_sms=True,
                    error="Банк запросил код из SMS для подтверждения.",
                )

        # Таймаут
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
        # Закрыть браузер, ТОЛЬКО если сессия не была сохранена обратно (3DS)
        if user_id not in _sessions:
            await session.close()


# ─── Phase 3: ввод SMS-кода 3D-Secure ───────────────────────────────────────


async def complete_3ds(user_id: int, sms_code: str) -> PurchaseResult:
    """Ввести SMS-код на странице 3D-Secure и дождаться результата."""

    session = _sessions.pop(user_id, None)
    if not session:
        return PurchaseResult(
            success=False, error="Сессия 3DS истекла. Начните покупку заново."
        )

    page = session.page
    total = session.total_amount

    try:
        log.info("Entering 3DS code for user %d", user_id)

        # Найти поле для SMS-кода
        code_filled = await _try_fill(page, _SMS_CODE_SELECTORS, sms_code)

        if not code_filled:
            # Проверить iframe
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                try:
                    code_filled = await _try_fill(frame, _SMS_CODE_SELECTORS, sms_code)
                    if code_filled:
                        break
                except Exception:
                    continue

        if not code_filled:
            log.warning("SMS code field not found")
            return PurchaseResult(
                success=False,
                payment_url=page.url,
                error="Не нашёл поле для SMS-кода. Откройте ссылку.",
                total_amount=total,
            )

        log.info("SMS code entered")

        # Нажать кнопку подтверждения
        await _submit_3ds(page)
        log.info("3DS submit clicked")

        # Ждём результат
        for _ in range(60):  # 30 сек
            await page.wait_for_timeout(500)
            current = page.url
            body = await page.content()
            low = (current + body).lower()

            # Успех
            if any(w in low for w in ("success", "paid", "успешно", "оплачен")):
                log.info("3DS payment SUCCESS")
                return PurchaseResult(success=True, total_amount=total)

            # Неверный код
            if any(w in low for w in ("неверный", "incorrect", "invalid", "wrong", "повторите")):
                log.info("Wrong SMS code")
                # Оставить сессию для повторной попытки
                _sessions[user_id] = session
                return PurchaseResult(
                    success=False,
                    needs_sms=True,
                    error="Неверный код. Попробуйте ещё раз.",
                    total_amount=total,
                )

            # Ошибка
            if any(w in low for w in ("отклонен", "declined", "fail", "ошибк")):
                log.info("Payment declined after 3DS")
                return PurchaseResult(
                    success=False,
                    error="Платёж отклонён банком.",
                    total_amount=total,
                )

        # Таймаут — проверим ещё раз
        final_url = page.url
        final_body = await page.content()
        if any(w in (final_url + final_body).lower() for w in ("success", "успешно", "оплачен")):
            return PurchaseResult(success=True, total_amount=total)

        return PurchaseResult(
            success=False,
            payment_url=final_url,
            error="Не удалось определить результат 3DS. Проверьте по ссылке.",
            total_amount=total,
        )

    except Exception as e:
        log.exception("complete_3ds failed")
        return PurchaseResult(success=False, error=str(e), total_amount=total)
    finally:
        if user_id not in _sessions:
            await session.close()


# ─── Cancel ──────────────────────────────────────────────────────────────────


async def cancel_purchase(user_id: int) -> None:
    session = _sessions.pop(user_id, None)
    if session:
        await session.close()
        log.info("Purchase session cancelled for user %d", user_id)


# ─── Helpers ─────────────────────────────────────────────────────────────────

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

_SMS_CODE_SELECTORS = [
    'input[name="password"]',
    'input[autocomplete="one-time-code"]',
    'input[name*="code"]',
    'input[name*="otp"]',
    'input[name*="sms"]',
    'input[placeholder*="Код"]',
    'input[placeholder*="код"]',
    'input[placeholder*="SMS"]',
    'input[placeholder*="Code"]',
    'input[type="tel"]',
    'input[type="password"]',
    'input[data-field="code"]',
    "#otp",
    "#code",
    "#smsCode",
]


def _is_3ds_page(text_lower: str) -> bool:
    """Определить, находимся ли мы на странице 3D-Secure."""
    indicators = [
        "3-d secure",
        "3ds",
        "3d secure",
        "введите код",
        "enter code",
        "одноразовый пароль",
        "sms-код",
        "код подтверждения",
        "confirmation code",
        "secure code",
        "код из сообщения",
    ]
    return any(ind in text_lower for ind in indicators)


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
    """Заполнить карту на странице банка."""

    # Проверить iframe
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

    card_ok = await _try_fill(target, _CARD_SELECTORS, card_number)
    if not card_ok:
        log.warning("Card number field not found")
        return False

    await page.wait_for_timeout(500)

    # Срок — единое поле или раздельные month/year
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

    cvv_ok = await _try_fill(target, _CVV_SELECTORS, card_cvv)

    log.info("Card fill: number=%s expiry=%s cvv=%s", card_ok, expiry_ok, cvv_ok)
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


async def _submit_3ds(page) -> None:
    """Нажать кнопку подтверждения на странице 3D-Secure."""
    for sel in [
        'input[type="submit"]',
        'button[type="submit"]',
        "button:has-text('Подтвердить')",
        "button:has-text('Отправить')",
        "button:has-text('Submit')",
        "button:has-text('Confirm')",
        "button:has-text('OK')",
        "#buttonSubmit",
        ".submit",
    ]:
        try:
            el = page.locator(sel)
            if await el.count() > 0:
                await el.first.click()
                log.info("Clicked 3DS submit: %s", sel)
                return
        except Exception:
            continue

    # Fallback: попробовать во фреймах
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        for sel in ['input[type="submit"]', 'button[type="submit"]']:
            try:
                el = frame.locator(sel)
                if await el.count() > 0:
                    await el.first.click()
                    log.info("Clicked 3DS submit in iframe: %s", sel)
                    return
            except Exception:
                continue
    log.warning("3DS submit button not found")
