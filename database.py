"""
Async SQLite database layer for sticker-to-word indexing.
Each user has their own set of indexed stickers.
Schema v2: adds is_favorite + set_name columns with auto-migration.
"""

from __future__ import annotations

import logging

import aiosqlite

from config import DB_PATH

logger = logging.getLogger("stickerbot.db")


async def get_db() -> aiosqlite.Connection:
    """Return a connection with row_factory enabled."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    """Create tables and run migrations."""
    db = await get_db()
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS stickers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                file_unique_id  TEXT NOT NULL,
                file_id         TEXT NOT NULL,
                emoji           TEXT NOT NULL DEFAULT '',
                is_favorite     INTEGER NOT NULL DEFAULT 1,
                set_name        TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, file_unique_id)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_stickers_user_emoji ON stickers(user_id, emoji)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_stickers_user_set ON stickers(user_id, set_name)"
        )

        # Migration: add columns if missing (for DBs from v1)
        await _migrate_add_column(db, "stickers", "is_favorite", "INTEGER NOT NULL DEFAULT 1")
        await _migrate_add_column(db, "stickers", "set_name", "TEXT NOT NULL DEFAULT ''")

        await db.commit()
    finally:
        await db.close()


async def _migrate_add_column(
    db: aiosqlite.Connection, table: str, column: str, col_def: str
) -> None:
    """Add a column if it doesn't exist (SQLite-safe)."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    columns = {row["name"] async for row in cursor}
    if column not in columns:
        logger.info("Migration: adding %s.%s (%s)", table, column, col_def)
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


# ── CRUD ─────────────────────────────────────────────────────────────

async def add_sticker(
    user_id: int,
    file_unique_id: str,
    file_id: str,
    emoji: str,
    is_favorite: int = 1,
    set_name: str = "",
) -> bool:
    """Insert a sticker. Returns True if new, False if duplicate."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO stickers "
            "(user_id, file_unique_id, file_id, emoji, is_favorite, set_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, file_unique_id, file_id, emoji, is_favorite, set_name),
        )
        await db.commit()
        return db.total_changes > 0
    finally:
        await db.close()


async def add_stickers_bulk(
    entries: list[tuple[int, str, str, str, int, str]]
) -> int:
    """Bulk-insert. Entry: (user_id, file_unique_id, file_id, emoji, is_favorite, set_name)."""
    if not entries:
        return 0
    db = await get_db()
    try:
        await db.executemany(
            "INSERT OR IGNORE INTO stickers "
            "(user_id, file_unique_id, file_id, emoji, is_favorite, set_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            entries,
        )
        await db.commit()
        return db.total_changes
    finally:
        await db.close()


async def get_sticker(user_id: int, file_unique_id: str) -> dict | None:
    """Get a sticker row or None."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM stickers WHERE user_id = ? AND file_unique_id = ?",
            (user_id, file_unique_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def toggle_favorite(user_id: int, file_unique_id: str) -> int | None:
    """Flip is_favorite. Returns new value (0 or 1), None if not found."""
    row = await get_sticker(user_id, file_unique_id)
    if row is None:
        return None
    new_val = 0 if row["is_favorite"] else 1
    db = await get_db()
    try:
        await db.execute(
            "UPDATE stickers SET is_favorite = ? WHERE user_id = ? AND file_unique_id = ?",
            (new_val, user_id, file_unique_id),
        )
        await db.commit()
        return new_val
    finally:
        await db.close()


async def set_favorite(user_id: int, file_unique_id: str, fav: int) -> bool:
    """Set is_favorite to 0 or 1. Returns True if row existed."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE stickers SET is_favorite = ? WHERE user_id = ? AND file_unique_id = ?",
            (fav, user_id, file_unique_id),
        )
        await db.commit()
        return db.total_changes > 0
    finally:
        await db.close()


async def remove_sticker(user_id: int, file_unique_id: str) -> bool:
    """Remove a single sticker."""
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM stickers WHERE user_id = ? AND file_unique_id = ?",
            (user_id, file_unique_id),
        )
        await db.commit()
        return db.total_changes > 0
    finally:
        await db.close()


async def remove_all_user_stickers(user_id: int) -> int:
    """Remove ALL stickers for a user."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM stickers WHERE user_id = ?", (user_id,))
        await db.commit()
        return db.total_changes
    finally:
        await db.close()


# ── Queries ──────────────────────────────────────────────────────────

async def get_user_sticker_count(user_id: int) -> int:
    """Total stickers for user."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM stickers WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0
    finally:
        await db.close()


async def get_user_sets(user_id: int) -> list[dict]:
    """Return [{set_name, total, favorites}] per imported pack."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            SELECT set_name, COUNT(*) as total, SUM(is_favorite) as favorites
            FROM stickers WHERE user_id = ? AND set_name != ''
            GROUP BY set_name ORDER BY set_name
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def search_stickers(
    user_id: int,
    emojis: list[str],
    limit: int = 50,
    favorites_only: bool = True,
) -> list[dict[str, str]]:
    """Find stickers matching any emoji. favorites_only → is_favorite=1 only.
    Returns [{file_id, emoji}]."""
    if not emojis:
        return []
    placeholders = ",".join("?" * len(emojis))
    fav_filter = "AND is_favorite = 1" if favorites_only else ""
    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT file_id, emoji FROM stickers "
            f"WHERE user_id = ? AND emoji IN ({placeholders}) {fav_filter} "
            f"LIMIT ?",
            [user_id, *emojis, limit],
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_pack_stickers(
    user_id: int, set_name: str, limit: int = 120
) -> list[dict]:
    """Get all stickers from a pack."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT file_id, emoji, is_favorite, file_unique_id "
            "FROM stickers WHERE user_id = ? AND set_name = ? "
            "ORDER BY id LIMIT ?",
            (user_id, set_name, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def user_has_stickers_from_set(user_id: int, set_name: str) -> int:
    """How many stickers from a set the user already has."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM stickers WHERE user_id = ? AND set_name = ?",
            (user_id, set_name),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0
    finally:
        await db.close()
