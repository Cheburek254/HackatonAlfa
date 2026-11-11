"""Стабильная пагинация и ограниченное переранжирование по действиям в сессии."""

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from math import log1p, sqrt
from pathlib import Path
import sqlite3

from app.catalog import catalog_data_dir, material_format
from app.contracts import InteractionEvent, Material, Recommendation
from app.recommender.engine import (
    ACTION_VERB_STEMS,
    _affinity_index,
    _load_recommended_links,
    _normalize_text,
)

WINDOW_PAGES = 3
CONFIRMATION_AGE = timedelta(minutes=30)
Card = tuple[Material, Recommendation]


def _live_covisitation(
    connection: sqlite3.Connection | None,
    seed_ids: set[str],
) -> dict[str, float]:
    """Извлекает реальные связи совстречаемости (Item-to-Item) из SQLite по сессиям и пользователям."""
    if connection is None or not seed_ids:
        return {}
    placeholders = ",".join("?" for _ in seed_ids)
    params = tuple(seed_ids)
    co_counts: dict[str, float] = defaultdict(float)
    try:
        # 1. Совстречаемость внутри батчей показов, где пользователь открывал превью или кликал
        batch_rows = connection.execute(
            f"""
            SELECT b2.material_id, COUNT(*) AS cnt,
                   SUM(CASE WHEN b2.clicked_at IS NOT NULL THEN 2.0 ELSE 1.0 END) AS weight
            FROM recommendation_impressions b1
            JOIN recommendation_impressions b2
              ON b1.batch_id = b2.batch_id AND b1.material_id != b2.material_id
            WHERE b1.material_id IN ({placeholders})
              AND (b1.clicked_at IS NOT NULL OR b1.previewed_at IS NOT NULL)
              AND (b2.clicked_at IS NOT NULL OR b2.previewed_at IS NOT NULL)
            GROUP BY b2.material_id
            LIMIT 64
            """,
            params,
        ).fetchall()
        for row in batch_rows:
            co_counts[row[0]] += log1p(float(row[2] or row[1])) * 2.2

        # 2. Совстречаемость в истории взаимодействий авторизованных пользователей
        user_rows = connection.execute(
            f"""
            SELECT e2.material_id,
                   SUM(CASE WHEN e2.kind = 'click' THEN 2.0 ELSE 1.0 END) AS weight
            FROM interaction_events e1
            JOIN interaction_events e2
              ON e1.user_id = e2.user_id AND e1.material_id != e2.material_id
            WHERE e1.material_id IN ({placeholders})
              AND ABS(e1.occurred_at - e2.occurred_at) <= 604800
            GROUP BY e2.material_id
            LIMIT 64
            """,
            params,
        ).fetchall()
        for row in user_rows:
            co_counts[row[0]] += log1p(float(row[1])) * 1.8
    except sqlite3.Error:
        return {}
    return co_counts


def rank_catalog_pages(
    cards: Sequence[Card],
    *,
    page: int,
    page_size: int,
    visited_pages: Mapping[int, Sequence[str]],
    exposed_ids: set[str],
    session_seen_ids: set[str],
    history: Sequence[InteractionEvent],
    now: datetime,
    links_directory: Path | None = None,
    db_connection: sqlite3.Connection | None = None,
) -> list[Card]:
    """Закрепляет открытые страницы и пересчитывает окно около новой страницы."""
    if not visited_pages:
        return list(cards)

    cards_by_id = {mat.id: (mat, rec) for mat, rec in cards}
    pinned = {}
    pinned_ids = set()
    for visited_page, ids in sorted(visited_pages.items()):
        start = (visited_page - 1) * page_size
        for position, mid in enumerate(ids[:page_size], start):
            if 0 <= position < len(cards) and mid in cards_by_id and mid not in pinned_ids:
                pinned[position] = cards_by_id[mid]
                pinned_ids.add(mid)

    remaining = iter(card for card in cards if card[0].id not in pinned_ids)
    ordered = [pinned[position] if position in pinned else next(remaining) for position in range(len(cards))]
    if page == 1 or page in visited_pages:
        return ordered

    start = (page - 1) * page_size
    positions = [index for index in range(start, min(len(cards), start + WINDOW_PAGES * page_size))
                 if index not in pinned]
    if len(positions) < 2:
        return ordered
    previously_exposed = {mid for previous, ids in visited_pages.items() if previous < page for mid in ids}
    confirmed = {event.material_id for event in history
                 if event.kind in {"click", "complete"} and event.material_id in session_seen_ids
                 and (event.occurred_at is None or now - CONFIRMATION_AGE <= event.occurred_at <= now)}
    materials = {mid: card[0] for mid, card in cards_by_id.items()}
    reordered = _rerank_window(
        [ordered[index] for index in positions], materials, session_seen_ids, confirmed,
        exposed_ids & previously_exposed, links_directory, db_connection,
    )
    for index, card in zip(positions, reordered, strict=True):
        ordered[index] = card
    return ordered


