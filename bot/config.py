import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _safe_int(val: str | None) -> int:
    try:
        return int(val) if val else 0
    except ValueError:
        return 0


@dataclass
class Person:
    name: str
    promo: str | None = None


@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_id: int = field(default_factory=lambda: _safe_int(os.getenv("ADMIN_ID")))

    # sportvsegda.ru
    base_url: str = "https://sportvsegda.ru"
    stadium_id: int = 2  # Каток Маяк

    # Контакты для заказа
    customer_phone: str = "89032548483"
    customer_name: str = "Katerina"
    customer_email: str = "Katerina.troshina@gmail.com"

    # Карта для оплаты
    card_number: str = "2202206859839834"
    card_expiry: str = "02/34"  # MM/YY

    # Люди и промокоды
    persons: list[Person] = field(default_factory=lambda: [
        Person(name="Тренер", promo="Shalaeva761o"),
        Person(name="Ребёнок 1", promo="676WS52WBP"),
        Person(name="Ребёнок 2", promo="6ENTG3639V"),
        Person(name="Ребёнок 3", promo="676WS52WBP"),
        Person(name="Ребёнок 4", promo="KTLO3B6733"),
        Person(name="Катерина", promo=None),
    ])


config = Config()
