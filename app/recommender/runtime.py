"""Рантайм-переранжирование следующих страниц пагинации на основе действий в сессии.

Комбинирует:
1. Гибридную матрицу совстречаемости (Item-to-Item Co-visitation):
   - Prior: кураторский граф связей (`recommended_links.json`) + контентная близость тем и заголовков.
   - Live: реальные переходы из сессий в `recommendation_impressions` и `interaction_events`.
2. Позитивный буст от открытых в текущей сессии материалов (`view` и `click`).
3. Пессимизацию (Implicit Negative Feedback) скипнутых тем и форматов — карточек, которые были
   показаны на предыдущих страницах, но проигнорированы пользователем (при том что он взаимодействовал
   с другими карточками).
4. Исключение уже показанных на предыдущих страницах карточек (чтобы верхушка выдачи не прыгала)
   и опускание уже просмотренных вне сетки материалов (например, из чата или траектории) в хвост.
"""

from collections import Counter, defaultdict
from collections.abc import Sequence
from math import log1p, sqrt
from pathlib import Path
import sqlite3

from app.catalog import catalog_data_dir, material_format
from app.contracts import InteractionEvent, Material, Profile, Recommendation
from app.recommender.engine import (
    ACTION_VERB_STEMS,
    _affinity_index,
    _load_recommended_links,
    _normalize_text,
)


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


