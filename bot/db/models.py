import aiosqlite
import os

DB_PATH = os.getenv("DB_PATH", "tickets.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                time_range TEXT NOT NULL,
                person_name TEXT NOT NULL,
                promo TEXT,
                status TEXT DEFAULT 'reserved',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def save_order(
    user_id: int,
    date: str,
    time_range: str,
    person_name: str,
    promo: str | None,
    status: str = "reserved",
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO orders (user_id, date, time_range, person_name, promo, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, date, time_range, person_name, promo, status),
        )
        await db.commit()


async def get_orders(user_id: int, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
