"""Общий интерфейс каталога, ранжирования и интерфейса пользователя."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


@dataclass(frozen=True)
class Material:
    id: str
    title: str
    url: str
    description: str = ""
    topics: tuple[str, ...] = ()
    format: str | None = None
    access: Literal["free", "paid", "unknown"] = "unknown"
    stages: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    experience_levels: tuple[str, ...] = ()
    popularity: float | None = None
    rating: float | None = None
    cost: float | None = None
    entity_type: str | None = None
    subscriptions: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    duration: str | None = None
    lesson_count: int | None = None


@dataclass(frozen=True)
class Profile:
    stage: str | None = None
    industry: str | None = None
    goal: str | None = None
    experience: str | None = None
    team_size: str | None = None
    learning_topic: str | None = None
    preferred_format: str | None = None


@dataclass(frozen=True)
class InteractionEvent:
    material_id: str
    kind: Literal["view", "click", "complete"] = "view"
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class Recommendation:
    material_id: str
    reason: str
    next_step: str


@dataclass(frozen=True)
class RecommendationResult:
    items: tuple[Recommendation, ...]
    ordering_reason: str
    follow_up_question: str | None = None


class Recommender(Protocol):
    def __call__(
        self,
        catalog: Sequence[Material],
        profile: Profile,
        history: Sequence[InteractionEvent],
        limit: int,
    ) -> RecommendationResult:
        """Вернуть до limit уникальных материалов каталога в порядке приоритета.

        Пустой профиль и пустая история допустимы. События по отсутствующим
        в каталоге материалам игнорируются. При пустом каталоге или limit <= 0
        список материалов пуст. Для непустой выдачи обязательны объяснение
        порядка, причина выбора каждого материала и следующий шаг.
        """
        ...
