"""Воспроизводимые сценарии на обезличенных событиях и материалах каталога."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from app.contracts import InteractionEvent, Material, Profile
from evaluation.quality_data import _time, _topic

SUITE_SIZE = 50
NARROW = {"туризм", "общепит", "образование", "строительство и недвижимость", "производство", "логистика"}
GLOSSARY_QUESTIONS = {
    "ABC-анализ": "Что такое ABC-анализ ассортимента?",
    "Смета": "Объясните, что такое смета проекта",
    "Франшиза": "Хочу узнать значение термина франшиза",
    "Сальдо": "Что значит сальдо в бухгалтерии?",
    "Венчур": "Венчур: что означает этот термин?",
    "LTV": "Объясните термин LTV",
    "Коносамент": "Что значит коносамент в перевозках?",
    "CPA": "Что означает CPA в рекламе?",
    "Аутсорсинг": "Дайте определение аутсорсинга",
    "Охрана труда": "Что означает охрана труда?",
    "Юнит-экономика": "Дайте определение юнит-экономики",
}
INDUSTRY_QUESTIONS = (
    ("туризм", "Как турагентству оформить авансовый отчёт по форме АО-1?", "e16c18e6-278b-44fb-9e42-1ce1222d1c33"),
    ("туризм", "Нужен скрипт телефонной продажи тура", "ad1789d1-6330-4ad5-b465-2bf618adce12"),
    ("образование", "Как открыть школу ораторского мастерства?", "836d20b1-55b9-4bd0-bc27-1cdadae0d655"),
    ("образование", "Как открыть фонетическую школу английского языка?", "c7441fb2-c22c-482d-93b8-fdba3e7f75fc"),
    ("образование", "Нужна справка об оплате образовательных услуг для налоговой", "39d8e2ca-77c4-4c6e-87c8-2afa3c314934"),
)


@dataclass(frozen=True)
class Case:
    group: str
    profile: Profile = Profile()
    history: tuple[InteractionEvent, ...] = ()
    targets: frozenset[str] = frozenset()
    cutoff: datetime | None = None


def sample_key(*values):
    return sha256("|".join(values).encode()).digest()


def history_before(events, cutoff, limit=9):
    earlier = [event for event in events if event.occurred_at < cutoff]
    return tuple(sorted(earlier, key=lambda e: (e.occurred_at, e.material_id, e.kind))[-limit:])


def publication_dates(reader):
    return {row["Id"]: published for table in ("Courses", "Articles", "Documents", "Webinars", "Podcasts", "Calculators")
            for row in reader.load_rows(table) if row.get("Id") and (published := _time(row.get("Published")))}


def behavioral_cases(reader, catalog, published):
    ids = {m.id for m in catalog}
    courses = {m.id for m in catalog if m.entity_type == "course"}
    parents, lessons = defaultdict(set), defaultdict(set)
    for row in reader.load_rows("CourseLessonBindings"):
        course, lesson = row["CourseId"], row["LessonId"]
        if course in courses and lesson and row["IsDeleted"] != "t":
            parents[lesson].add(course)
            lessons[course].add(lesson)

    users, bindings, abandoned = {}, [], []
    activity, lesson_activity = defaultdict(set), defaultdict(set)
    for row in reader.load_rows("CourseUserBindings"):
        user, course = row["UserId"], row["CourseId"]
        if row["IsDeleted"] == "t" or not user or course not in courses:
            continue
        users[row["Id"]] = (user, course)
        started = _time(row["StudyingStartDate"])
        created = _time(row["Created"])
        if started or created:
            activity[user].add(InteractionEvent(course, "click", started or created))
        if row["IsCompleted"] == "t" and (finished := _time(row["Completed"])):
            activity[user].add(InteractionEvent(course, "complete", finished))
        bindings.append((row, user, course, started))

    for row in reader.load_rows("CourseUserLessonBindings"):
        binding = users.get(row["CourseUserId"])
        when = _time(row["Completed"]) or _time(row["StudyingStartDate"])
        if binding and when:
            user, course = binding
            activity[user].add(InteractionEvent(course, "view", when))
            lesson_activity[row["CourseUserId"]].add(when)

    temporal = []
    for user, events in sorted(activity.items(), key=lambda item: sample_key(item[0])):
        ordered = sorted(events, key=lambda e: (e.occurred_at, e.material_id, e.kind))
        timeline = defaultdict(set)
        for event in ordered:
            timeline[event.occurred_at].add(event.material_id)
        previously_seen, eligible = set(), []
        final_timestamps = tuple(timeline)[-2:]
        for timestamp, materials in timeline.items():
            if (timestamp in final_timestamps and len(materials) == 1 and not materials & previously_seen
                    and all(mid in published and published[mid] < timestamp for mid in materials)):
                history = history_before(ordered, timestamp)
                if len(history) >= 4:
                    eligible.append(Case("Следующий курс", history=history, targets=frozenset(materials), cutoff=timestamp))
            previously_seen.update(materials)
        if eligible:
            temporal.append(eligible[-1])

    scored, positives = [], defaultdict(set)
    for row in reader.load_rows("UserRatings"):
        user, entity = row["UserId"], row["EntityId"]
        if not user or row["IsDeleted"] == "t":
            continue
        when = _time(row["Created"])
        parent_ids = parents[entity]
        target = entity if entity in ids else next(iter(parent_ids)) if len(parent_ids) == 1 else None
        if target and when:
            if row["Value"] in {"4", "5"}:
                positives[user].add(InteractionEvent(target, "click", when))
            elif row["Value"] in {"1", "2"}:
                scored.append((user, target, when))
    ratings = [(user, target, when, Case("Оценки 1–2", history=history_before(activity[user] | positives[user], when),
                                         targets=frozenset({target}))) for user, target, when in scored]

    snapshot = max(event.occurred_at for events in activity.values() for event in events)
    for row, user, course, started in bindings:
        if not started or row["IsCompleted"] == "t" or not lessons[course]:
            continue
        completed = int(row["LessonsCompleted"] or 0)
        last_activity = max(lesson_activity[row["Id"]], default=started)
        if completed / len(lessons[course]) > .05 or snapshot - last_activity < timedelta(days=30):
            continue
        history = history_before(activity[user] | positives[user], started)
        if not any(e.material_id == course and e.occurred_at > last_activity for e in activity[user]):
            abandoned.append((user, course, started, Case("Прогресс ≤5%", history=history, targets=frozenset({course}))))

    def select(pool, limit):
        cases, seen = [], set()
        for user, target, when, case in sorted(pool, key=lambda r: (not r[3].history, sample_key(r[0], r[1], r[2].isoformat()))):
            if user in seen:
                continue
            seen.add(user)
            cases.append(case)
            if len(cases) == limit:
                break
        return cases

    return temporal[:SUITE_SIZE], select(ratings, 35) + select(abandoned, 15), len(temporal)


def read_authors(reader):
    authors = defaultdict(set)
    for kind in ("Article", "Course", "Podcast", "Webinar"):
        for row in reader.load_rows(f"{kind}AuthorBindings"):
            if row["AuthorId"] and row[f"{kind}Id"]:
                authors[row[f"{kind}Id"]].add(row["AuthorId"])
    return authors


def rare_cases(reader, catalog, authored):
    glossary = tuple(Material(
        id=row["Id"], title=row["Title"], url="https://kurs.alfabank.ru/glossary/" + row["Url"],
        description=row["Description"] or "", format="термин", access="free", entity_type="glossary",
    ) for row in reader.load_rows("Glossary") if row["Published"] and row["IsDeleted"] != "t"
        and row["Title"] and row["Url"])
    by_id = {m.id: m for m in catalog}
    cases, industry_cases = [], []
    labels = {"calculator": "Калькуляторы", "podcast": "Подкасты", "webinar": "Вебинары"}
    for row in authored:
        targets = frozenset(row["relevant_ids"])
        formats = {by_id[mid].entity_type for mid in targets}
        profile = Profile(**row["profile"])
        if len(formats) == 1 and next(iter(formats)) in labels:
            cases.append(Case(labels[next(iter(formats))], profile, targets=targets))
        industry = _topic(profile.industry)
        if industry in NARROW:
            matching = frozenset(mid for mid in targets if industry in set(map(_topic, by_id[mid].industries)))
            if matching:
                industry_cases.append(Case("Отрасль · " + industry, profile, targets=matching))

    for material in glossary:
        if goal := GLOSSARY_QUESTIONS.get(material.title):
            cases.append(Case("Глоссарий", Profile(goal=goal), targets=frozenset({material.id})))

    for industry, goal, target in INDUSTRY_QUESTIONS:
        industry_cases.append(Case("Отрасль · " + industry, Profile(industry=industry, goal=goal),
                                   targets=frozenset({target})))
    per_group = defaultdict(list)
    for case in industry_cases:
        per_group[case.group].append(case)
    for index in range(SUITE_SIZE):
        for group in sorted(per_group):
            if index < len(per_group[group]) and len(cases) < SUITE_SIZE:
                cases.append(per_group[group][index])
    return glossary, cases


def context_cases(catalog, topics):
    beginner = {"начинающий", "без опыта"}
    experienced = {"есть опыт", "опытный", "с опытом"}
    dated = datetime(2026, 9, 1, tzinfo=UTC)

    def make_case(group, old, fresh, expected, index):
        old = sorted(old, key=lambda mid: sample_key(group, str(index), mid))[:4]
        fresh = sorted(set(fresh) - set(old), key=lambda mid: sample_key(group, str(index), mid))[:2]
        targets = frozenset(set(expected) - set(old) - set(fresh))
        if len(old) < 4 or len(fresh) < 2 or not targets:
            return None
        history = tuple(InteractionEvent(mid, "click", dated - timedelta(days=age))
                        for mid, age in zip(old + fresh, (65, 64, 63, 62, 2, 1)))
        return Case(group, history=history, targets=targets)

    by_topic, by_industry = defaultdict(list), defaultdict(list)
    for m in catalog:
        for topic in topics.get(m.id, ()):
            by_topic[topic].append(m)
        for industry in set(map(_topic, m.industries)):
            if industry in NARROW:
                by_industry[industry].append(m)

    levels = []
    for topic in sorted(by_topic):
        old = [m.id for m in by_topic[topic] if beginner.intersection(map(_topic, m.experience_levels))]
        fresh = [m.id for m in by_topic[topic] if experienced.intersection(map(_topic, m.experience_levels))]
        if len(old) >= 4 and len(fresh) >= 3:
            levels.append((old, fresh))
    cases = []
    for index in range(25):
        if levels:
            old, fresh = levels[index % len(levels)]
            cases.append(make_case("Рост уровня", old, fresh, fresh, index))
    pairs = [(old, fresh) for old in sorted(by_industry) for fresh in sorted(by_industry) if old != fresh]
    for index, (old_name, new_name) in enumerate(pairs):
        old = [m.id for m in by_industry[old_name] if new_name not in set(map(_topic, m.industries))]
        fresh = [m.id for m in by_industry[new_name] if old_name not in set(map(_topic, m.industries))]
        if case := make_case("Смена отрасли", old, fresh, fresh, index):
            cases.append(case)
            if len(cases) == SUITE_SIZE:
                break
    return [case for case in cases if case]


def cold_cases(catalog, topics):
    cases = [Case("Без действий и тегов")]
    industries = ("розница", "услуги", "логистика", "производство", "образование", "общепит",
                  "строительство и недвижимость", "информационные технологии")
    for industry in industries:
        for topic in (None, "маркетинг", "финансы"):
            ids = frozenset(m.id for m in catalog if m.access == "free" and industry in set(map(_topic, m.industries)))
            cases.append(Case("Без действий, отрасль задана", Profile(industry=industry, learning_topic=topic), targets=ids))
    per_topic = defaultdict(int)
    for m in sorted(catalog, key=lambda item: sample_key(item.id)):
        labels = topics.get(m.id, set())
        if m.entity_type != "article" or not labels or all(per_topic[label] >= 3 for label in labels):
            continue
        for label in labels:
            per_topic[label] += 1
        cases.append(Case("Один клик", history=(InteractionEvent(m.id, "click"),)))
        if len(cases) == SUITE_SIZE:
            break
    return cases
