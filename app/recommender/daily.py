"""Ежедневные подборки статей и подкастов по сигналам из всего каталога."""

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from math import log1p
from pathlib import Path

from app.catalog.publications import MSK
from app.catalog.subscriptions import access_category
from app.contracts import InteractionEvent, Material, Profile
from app.recommender.engine import (
    TITLE_HISTORY_WEIGHT, _experience_score, _history_signals, _industry_score, _load_recommended_links,
    _normalize_text, _stage_score, _tokenize_stems,
)

DAILY_LIMIT = 5


@dataclass(frozen=True)
class DailyPick:
    material: Material
    reason: str
    published: date | None


def daily_picks(
    catalog: dict[str, Material], profile: Profile, history: tuple[InteractionEvent, ...], dates: dict[str, date],
    available: tuple[str, ...], *, now: datetime, links_directory: Path,
) -> dict[str, list[DailyPick]]:
    today = now.astimezone(MSK).date()
    history = tuple(event for event in history if event.material_id in catalog)
    topic_scores, industry_scores, terms, links, seen, _ = _history_signals(
        history, catalog, _load_recommended_links(links_directory), now,
    )
    interest = _normalize_text(profile.learning_topic or "")
    profile_known = bool(profile.stage or profile.industry not in (None, "", "другое") or profile.experience
                         or interest not in ("", "любая тема"))
    known_history = bool(terms) or any(score >= 6 for score, _ in topic_scores.values())
    result = {}
    for kind, title in (("article", "статья"), ("podcast", "подкаст")):
        materials = [m for m in catalog.values() if (m.entity_type == kind or m.format == title) and m.title and m.url
                     and (m.id not in dates or dates[m.id] <= today)]
        latest = max((dates[m.id] for m in materials if m.id in dates), default=today)
        scored = []
        for material in materials:
            topics = tuple(dict.fromkeys(_normalize_text(t) for t in material.topics if _normalize_text(t)))
            trend, source = max((topic_scores[t] for t in topics if t in topic_scores), default=(0., ""))
            graph, graph_source = links.get(material.id, (0., ""))
            title_signal = max((terms.get(term, 0.) for term in _tokenize_stems(material.title)), default=0.)
            sector_signal = max((industry_scores.get(_normalize_text(sector), 0.) for sector in material.industries), default=0.)
            stage, stage_match = _stage_score(material, profile.stage)
            industry, industry_match = _industry_score(material, profile.industry)
            experience, experience_match = _experience_score(material, profile.experience)
            interest_match = interest not in ("", "любая тема") and any(interest in t for t in topics)
            published = dates.get(material.id)
            freshness = (4.0 if kind == "article" else 1.5) * 2 ** (-(latest - published).days / 45) if published else 0.
            popularity = min(2., log1p(material.popularity or 0.) / 4)
            intro = not profile_known and any(_normalize_text(item) in {"идея", "запуск", "начинающий", "без опыта"}
                                              for item in (*material.stages, *material.experience_levels))
            eligible = access_category(material) in available
            score = (
                trend + graph + title_signal + sector_signal + 0.45 * (stage + industry + experience)
                + 4 * interest_match + freshness + popularity + (material.rating or 0.) / 5 + 2 * intro
                - (6 if not eligible else 0) - (1.5 if seen.get(material.id) == "view" else 0)
            )
            if (trend >= 6 or graph >= 2) and (graph_source or source):
                reason = f"Развивает тему материала «{graph_source or source}», который вы открывали."
            elif title_signal >= TITLE_HISTORY_WEIGHT:
                reason = "По темам ваших недавних переходов."
            elif interest_match:
                reason = f"По интересу «{profile.learning_topic}» из профиля."
            elif industry_match or stage_match or experience_match:
                reason = "Подходит по параметрам вашего бизнеса."
            elif intro:
                reason = "Подойдёт для знакомства с темой."
            elif published and (latest - published).days <= 45:
                reason = "Среди свежих публикаций в каталоге."
            else:
                reason = "Из подборки для знакомства с каталогом."
            daily_tie = sha256(f"{today}:{material.id}".encode()).digest()
            scored.append((seen.get(material.id) in {"click", "complete"}, -score, daily_tie,
                           topics[0] if topics else material.id, DailyPick(material, reason, published)))

        scored.sort(key=lambda row: row[:3])
        if profile_known or known_history:
            result[kind] = [row[4] for row in scored[:DAILY_LIMIT]]
            continue
        accessible = [row for row in scored if access_category(row[4].material) in available]
        other = [row for row in scored if access_category(row[4].material) not in available]
        distinct = {}
        for row in accessible:
            distinct.setdefault(row[3], row[4])
        picks = list(distinct.values())[:DAILY_LIMIT]
        if len(picks) < DAILY_LIMIT:
            picked_ids = {pick.material.id for pick in picks}
            picks.extend(row[4] for row in accessible + other if row[4].material.id not in picked_ids)
        result[kind] = picks[:DAILY_LIMIT]
    return result