def rerank_next_page(
    cards: Sequence[tuple[Material, Recommendation]],
    *,
    shown_ids: Sequence[str],
    session_seen_ids: set[str],
    history: Sequence[InteractionEvent],
    profile: Profile,
    links_directory: Path | None = None,
    db_connection: sqlite3.Connection | None = None,
) -> list[tuple[Material, Recommendation]]:
    """Переранжирует хвост выдачи после `shown_ids` на основе действий пользователя с показанными карточками."""
    if not cards or not shown_ids:
        return list(cards)

    cards_by_id = {mat.id: (mat, rec) for mat, rec in cards}
    locked_prefix: list[tuple[Material, Recommendation]] = []
    locked_set: set[str] = set()
    for mid in shown_ids:
        if mid in cards_by_id and mid not in locked_set:
            locked_prefix.append(cards_by_id[mid])
            locked_set.add(mid)

    remaining = [(mat, rec) for mat, rec in cards if mat.id not in locked_set]
    if len(remaining) <= 1:
        return locked_prefix + remaining

    # Собираем позитивные сигналы: что пользователь реально открывал (модалки) или кликал
    clicked_ids = {ev.material_id for ev in history if ev.kind in {"click", "complete"}}
    viewed_ids = {ev.material_id for ev in history if ev.kind == "view"} | set(session_seen_ids)
    engaged_ids = clicked_ids | viewed_ids

    # Если пользователь ещё ни с чем не взаимодействовал — сохраняем исходный порядок остальных карточек
    if not engaged_ids:
        return locked_prefix + remaining

    # Карточки из показанных на предыдущих страницах, которые пользователь открыл vs пропустил (скипнул)
    engaged_on_page = [mid for mid in locked_set if mid in engaged_ids]
    skipped_on_page = [mid for mid in locked_set if mid not in engaged_ids]

    all_materials = {mat.id: mat for mat, _ in cards}
    links = _load_recommended_links(links_directory or catalog_data_dir())

    # 1. Считаем положительные темы и форматы из просмотренных/кликнутых материалов
    positive_topics: Counter[str] = Counter()
    positive_formats: Counter[str] = Counter()
    seed_titles: dict[str, str] = {}
    for mid in engaged_ids:
        mat = all_materials.get(mid)
        if mat is None:
            continue
        weight = 2.2 if mid in clicked_ids else 1.2
        if mid in locked_set:
            # Действие прямо на текущей странице выдачи имеет повышенный вес
            weight *= 1.35
        seed_titles[mid] = mat.title
        for t in mat.topics:
            tn = _normalize_text(t)
            if tn:
                positive_topics[tn] += weight
        fmt = material_format(mat)
        if fmt:
            positive_formats[fmt] += weight * 0.5

    # 2. Считаем скипнутые темы и форматы (только если пользователь взаимодействовал хотя бы с 1 карточкой
    # или открыл что-то в сессии, чтобы проигнорированные карточки на экране считались осознанным скипом)
    skipped_topics: Counter[str] = Counter()
    skipped_formats: Counter[str] = Counter()
    for mid in skipped_on_page:
        mat = all_materials.get(mid)
        if mat is None:
            continue
        for t in mat.topics:
            tn = _normalize_text(t)
            # Не штрафуем тему, если пользователь одновременно открыл другую карточку с этой же темой
            if tn and tn not in positive_topics:
                skipped_topics[tn] += 1.0
        fmt = material_format(mat)
        if fmt and fmt not in positive_formats:
            skipped_formats[fmt] += 0.5

    # 3. Матрица Item-to-Item Co-visitation (Prior граф + контентная близость + Live SQLite)
    index_records = tuple((m.id, m.title, m.topics) for m in all_materials.values())
    titles_map, topics_map, title_idf, topic_idf = _affinity_index(index_records)

    covisit_scores: dict[str, tuple[float, str]] = {}
    for seed_id in engaged_ids:
        if seed_id not in all_materials:
            continue
        seed_weight = 1.6 if seed_id in clicked_ids else 1.0
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
            pair_score = (content_sim * 2.5 + graph_sim) * seed_weight
            if pair_score > covisit_scores.get(cid, (0.0, ""))[0]:
                covisit_scores[cid] = (pair_score, seed_title)

    live_co = _live_covisitation(db_connection, engaged_ids)

    # 4. Следующий логический шаг по формату (Статья/Подкаст -> Курс/Вебинар -> Шаблон/Калькулятор/Чек-лист)
    next_format_Map = {
        "статья": {"курс", "шаблон", "чек-лист", "калькулятор"},
        "подкаст": {"курс", "статья", "чек-лист"},
        "курс": {"шаблон", "чек-лист", "калькулятор"},
        "вебинар": {"курс", "шаблон", "чек-лист"},
    }
    preferred_next_formats: set[str] = set()
    for fmt in positive_formats:
        preferred_next_formats.update(next_format_Map.get(fmt, set()))

    # 5. Итоговое переранжирование оставшегося пула
    total_remaining = len(remaining)
    reranked_unseen: list[tuple[float, Material, Recommendation]] = []
    reranked_already_viewed: list[tuple[float, Material, Recommendation]] = []

    for rank_idx, (mat, rec) in enumerate(remaining):
        # Базовый скор от исходной позиции (плавно убывает, сохраняя релевантность запросу)
        base_rank_score = 12.0 * (1.0 - (rank_idx / max(total_remaining, 1)) * 0.65)
        pop_tiebreak = min((mat.popularity or 0.0) / 3000.0, 1.2)

        mat_topics = [_normalize_text(t) for t in mat.topics if _normalize_text(t)]
        mat_fmt = material_format(mat)

        # Буст за совпадение с интересными темами
        pos_topic_boost = sum(positive_topics.get(t, 0.0) for t in mat_topics) * 3.2
        # Буст от Co-visitation (Item-to-Item)
        covisit_val, covisit_source = covisit_scores.get(mat.id, (0.0, ""))
        live_covisit_val = live_co.get(mat.id, 0.0)
        # Буст за развитие формата
        fmt_progression_boost = 1.8 if mat_fmt in preferred_next_formats else 0.0

        # Пессимизация за скипнутые темы и форматы
        skip_topic_penalty = sum(skipped_topics.get(t, 0.0) for t in mat_topics) * 2.6
        skip_fmt_penalty = skipped_formats.get(mat_fmt, 0.0) * 1.4

        runtime_score = (
            base_rank_score
            + pos_topic_boost
            + covisit_val * 2.4
            + live_covisit_val
            + fmt_progression_boost
            + pop_tiebreak
            - skip_topic_penalty
            - skip_fmt_penalty
        )

        # Если карточка сильно поднялась благодаря рантайм-действиям, обогащаем объяснение («Почему подходит»)
        updated_rec = rec
        if covisit_source and (pos_topic_boost >= 3.0 or covisit_val >= 1.8):
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
    return locked_prefix + final_tail
