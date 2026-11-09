"""Подписанные позиции рекомендаций для учёта показов и переходов."""

import secrets
import sqlite3
from collections.abc import Sequence

from flask import current_app, session
from itsdangerous import BadData, URLSafeTimedSerializer

from app.ui.accounts import save_recommendation_impressions

IMPRESSION_REQUEST_LIMIT = 16
TICKET_MAX_AGE = 24 * 60 * 60


def _signer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt=f"recommendations-{salt}")


def _read_ticket(signer: URLSafeTimedSerializer, ticket: str | None, size: int) -> list | None:
    if not ticket or len(ticket) > 1024:
        return None
    try:
        data = signer.loads(ticket, max_age=TICKET_MAX_AGE)
    except BadData:
        return None
    return data if isinstance(data, list) and len(data) == size else None


def issue_impression_tickets(
    material_ids: Sequence[str], algorithm: str, result_order: str, previous_batch: str | None,
) -> tuple[str, tuple[str, ...]]:
    if not material_ids:
        return "", ()
    if "recommendation_visit" not in session:
        session["recommendation_visit"] = secrets.token_urlsafe(16)
    scope = [session["recommendation_visit"], algorithm, result_order]
    batch_signer = _signer("batch")
    previous = _read_ticket(batch_signer, previous_batch, 4)
    batch_id = previous[3] if previous and previous[:3] == scope else secrets.token_urlsafe(16)
    card_signer = _signer("card")
    tickets = tuple(
        card_signer.dumps([scope[0], batch_id, material_id, position, algorithm, result_order])
        for position, material_id in enumerate(material_ids, start=1)
    )
    return batch_signer.dumps([*scope, batch_id]), tickets


def record_impressions(
    tickets: Sequence[str | None], account: sqlite3.Row | None, *, clicked_material: str | None = None,
) -> None:
    visit = session.get("recommendation_visit")
    if not visit:
        return
    signer = _signer("card")
    placements = []
    for ticket in tickets[:IMPRESSION_REQUEST_LIMIT]:
        data = _read_ticket(signer, ticket, 6)
        if data is None or data[0] != visit:
            continue
        _, batch_id, material_id, position, algorithm, result_order = data
        if clicked_material is None or clicked_material == material_id:
            placements.append((batch_id, material_id, position, algorithm, result_order))
    if placements:
        save_recommendation_impressions(account, placements, clicked=clicked_material is not None)
