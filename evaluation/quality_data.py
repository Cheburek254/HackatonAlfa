"""Данные бэкапа для офлайн-оценки. Идентификаторы пользователей остаются в памяти."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from app.catalog.normalizer import PgDumpReader
from app.contracts import InteractionEvent, Material


@dataclass(frozen=True)
class ReplayCase:
    history: tuple[InteractionEvent, ...]
    completed_courses: frozenset[str]
    completed_lessons: frozenset[str]
    liked_topics: frozenset[str]


@dataclass(frozen=True)
class QualityData:
    replay: list[ReplayCase]
    eligible_users: int
    observed_items: set[str]
    topics: dict[str, set[str]]
    expert: dict[str, set[str]]
    expert_raw_count: int
    text_blocks: dict[str, str]
    course_bindings_with_subscription: int


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.year > 1970 and result.tzinfo is not None else None


def _topic(text: str | None) -> str:
    return " ".join((text or "").casefold().replace("ё", "е").split())


def read_quality_data(backup: Path, catalog: tuple[Material, ...], sample_size: int) -> QualityData:
    reader = PgDumpReader(backup)
    tables = {
        "ContentSettings", "ContentSettingsTopics", "ContentSettingsTags", "Topics", "Tags",
        "CourseLessonBindings", "CourseUserBindings", "CourseUserLessonBindings", "UserRatings",
        "RatingTags", "UserRatingTagsBindings", "RecommendedItems", "ArticleEmbedItems", "LessonEmbedItems",
    }
    if missing := tables - reader.toc_tables.keys():
        raise ValueError(f"Бэкап не содержит таблицы для оценки: {', '.join(sorted(missing))}")
    catalog_ids = {item.id for item in catalog}
    courses = {item.id for item in catalog if item.entity_type == "course" or item.format == "курс"}
    lessons = {item.id for item in catalog if item.entity_type == "lesson" or item.format == "урок"}
    topics = defaultdict(set)
    settings = {row["Id"]: row["EntityId"] for row in reader.load_rows("ContentSettings") if row["IsDeleted"] != "t"}
    for table, column in (("Topics", "TopicId"), ("Tags", "TagId")):
        names = {row["Id"]: _topic(row["Title"]) for row in reader.load_rows(table) if row["IsDeleted"] != "t"}
        for row in reader.load_rows(f"ContentSettings{table}"):
            entity, topic = settings.get(row["ContentSettingsId"]), names.get(row[column])
            if entity and topic:
                topics[entity].add(topic)

    lesson_courses = defaultdict(set)
    for row in reader.load_rows("CourseLessonBindings"):
        if row["IsDeleted"] != "t" and row["CourseId"] in courses and row["LessonId"]:
            lesson_courses[row["LessonId"]].add(row["CourseId"])

    blocks = defaultdict(list)
    for table, column in (("ArticleEmbedItems", "ArticleId"), ("LessonEmbedItems", "LessonId")):
        for row in reader.load_rows(table):
            block, entity = row.get("HtmlContent"), row.get(column)
            if not block:
                continue
            if entity in catalog_ids:
                blocks[entity].append(block)
            elif column == "LessonId":
                for course in lesson_courses.get(entity, ()):
                    blocks[course].append(block)

    events = defaultdict(set)
    likes = defaultdict(list)
    lesson_completions = defaultdict(list)
    course_users = {}
    bindings = reader.load_rows("CourseUserBindings")
    for row in bindings:
        user, course = row["UserId"], row["CourseId"]
        if row["IsDeleted"] == "t" or not user or course not in courses:
            continue
        course_users[row["Id"]] = (user, course)
        if started := _time(row["StudyingStartDate"]) or _time(row["Created"]):
            events[user].add(InteractionEvent(course, "click", started))
        if row["IsCompleted"] == "t" and (finished := _time(row["Completed"])):
            events[user].add(InteractionEvent(course, "complete", finished))
    for row in reader.load_rows("CourseUserLessonBindings"):
        binding = course_users.get(row["CourseUserId"])
        if binding and (completed := _time(row["Completed"])):
            user, course = binding
            events[user].add(InteractionEvent(course, "view", completed))
            if row["LessonId"] in lessons:
                lesson_completions[user].append((completed, row["LessonId"]))
                events[user].add(InteractionEvent(row["LessonId"], "complete", completed))

    rating_tags = {row["Id"]: _topic(row["Title"]) for row in reader.load_rows("RatingTags") if row["IsDeleted"] != "t"}
    by_rating = defaultdict(set)
    for row in reader.load_rows("UserRatingTagsBindings"):
        if tag := rating_tags.get(row["TagId"]):
            by_rating[row["UserRatingId"]].add(tag)
    for row in reader.load_rows("UserRatings"):
        user, entity = row["UserId"], row["EntityId"]
        if row["IsDeleted"] == "t" or row["Value"] not in {"4", "5"} or not user or not (rated := _time(row["Created"])):
            continue
        parents = lesson_courses.get(entity, set())
        item = entity if entity in catalog_ids else next(iter(parents)) if len(parents) == 1 else None
        if item:
            events[user].add(InteractionEvent(item, "click", rated))
        liked = topics.get(entity, set()) | topics.get(item, set()) | by_rating.get(row["Id"], set())
        if liked:
            likes[user].append((rated, liked))

    # Одновременные события целиком остаются за границей истории.
    candidates = {}
    for user, activity in events.items():
        cutoff = max(event.occurred_at for event in activity)
        earlier = [event for event in activity if event.occurred_at < cutoff]
        if earlier:
            candidates[user] = (cutoff, earlier)
    replay = []
    strength = {"view": 0, "click": 1, "complete": 2}
    for user in sorted(candidates, key=lambda value: sha256(value.encode()).digest())[:sample_size]:
        cutoff, earlier = candidates[user]
        completed = frozenset(event.material_id for event in earlier
                              if event.kind == "complete" and event.material_id in courses)
        completed_lessons = frozenset(mid for finished, mid in lesson_completions[user] if finished < cutoff)
        by_item = {}
        for event in sorted(earlier, key=lambda event: (strength[event.kind], event.occurred_at)):
            by_item[event.material_id] = event
        history = tuple(sorted(by_item.values(), key=lambda event: (event.occurred_at, event.material_id))[-20:])
        liked = frozenset(tag for rated, tags in likes[user] if rated < cutoff for tag in tags)
        replay.append(ReplayCase(history, completed, completed_lessons, liked))

    expert = defaultdict(set)
    links = reader.load_rows("RecommendedItems")
    for row in links:
        source, target = row["CourseId"] or row["Id"], row["ItemId"]
        if source in catalog_ids and target in catalog_ids and source != target:
            expert[source].add(target)
    return QualityData(
        replay, len(candidates), {event.material_id for activity in events.values() for event in activity},
        dict(topics), dict(expert), len(links),
        {mid: " ".join(blocks[mid])[:8000] for mid in catalog_ids if mid in blocks},
        sum(bool(row["SubscriptionId"]) for row in bindings if row["IsDeleted"] != "t"),
    )
