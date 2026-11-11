"""Агрегаты времени ответа и открытий карточек в чате."""

from math import ceil
from time import time

from app.ui.accounts import _db


RETENTION_SECONDS = 30 * 86400


def record_response(turn_id, source, intent, elapsed_ms, card_count):
    now = int(time())
    with _db() as connection:
        connection.execute("DELETE FROM chat_responses WHERE created_at < ?", (now - RETENTION_SECONDS,))
        connection.execute("""INSERT INTO chat_responses (id, created_at, source, intent, elapsed_ms, card_count)
                              VALUES (?, ?, ?, ?, ?, ?)""", (turn_id, now, source, intent, elapsed_ms, card_count))


def record_open(turn_id):
    with _db() as connection:
        connection.execute("UPDATE chat_responses SET opened_at = ? WHERE id = ? AND opened_at IS NULL",
                           (int(time()), turn_id))


def chat_statistics():
    cutoff = int(time()) - RETENTION_SECONDS
    connection = _db()
    row = connection.execute("""SELECT COUNT(*) AS responses,
        COUNT(CASE WHEN intent = 'material_search' THEN 1 END) AS searches,
        COUNT(CASE WHEN source = 'fallback' AND intent = 'material_search' THEN 1 END) AS fallback,
        COUNT(CASE WHEN card_count > 0 THEN 1 END) AS with_cards,
        COUNT(opened_at) AS opened
        FROM chat_responses WHERE created_at >= ?""", (cutoff,)).fetchone()
    stats = dict(row)
    stats["p95_ms"] = None
    if row["responses"]:
        stats["p95_ms"] = connection.execute("""SELECT elapsed_ms FROM chat_responses WHERE created_at >= ?
            ORDER BY elapsed_ms LIMIT 1 OFFSET ?""", (cutoff, ceil(0.95 * row["responses"]) - 1)).fetchone()[0]
    return stats
