"""Группы экспериментов, атрибуция и метрики веб-демо."""

import re
import secrets
from dataclasses import dataclass
from hashlib import sha256
from sqlite3 import Row
from time import time

from flask import Response, current_app, g, request
from itsdangerous import BadData, URLSafeTimedSerializer

from app.ui.accounts import DEMO_USERNAME, _db, current_account

VISITOR_COOKIE = "alfahack_ab_visitor"
COOKIE_AGE = 30 * 86400
DAY = 86400
ACTIVITY_ENDPOINTS = {"index", "material_page", "profile_page", "profile_settings", "accounts.login", "accounts.register"}
AUTOMATION = re.compile(r"bot\b|crawler|spider|preview|headless", re.IGNORECASE)


@dataclass(frozen=True)
class Variant:
    experiment_id: int
    subject: str
    mode: str
    bm25_weight: float


@dataclass(frozen=True)
class GroupMetrics:
    assigned: int = 0
    participants: int = 0
    impressions: int = 0
    clicks: int = 0
    previews: int = 0
    d7_ready: int = 0
    d7_returned: int = 0


def running_experiment() -> Row | None:
    return _db().execute("SELECT * FROM ab_experiments WHERE ended_at IS NULL").fetchone()


def experiments() -> tuple[Row, ...]:
    return tuple(_db().execute("SELECT * FROM ab_experiments ORDER BY id DESC"))


def start_experiment(mode_a: str, mode_b: str, share_a: int, traffic: int, weight: float) -> bool:
    with _db() as connection:
        cursor = connection.execute(
            """INSERT INTO ab_experiments (mode_a, mode_b, share_a, traffic, bm25_weight, seed, started_at)
               SELECT ?, ?, ?, ?, ?, ?, ?
               WHERE NOT EXISTS (SELECT 1 FROM ab_experiments WHERE ended_at IS NULL)""",
            (mode_a, mode_b, share_a, traffic, weight, secrets.token_hex(16), int(time())),
        )
        return cursor.rowcount == 1


def end_experiment(experiment_id: int) -> bool:
    with _db() as connection:
        cursor = connection.execute(
            "UPDATE ab_experiments SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
            (int(time()), experiment_id),
        )
        return cursor.rowcount == 1


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt="ab-visitor")


def _visitor() -> str | None:
    if "ab_visitor" not in g:
        token = request.cookies.get(VISITOR_COOKIE, "")
        g.ab_visitor = None
        if 0 < len(token) <= 128:
            try:
                subject = _signer().loads(token, max_age=COOKIE_AGE)
            except BadData:
                pass
            else:
                if isinstance(subject, str) and re.fullmatch(r"g:[a-f0-9]{32}", subject):
                    g.ab_visitor = subject
    return g.ab_visitor


def _new_visitor() -> str:
    g.ab_visitor = "g:" + secrets.token_hex(16)
    g.ab_visitor_changed = True
    return g.ab_visitor


def excluded_request(account: Row | None) -> bool:
    if account is not None and (account["username"] == DEMO_USERNAME or account["is_admin"]):
        return True
    if AUTOMATION.search(request.headers.get("User-Agent", "")):
        return True
    return any("prefetch" in request.headers.get(header, "").lower() for header in ("Sec-Purpose", "Purpose"))


def attach_guest(user_id: int) -> None:
    subject = _visitor()
    if subject is None:
        return
    with _db() as connection:
        connection.execute(
            """UPDATE ab_enrollments AS guest SET user_id = ?
               WHERE guest.subject = ? AND guest.user_id IS NULL
                 AND NOT EXISTS (
                    SELECT 1 FROM ab_enrollments WHERE experiment_id = guest.experiment_id AND user_id = ?
                 )""",
            (user_id, subject, user_id),
        )


def _arm(experiment: Row, subject: str) -> str:
    draw = sha256(f"{experiment['seed']}:{subject}".encode()).digest()
    if int.from_bytes(draw[:8]) % 100 >= experiment["traffic"]:
        return "outside"
    return "A" if int.from_bytes(draw[8:16]) % 100 < experiment["share_a"] else "B"


