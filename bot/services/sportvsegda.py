import re
import logging
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import aiohttp

BASE_URL = "https://sportvsegda.ru"
MASS_SKATING_TYPE = 1
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

log = logging.getLogger(__name__)


@dataclass
class Session:
    date: str          # "15.02.2026"
    time_range: str    # "19:00 - 20:00"
    session_type: int  # 1=массовое
    available: int     # 0 = неизвестно (из расписания HTML нет данных о местах)


@dataclass
class DateInfo:
    date: str               # "DD.MM.YYYY"
    day_of_week: str        # "понедельник"
    sessions: list[Session]


class SportVsegdaClient:
    """Парсит расписание из HTML страницы sportvsegda.ru.

    AJAX API сайта заблокирован DDoS-защитой (DDos-Guard),
    но расписание встроено в HTML блок #schedule_skate.
    """

    def __init__(self, stadium_id: int = 2):
        self.stadium_id = stadium_id

    async def _fetch_page(self) -> str:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(
                f"{BASE_URL}/mass_skating_tickets/",
                params={"stadium": self.stadium_id},
            ) as resp:
                return await resp.text()

    def _parse_schedule(self, html: str) -> list[DateInfo]:
        """Извлекает расписание из HTML блока schedule_skate для нужного стадиона."""
        # Находим блок нашего стадиона в расписании
        pattern = rf'data-stadium="{self.stadium_id}">\s*'
        stadium_blocks = list(re.finditer(pattern, html))

        # Нам нужен блок внутри skate_sche_main_hidden (расписание), а не из select
        hidden_start = html.find("skate_sche_main_hidden")
        if hidden_start < 0:
            log.warning("Schedule block not found in HTML")
            return []

        block_start = None
        for m in stadium_blocks:
            if m.start() > hidden_start:
                block_start = m.start()
                break

        if block_start is None:
            log.warning("Stadium %d block not found in schedule", self.stadium_id)
            return []

        # Конец блока — следующий data-stadium или конец контейнера
        next_stadium = re.search(
            r'data-stadium="\d+"', html[block_start + 50:]
        )
        block_end = (
            block_start + 50 + next_stadium.start()
            if next_stadium
            else len(html)
        )
        block = html[block_start:block_end]

        # Парсим каждый день
        dates: list[DateInfo] = []
        day_blocks = re.split(r'skate_sche_item', block)

        for day_block in day_blocks:
            date_match = re.search(r'skate_sche_head">([^<]+)', day_block)
            dow_match = re.search(r'skate_sche_data">([^<]+)', day_block)
            if not date_match:
                continue

            date_short = date_match.group(1).strip()  # "15.02"
            day_of_week = dow_match.group(1).strip() if dow_match else ""

            # Все ссылки на сеансы
            links = re.findall(
                r'href="(/mass_skating_tickets/\?[^"]+)"[^>]*data-type="(\d+)"',
                day_block,
            )
            # Fallback: парсим ссылки без data-type, берём type из URL
            if not links:
                links_raw = re.findall(
                    r'href="(/mass_skating_tickets/\?stadium=\d+[^"]+)"',
                    day_block,
                )
                links = []
                for url in links_raw:
                    qs = parse_qs(urlparse(url).query)
                    stype = int(qs.get("type", [0])[0])
                    links.append((url, str(stype)))

            sessions = []
            for url, stype_str in links:
                stype = int(stype_str)
                if stype != MASS_SKATING_TYPE:
                    continue

                qs = parse_qs(urlparse(url).query)
                full_date = qs.get("date", [""])[0]  # "15.02.2026"
                time_range = qs.get("time", [""])[0]  # "19:00 - 20:00"

                sessions.append(Session(
                    date=full_date,
                    time_range=time_range,
                    session_type=stype,
                    available=0,
                ))

            if sessions:
                dates.append(DateInfo(
                    date=sessions[0].date,
                    day_of_week=day_of_week,
                    sessions=sessions,
                ))

        return dates

    async def get_schedule(self) -> list[DateInfo]:
        html = await self._fetch_page()
        return self._parse_schedule(html)

    async def check_promo(self, promo_code: str) -> dict:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(
                f"{BASE_URL}/assets/ajax/check_promo.php",
                params={"promo": promo_code},
            ) as resp:
                return await resp.json(content_type=None)

    async def reserve_ticket(
        self,
        date: str,
        time_range: str,
        promo: str | None,
        tickets: list[dict],
    ) -> dict:
        payload = {
            "stadium": self.stadium_id,
            "date": date,
            "range": time_range,
            "promo": promo or "",
        }
        for i, ticket in enumerate(tickets):
            payload[f"tickets[{i}][id]"] = ticket["id"]
            payload[f"tickets[{i}][count]"] = ticket["count"]

        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.post(
                f"{BASE_URL}/assets/ajax/tickets_reserve.php",
                data=payload,
            ) as resp:
                return await resp.json(content_type=None)
