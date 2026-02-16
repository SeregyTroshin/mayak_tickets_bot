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
    page = None

    try:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        # Stealth: скрыть headless-признаки от DDoS-Guard
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => false});
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['ru-RU', 'ru', 'en-US', 'en']
            });
            window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
            const origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) =>
                parameters.name === 'notifications'
                    ? Promise.resolve({state: Notification.permission})
                    : origQuery(parameters);
        """)
        page = await context.new_page()

        log.info("Browser launched (stealth), loading page...")
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        log.info("DOM loaded, waiting for orderForm...")
        await page.wait_for_selector("#orderForm", timeout=30_000)
        log.info("orderForm found")

        # Дать странице время обработать JS (DDoS-Guard challenge + AJAX)
        await page.wait_for_timeout(5000)
        log.info("Filling form via JS evaluate...")

        # ── Заполняем форму целиком через page.evaluate() ──
        # Все действия в одном evaluate — не зависает на ожидании видимости.
        # Кликаем "+" через JS element.click() — триггерит обработчики сайта.

        js_result = await page.evaluate("""async (args) => {
            const {timeRange, promo, name, phone, email} = args;
            const log = [];

            try {
                // 1. Выбрать нужный сеанс в dropdown
                const sel = document.querySelector('#order_range');
                if (sel) {
                    let found = false;
                    for (const opt of sel.options) {
                        if (opt.value === timeRange) {
                            opt.selected = true;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            found = true;
                            log.push('Range: ' + opt.value);
                            break;
                        }
                    }
                    if (!found) {
                        if (sel.options.length > 0) {
                            sel.options[0].selected = true;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            log.push('Range fallback: ' + sel.options[0].value);
                        }
                    }
                }
                await new Promise(r => setTimeout(r, 500));

                // 2. Промокод (ДО выбора количества)
                if (promo) {
                    const pi = document.querySelector('input[name="f_Promo"]');
                    if (pi) {
                        pi.value = promo;
                        pi.dispatchEvent(new Event('input', {bubbles: true}));
                        log.push('Promo set: ' + promo);
                    }
                    const btn = document.querySelector('.apply_promo');
                    if (btn) {
                        btn.click();
                        log.push('Promo apply clicked');
                        await new Promise(r => setTimeout(r, 2000));
                    }
                }

                // 3. Контактные данные
                const setField = (fname, val) => {
                    const el = document.querySelector('input[name="' + fname + '"]');
                    if (el) {
                        el.value = val;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                    }
                };
                setField('f_Name', name);
                setField('f_Phone', phone);
                setField('f_Email', email);
                log.push('Contacts filled');

                // 4. Оплата картой
                const radio = document.querySelector('#payment_2');
                if (radio) {
                    radio.checked = true;
                    radio.dispatchEvent(new Event('change', {bubbles: true}));
                    log.push('Card selected');
                }

                // 5. Чекбоксы
                document.querySelectorAll('input[type=checkbox]').forEach(cb => {
                    if (!cb.checked) {
                        cb.checked = true;
                        cb.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                });
                log.push('Checkboxes done');

                // 6. Кликаем "+" ПОСЛЕДНИМ — чтобы ничего не сбросило сумму
                const plusBtn = document.querySelector('.chek_ticket_item .number .plus');
                if (plusBtn) {
                    plusBtn.click();
                    log.push('Plus clicked');
                } else {
                    const inp = document.querySelector('.chek_ticket_item input');
                    if (inp) {
                        inp.value = '1';
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        log.push('Direct input=1');
                    } else {
                        return {error: 'No plus btn and no input', log};
                    }
                }

                await new Promise(r => setTimeout(r, 1000));

                // 7. Читаем сумму
                const totalEl = document.querySelector('.summ_itog');
                let total = totalEl ? totalEl.textContent.trim() : '?';
                log.push('Total: ' + total);

                // Fallback: если сумма 0, берём цену из карточки билета
                if (total === '0 ₽' || total === '0') {
                    const priceEl = document.querySelector('.chek_ticket_item .chek_ticket_price');
                    if (priceEl) {
                        total = priceEl.textContent.trim();
                        log.push('Fallback price from card: ' + total);
                    }
                }

                const inp = document.querySelector('.chek_ticket_item input');
                log.push('Input value: ' + (inp ? inp.value : 'N/A'));

                return {success: true, total, log};

            } catch (e) {
                return {error: String(e), log};
            }
        }""", {
            "timeRange": time_range,
            "promo": promo or "",
            "name": name,
            "phone": phone,
            "email": email,
        })

        js_log = js_result.get("log", [])
        log.info("JS fill result: %s", js_log)

        if not js_result.get("success"):
            error = js_result.get("error", "Неизвестная ошибка JS")
            html_snip = js_result.get("html", "")
            log.error("JS fill failed: %s | HTML: %s", error, html_snip)
            raise Exception(error)

        total_amount = js_result.get("total")
        log.info("Total: %s", total_amount)

        session = PurchaseSession(pw, browser, page, total_amount, user_id)
        _sessions[user_id] = session

        return PurchaseResult(success=True, total_amount=total_amount)

    except Exception as e:
        log.exception("prepare_purchase failed: %s", e)
        if page:
            try:
                body = await page.evaluate(
                    "document.body ? document.body.innerHTML.substring(0, 3000) : 'no body'"
                )
                log.error("PAGE HTML: %s", body)
            except Exception:
                pass
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