def experiment_variant(account: Row | None) -> Variant | None:
    if excluded_request(account) or (experiment := running_experiment()) is None:
        return None
    experiment_id = experiment["id"]
    connection = _db()
    user_id = account["id"] if account is not None else None
    if user_id is not None:
        enrollment = connection.execute(
            "SELECT subject, arm FROM ab_enrollments WHERE experiment_id = ? AND user_id = ?",
            (experiment_id, user_id),
        ).fetchone()
        subject = f"u:{user_id}"
    else:
        subject = _visitor() or _new_visitor()
        enrollment = connection.execute(
            "SELECT subject, arm, user_id FROM ab_enrollments WHERE experiment_id = ? AND subject = ?",
            (experiment_id, subject),
        ).fetchone()
        if enrollment is not None and enrollment["user_id"] is not None:
            subject, enrollment = _new_visitor(), None
    if enrollment is None:
        with connection:
            connection.execute(
                """INSERT INTO ab_enrollments (experiment_id, subject, user_id, arm)
                   SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM ab_experiments WHERE id = ? AND ended_at IS NULL)
                   ON CONFLICT DO NOTHING""",
                (experiment_id, subject, user_id, _arm(experiment, subject), experiment_id),
            )
            if user_id is None:
                enrollment = connection.execute(
                    """SELECT subject, arm FROM ab_enrollments
                       WHERE experiment_id = ? AND subject = ? AND user_id IS NULL""",
                    (experiment_id, subject),
                ).fetchone()
            else:
                enrollment = connection.execute(
                    "SELECT subject, arm FROM ab_enrollments WHERE experiment_id = ? AND user_id = ?",
                    (experiment_id, user_id),
                ).fetchone()
    if enrollment is None or enrollment["arm"] == "outside":
        return None
    mode = experiment["mode_a" if enrollment["arm"] == "A" else "mode_b"]
    return Variant(experiment_id, enrollment["subject"], mode, experiment["bm25_weight"])


def track_experiment_activity(response: Response) -> Response:
    if response.status_code == 200 and g.get("ab_visitor_changed"):
        response.set_cookie(VISITOR_COOKIE, _signer().dumps(_visitor()), max_age=COOKIE_AGE,
                            httponly=True, secure=request.is_secure, samesite="Lax")
    if (
        request.endpoint not in ACTIVITY_ENDPOINTS
        or response.status_code != 200
        or (request.method != "GET" and not (request.endpoint == "index" and request.method == "POST"))
    ):
        return response
    subject = _visitor()
    account = current_account()
    if (subject is None and account is None) or excluded_request(account):
        return response
    now = int(time())
    user_id = account["id"] if account is not None else None
    connection = _db()
    pending = connection.execute(
        """SELECT a.experiment_id, a.subject FROM ab_enrollments AS a
           JOIN ab_experiments AS e ON e.id = a.experiment_id
           WHERE ((? IS NOT NULL AND a.user_id = ?) OR (? IS NULL AND a.user_id IS NULL AND a.subject = ?))
             AND a.arm IN ('A', 'B') AND (e.ended_at IS NULL OR e.ended_at >= ?)
             AND NOT EXISTS (SELECT 1 FROM ab_activity_days WHERE experiment_id = a.experiment_id
                             AND subject = a.subject AND day = ?)""",
        (user_id, user_id, user_id, subject, now - 9 * DAY, now // DAY),
    ).fetchall()
    if pending:
        with connection:
            connection.executemany(
                "INSERT OR IGNORE INTO ab_activity_days (experiment_id, subject, day) VALUES (?, ?, ?)",
                [(row["experiment_id"], row["subject"], now // DAY) for row in pending],
            )
    return response


def group_metrics(experiment_id: int) -> dict[str, GroupMetrics]:
    today = int(time()) // DAY
    rows = _db().execute(
        """WITH exposure AS (
               SELECT experiment_subject AS subject, MIN(shown_at) / 86400 AS first_day,
                      COUNT(*) AS impressions, COUNT(clicked_at) AS clicks, COUNT(previewed_at) AS previews
               FROM recommendation_impressions WHERE experiment_id = ? AND audience IN ('guest', 'account')
               GROUP BY experiment_subject
           )
           SELECT a.arm, COUNT(*), COUNT(x.subject), COALESCE(SUM(x.impressions), 0),
                  COALESCE(SUM(x.clicks), 0), COALESCE(SUM(x.previews), 0),
                  COUNT(CASE WHEN x.first_day + 7 < ? THEN 1 END),
                  COUNT(CASE WHEN x.first_day + 7 < ? AND d.day IS NOT NULL THEN 1 END)
           FROM ab_enrollments AS a LEFT JOIN exposure AS x ON a.subject = x.subject
           LEFT JOIN ab_activity_days AS d ON d.experiment_id = a.experiment_id AND d.subject = a.subject
                AND d.day = x.first_day + 7
           WHERE a.experiment_id = ? AND a.arm IN ('A', 'B') GROUP BY a.arm""",
        (experiment_id, today, today, experiment_id),
    )
    return {"A": GroupMetrics(), "B": GroupMetrics(), **{row[0]: GroupMetrics(*row[1:]) for row in rows}}