def _rerank_window(remaining, all_materials, seen_ids, clicked_ids, exposed_ids, links_directory, db_connection):
    engaged_ids = seen_ids & all_materials.keys()
    if not engaged_ids:
        return remaining

    links = _load_recommended_links(links_directory or catalog_data_dir())

    # Два разных подтверждённых материала дают сильный сигнал только по общей теме.
    clicked_topics = Counter(topic for mid in clicked_ids & engaged_ids
                             for topic in set(map(_normalize_text, all_materials[mid].topics)) if topic)
    established_topics = {topic for topic, count in clicked_topics.items() if count >= 2}
    established_ids = {mid for mid in clicked_ids & engaged_ids
                       if established_topics.intersection(map(_normalize_text, all_materials[mid].topics))}
    positive_topics: Counter[str] = Counter()
    positive_formats = set()
    for mid in engaged_ids:
        mat = all_materials[mid]
        for topic in set(map(_normalize_text, mat.topics)) - {""}:
            positive_topics[topic] += 1.4 if topic in established_topics and mid in established_ids else (
                0.45 if mid in clicked_ids else 0.15
            )
        fmt = material_format(mat)
        if fmt:
            positive_formats.add(fmt)

    # Пропуск учитывается после подтверждения интереса и только для карточек из видимой области.
    skipped_topics: Counter[str] = Counter()
    skipped_formats: Counter[str] = Counter()
    if established_ids:
        for mid in (exposed_ids & all_materials.keys()) - engaged_ids:
            mat = all_materials[mid]
            for topic in set(map(_normalize_text, mat.topics)) - {""} - positive_topics.keys():
                skipped_topics[topic] += 0.45
            fmt = material_format(mat)
            if fmt and fmt not in positive_formats:
                skipped_formats[fmt] += 0.2

    # 3. Матрица Item-to-Item Co-visitation (Prior граф + контентная близость + Live SQLite)
    covisit_scores: dict[str, tuple[float, str]] = {}
    if established_ids:
        index_records = tuple((m.id, m.title, m.topics) for m in all_materials.values())
        titles_map, topics_map, title_idf, topic_idf = _affinity_index(index_records)
    for seed_id in sorted(established_ids):
        seed_title = all_materials[seed_id].title
        direct_neighbors = set(links.get(seed_id, ()))

        # Prior: 2-шаговый граф связей
        two_hop: Counter[str] = Counter()
        for neighbor in direct_neighbors:
            for second in links.get(neighbor, ()):
                if second != seed_id:
                    two_hop[second] += 1.0 / sqrt(max(1, len(links.get(neighbor, ()))))

        for cand_mat, _ in remaining:
            cid = cand_mat.id
            shared_terms = sum(
                title_idf.get(t, 0.0)
                for t in (titles_map.get(seed_id, frozenset()) & titles_map.get(cid, frozenset())) - ACTION_VERB_STEMS
            )
            shared_topics = sum(
                topic_idf.get(t, 0.0)
                for t in topics_map.get(seed_id, frozenset()) & topics_map.get(cid, frozenset())
            )
            content_sim = (shared_terms + 0.75 * shared_topics) / sqrt(4 + len(titles_map.get(cid, ())))
            graph_sim = (2.8 if cid in direct_neighbors else 0.0) + 1.5 * two_hop.get(cid, 0.0)
            pair_score = content_sim + graph_sim
            if pair_score > covisit_scores.get(cid, (0.0, ""))[0]:
                covisit_scores[cid] = (pair_score, seed_title)

    live_co = _live_covisitation(db_connection, established_ids)

    # 4. Следующий логический шаг по формату (Статья/Подкаст -> Курс/Вебинар -> Шаблон/Калькулятор/Чек-лист)
    next_formats = {
        "статья": {"курс", "шаблон", "чек-лист", "калькулятор"},
        "подкаст": {"курс", "статья", "чек-лист"},
        "курс": {"шаблон", "чек-лист", "калькулятор"},
        "вебинар": {"курс", "шаблон", "чек-лист"},
    }
    preferred_next_formats: set[str] = set()
    for mid in established_ids:
        preferred_next_formats.update(next_formats.get(material_format(all_materials[mid]), set()))

    # 5. Итоговое переранжирование оставшегося пула
    total_remaining = len(remaining)
    reranked_unseen: list[tuple[float, Material, Recommendation]] = []
    reranked_already_viewed: list[tuple[float, Material, Recommendation]] = []

    for rank_idx, (mat, rec) in enumerate(remaining):
        # Базовый скор от исходной позиции (плавно убывает, сохраняя релевантность запросу)
        base_rank_score = 12.0 * (1.0 - (rank_idx / max(total_remaining, 1)) * 0.65)

        mat_topics = [_normalize_text(t) for t in mat.topics if _normalize_text(t)]
        mat_fmt = material_format(mat)

        # Буст за совпадение с интересными темами
        pos_topic_boost = sum(positive_topics.get(t, 0.0) for t in set(mat_topics))
        if not established_ids:
            pos_topic_boost = min(pos_topic_boost, 0.6)
        # Буст от Co-visitation (Item-to-Item)
        covisit_val, covisit_source = covisit_scores.get(mat.id, (0.0, ""))
        live_covisit_val = live_co.get(mat.id, 0.0)
        # Буст за развитие формата
        fmt_progression_boost = 0.7 if mat_fmt in preferred_next_formats else 0.0

        # Пессимизация за скипнутые темы и форматы
        skip_topic_penalty = sum(skipped_topics.get(t, 0.0) for t in set(mat_topics))
        skip_fmt_penalty = skipped_formats.get(mat_fmt, 0.0)

        runtime_score = (
            base_rank_score
            + min(5.0, pos_topic_boost + covisit_val + live_covisit_val + fmt_progression_boost)
            - min(1.5, skip_topic_penalty + skip_fmt_penalty)
        )

        # Если карточка сильно поднялась благодаря рантайм-действиям, обогащаем объяснение («Почему подходит»)
        updated_rec = rec
        if covisit_source and (established_topics.intersection(mat_topics) or covisit_val >= 1.8):
            prefix = f"Подобрано на основе вашего интереса к материалу «{covisit_source}». "
            if covisit_source not in rec.reason:
                updated_rec = Recommendation(
                    material_id=rec.material_id,
                    reason=prefix + rec.reason,
                    next_step=rec.next_step,
                )

        # Если материал уже был открыт пользователем (например, через модалку траектории или чат),
        # убираем его из следующей шестёрки в самый конец, чтобы не предлагать уже увиденное
        if mat.id in engaged_ids:
            reranked_already_viewed.append((runtime_score, mat, updated_rec))
        else:
            reranked_unseen.append((runtime_score, mat, updated_rec))

    reranked_unseen.sort(key=lambda item: item[0], reverse=True)
    reranked_already_viewed.sort(key=lambda item: item[0], reverse=True)

    final_tail = [(mat, rec) for _, mat, rec in reranked_unseen] + [
        (mat, rec) for _, mat, rec in reranked_already_viewed
    ]
    return final_tail
