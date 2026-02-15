import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Person:
    name: str
    promo: str | None = None


@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_id: int = int(os.getenv("ADMIN_ID") or "0")

    # sportvsegda.ru
    base_url: str = "https://sportvsegda.ru"
    stadium_id: int = 2  # Каток Маяк

    # Контакты для заказа
    customer_phone: str = "89032548483"
    customer_name: str = "Katerina"
    customer_email: str = "Katerina.troshina@gmail.com"

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
