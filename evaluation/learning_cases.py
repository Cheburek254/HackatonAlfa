"""Сценарии с несколькими интересами, учебным прогрессом и практическими задачами."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.contracts import InteractionEvent, Material, Profile
from evaluation.learning_briefs import AMBIGUOUS_QUESTIONS, BRIEFS, GRADUATE_TOPICS
from evaluation.quality_data import _time, _topic
from evaluation.stress_cases import SUITE_SIZE, publication_dates, read_authors, sample_key

BASIC, INTERMEDIATE, ADVANCED = "базовый", "средний", "продвинутый"
BEGINNER = re.compile(r"\b(?:азы|основы|азбука|введение|первые шаги|для новичков|с нуля|что такое|как начать)\b")
ADVANCED_TITLE = re.compile(
    r"модель lbo|госзакупки: теория|торговля за рубежом|бизнес с китаем|консолидац\w*|для опытных|продвинут\w*"
    r"|восстановить ндс|ндс при импорте и экспорте|ндс в строительстве|налогов\w* агент\w* по ндс"
    r"|курсовые разницы|таможенн\w* стоимост|трансфертн\w* ценообразован|контролируем\w* иностранн"
    r"|агент\w* банковских гарантий|как не нарушить.*115-фз|переходим с усн на осн и обратно"
)
BASIC_ARTICLES = (
    r"переход на усн: пошагов|как работать на усн|усн для ип",
    r"как зарегистрировать онлайн-кассу|чем кассовый чек отличается|онлайн-кассы\. вопрос",
    r"ндс в 2026 году: как работает|как селлеру маркетплейса подготовиться|ндс в 2025 году",
    r"зарплата гросс и нет|системы и формы оплаты труда|нужен ли вашему бизнесу зарплатный проект",
    r"как стать самозанятым|налог на профессиональный доход для самозанятых|чем ип отличается от самозанятого",
    r"как отвечать на жалобы|клиентский сервис: как улучшить|возвраты и невыкупы",
    r"как отследить эффективность рекламы|как разработать стратегию продвижения|что такое контекстная реклама",
    r"госзакупки: опровергаем мифы|как выбрать поставщика: основные|госзакупки: видеоинструкция",
    r"уведомление в роскомнадзор: кому|как составить согласие на обработку|защищаем персональные данные",
    r"виды кредитования для бизнеса|как оформить договор займа|как повысить шансы на одобрение кредита",
)
MID_ARTICLES = re.compile(r"лизинг или кредит|оформить договор займа|выдать другому ип заем|как привлечь инвестиции|"
                          r"как подать и заполнить заявку|ответственность за нарушение условий госзакупок|"
                          r"подготовиться к работе с ндс|ндс при импорте и экспорте|как восстановить ндс")
SNAPSHOT = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Scenario:
    group: str
    profile: Profile
    history: tuple[InteractionEvent, ...]
    targets: frozenset[str] = frozenset()
    avoid: frozenset[str] = frozenset()
    noise: tuple[InteractionEvent, ...] = ()
    shift: tuple[InteractionEvent, ...] = ()
    new_topic: frozenset[str] = frozenset()
    control: str = ""


def text_matches(material, pattern):
    return bool(re.search(pattern, _topic(material.title)))


def glossary_materials(reader):
    return tuple(Material(
        id=row["Id"], title=row["Title"], url="https://kurs.alfabank.ru/glossary/" + row["Url"],
        description=row["Description"] or "", format="термин", access="free", entity_type="glossary",
    ) for row in reader.load_rows("Glossary") if row.get("Published") and row.get("IsDeleted") != "t"
        and row.get("Title") and row.get("Url"))


def classify(material):
    title = _topic(material.title)
    experience = set(map(_topic, material.experience_levels))
    if material.entity_type == "glossary" or BEGINNER.search(title) or any(re.search(pattern, title) for pattern in BASIC_ARTICLES):
        return BASIC
    if ADVANCED_TITLE.search(title) or re.search(r"\b(?:для опытных|продвинутый уровень)\b", _topic(material.description)[:350]):
        return ADVANCED
    if experience.intersection({"начинающий", "без опыта"}):
        return BASIC
    if experience.intersection({"есть опыт", "опытный", "с опытом"}) or MID_ARTICLES.search(title):
        return INTERMEDIATE
    if material.entity_type in {"document", "calculator"}:
        return INTERMEDIATE
    return None


def order(items, seed):
    return sorted(items, key=lambda item: sample_key(seed, item.id))


def events(items, ages, *, kind="click", reference=SNAPSHOT):
    return tuple(InteractionEvent(item.id, kind, reference - timedelta(days=age)) for item, age in zip(items, ages))


def history_mix(groups, first, variation, *, primary_count=4, noise=False):
    size = len(groups)
    secondary = (first + 3 + variation) % size
    tertiary = (first + 6 + variation) % size
    if tertiary == first:
        tertiary = (tertiary + 1) % size
    main = order(groups[first], f"main:{first}:{variation}")[:primary_count]
    other = order(groups[secondary], f"other:{first}:{variation}")[:3]
    side = order(groups[tertiary], f"side:{first}:{variation}")[:2]
    earlier = events(other, (75, 58, 43)) + events(side, (68, 38))
    if noise:
        outlier = next(m for m in order(groups[(first + 1) % size], f"noise:{first}:{variation}")
                       if m.id not in {x.id for x in main + other + side})
        earlier += events((outlier,), (13,), kind="view")
    return tuple(sorted(earlier + events(main, (20, 9, 3, 1)[-primary_count:]), key=lambda event: event.occurred_at))


def theme_groups(catalog):
    groups = [[m for m in catalog if m.entity_type == "article" and text_matches(m, brief.titles)] for brief in BRIEFS]
    if any(len(group) < 4 for group in groups):
        raise ValueError(f"Недостаточно теоретических материалов в тематическом пуле: {[len(g) for g in groups]}")
    return groups


BRIDGE_TARGETS = {
    "Учёт на УСН": (r"книга .*усн", r"книга .*усн", r"уведомление о переходе", r"декларация усн", r"книга .*усн"),
    "Возвраты": (r"политика возвратов", r"акт возврата товара поставщику", r"акт о возврате денежных", r"претензия.*возврат", r"политика возвратов"),
}
ROBUSTNESS_HINTS = (
    "Я занимаюсь упрощёнкой и сверяю поступления из двух каналов.",
    "Сейчас наводим порядок с наличными по сменам.",
    "Хочу сверить налог в ценах поставщиков.",
    "В этом месяце проверяю начисления и выплаты сотрудникам.",
    "В фокусе разовые исполнители с чеками НПД.",
    "Сейчас разбираю претензии клиентов после покупки.",
    "В фокусе расходы на привлечение клиентов.",
    "Налаживаю работу с поставщиками для новой точки.",
    "Собираем контакты через форму и хотим оформить процесс.",
    "Готовлю план финансирования и сравниваю ставки.",
)


def bridge_cases(catalog, groups):
    for i, brief in enumerate(BRIEFS):
        for j, question in enumerate(brief.questions):
            patterns = BRIDGE_TARGETS.get(brief.theme)
            pattern = patterns[j] if patterns else brief.tools
            targets = frozenset(m.id for m in catalog if m.entity_type in {"document", "calculator"} and text_matches(m, pattern))
            if not targets:
                raise ValueError(f"Не найдены инструменты: {brief.theme}, {pattern}")
            yield Scenario(brief.theme, Profile(industry=brief.industry, goal=question),
                           history_mix(groups, i, j, primary_count=2 + j % 2, noise=True), targets)


def robustness_cases(catalog, groups):
    main_sets = [frozenset(m.id for m in catalog if text_matches(m, brief.titles)) for brief in BRIEFS]
    for i, brief in enumerate(BRIEFS):
        for j, question in enumerate(AMBIGUOUS_QUESTIONS):
            anchored = (i + j) % 2 == 0
            past = history_mix(groups, i, j, noise=True)
            group = "Устойчивый интерес и слабая подсказка" if anchored else "Конкурирующие интересы без подсказки"
            if anchored:
                recent = order(groups[i], f"established:{i}:{j}")[:7]
                context = tuple(event for event in past if event.material_id not in main_sets[i])[:3]
                history = context + events(recent, (16, 12, 9, 6, 4, 2, 1))
                question = f"{ROBUSTNESS_HINTS[i]} {question}"
            else:
                history = past
            seen = {event.material_id for event in history}
            possible = [(i + step) % len(BRIEFS) for step in (4, 5, 6, 7)]
            candidates = {topic: [m for m in groups[topic] if m.id not in main_sets[i] | seen] for topic in possible}
            topic = next(topic for topic in possible if len(candidates[topic]) >= 4)
            unrelated = candidates[topic]
            new = order(unrelated, f"shift:{i}:{j}")[:4]
            if len(new) != 4:
                raise ValueError(f"Недостаточно независимых материалов для смены темы: {brief.theme}.")
            noise = events(new[:1], (0,))
            shift = tuple(InteractionEvent(m.id, "click", SNAPSHOT + timedelta(hours=index))
                          for index, m in enumerate(new[1:1 + 2 + j % 2]))
            yield Scenario(group, Profile(industry=brief.industry, goal=question), history, main_sets[i],
                           noise=noise, shift=shift, new_topic=main_sets[topic] - main_sets[i])


def completions(reader, courses):
    real_lessons = {row["Id"] for row in reader.load_rows("Lessons") if row["IsDeleted"] != "t" and row["IsGroup"] != "t"}
    curriculum, activity, users = defaultdict(set), defaultdict(dict), defaultdict(list)
    for row in reader.load_rows("CourseLessonBindings"):
        if row["CourseId"] in courses and row["LessonId"] in real_lessons and row["IsDeleted"] != "t":
            curriculum[row["CourseId"]].add(row["LessonId"])
    for row in reader.load_rows("CourseUserLessonBindings"):
        if when := _time(row.get("Completed")):
            activity[row["CourseUserId"]][row["LessonId"]] = when
    for row in reader.load_rows("CourseUserBindings"):
        if row["IsDeleted"] != "t" and row.get("UserId") and row["CourseId"] in courses:
            users[row["UserId"]].append(row)
    completed = defaultdict(list)
    for user, bindings in users.items():
        for row in bindings:
            cid = row["CourseId"]
            progress = activity.get(row["Id"], {})
            if not curriculum[cid] or not curriculum[cid] <= progress.keys():
                continue
            finished = max(progress[lesson] for lesson in curriculum[cid])
            previous = []
            for other in bindings:
                started = _time(other.get("StudyingStartDate")) or _time(other.get("Created"))
                if other["CourseId"] != cid and started and started < finished:
                    previous.append(InteractionEvent(other["CourseId"], "click", started))
            previous.sort(key=lambda event: (event.occurred_at, event.material_id))
            completed[cid].append((sample_key(user), finished, tuple(previous[-6:])))
    return completed


def progression_cases(reader, catalog, glossary, groups, levels):
    combined = catalog + glossary
    complete = completions(reader, {m.id for m in catalog if m.entity_type == "course"})
    novice_cases = []
    for i, brief in enumerate(BRIEFS):
        introductory = [m for m in groups[i] if levels[m.id] == BASIC]
        if not introductory:
            continue
        eligible = frozenset(m.id for m in combined if text_matches(m, brief.titles) and levels[m.id] in {BASIC, INTERMEDIATE})
        for j, question in enumerate(brief.questions[:3]):
            past = history_mix(groups, i, j, primary_count=2, noise=True)
            base = events(order(introductory, f"intro:{i}:{j}")[:3], (4, 2, 1))
            novice_cases.append(Scenario("Первые шаги", Profile(industry=brief.industry, experience="начинающий", goal=question),
                                         past[:-2] + base, eligible))
    yield from novice_cases

    seen_graduates = set()
    for index, (course_pattern, topic_pattern, goal) in enumerate(GRADUATE_TOPICS):
        course = next(m for m in catalog if m.entity_type == "course" and text_matches(m, course_pattern))
        target = frozenset(m.id for m in combined if m.id != course.id and text_matches(m, topic_pattern)
                           and levels[m.id] in {INTERMEDIATE, ADVANCED})
        avoid = frozenset(m.id for m in combined if m.entity_type == "glossary" and text_matches(m, topic_pattern))
        cohort = sorted(complete[course.id], key=lambda row: row[0])
        if len(cohort) < 5 or not avoid or not target:
            raise ValueError(f"Неполная контрольная группа завершивших {course.title}: {len(cohort)}, {len(avoid)}, {len(target)}.")
        chosen = {}
        for user_key, finished, previous in cohort:
            if user_key not in seen_graduates:
                chosen[user_key] = finished, previous
                seen_graduates.add(user_key)
            if len(chosen) == 5:
                break
        for j, (finished, previous) in enumerate(chosen.values()):
            background = history_mix(groups, index, j, primary_count=2, noise=True)[:3]
            mixed = tuple(InteractionEvent(event.material_id, event.kind, finished - timedelta(days=70 - n * 8))
                          for n, event in enumerate(background))
            history = tuple(sorted(mixed + previous, key=lambda event: event.occurred_at))
            history += (InteractionEvent(course.id, "complete", finished),)
            yield Scenario("После полного прохождения", Profile(experience="опытный", goal=goal), history, target, avoid)


def freshness_cases(reader, catalog, groups, topics):
    dates = publication_dates(reader)
    dated = sorted((m for m in catalog if m.id in dates), key=lambda m: (dates[m.id], m.id))
    authors = read_authors(reader)
    for row in reader.load_rows("PodcastExperts"):
        if row.get("PodcastId") and row.get("Id"):
            authors.setdefault(row["PodcastId"], set()).add(row["Id"])
    by_author = defaultdict(list)
    for m in dated:
        for author in authors.get(m.id, ()):
            by_author[author].append(m)
    ratings = Counter(row["EntityId"] for row in reader.load_rows("UserRatings") if row.get("IsDeleted") != "t")
    newest = dated[-round(len(dated) * .125):]
    recent_cutoff = dates[dated[round(len(dated) * .65)].id]
    newcomers = {author for author, items in by_author.items() if dates[items[0].id] >= recent_cutoff
                 and sum(ratings[item.id] for item in items) <= 2}
    used = set()
    controls = defaultdict(list)
    for m in dated:
        controls[m.entity_type].append(m)
    for name, pool in (("Новые эксперты", [m for m in dated if authors.get(m.id, set()) & newcomers]),
                       ("Свежие публикации", newest)):
        count = 0
        for material in order(pool, name):
            if material.id in used:
                continue
            tags = topics.get(material.id, set())
            if not tags:
                continue
            options = [(len(tags & topics.get(old.id, set())), old) for old in controls[material.entity_type]
                       if dates[old.id] <= dates[material.id] - timedelta(days=90)]
            options = [(overlap, old) for overlap, old in options if overlap]
            if not options:
                continue
            _, old = max(options, key=lambda pair: (pair[0], pair[1].popularity or 0, -dates[pair[1].id].timestamp(), pair[1].id))
            idx = max(range(len(groups)), key=lambda index: len(tags & set().union(*(topics.get(m.id, set()) for m in groups[index]))))
            history = history_mix(groups, idx, count % 5, primary_count=3, noise=True)
            selected = sorted(tags, key=lambda tag: (-len(tag), tag))[:2]
            themes = " и ".join(selected)
            prompt = (
                f"На следующий квартал хочу пересмотреть подход. Что стоит проверить по теме «{themes}», прежде чем менять процесс?",
                f"Помогите подготовить обсуждение с командой. По теме «{themes}» нам нужен следующий применимый шаг.",
                f"Как сделать рабочий план по теме «{themes}», если пока видны только отдельные проблемы?",
                f"Не уверен, с какой части начать. Что поможет сопоставить варианты по теме «{themes}» до запуска?",
                f"Нужно понять, что из темы «{themes}» пригодится для роста в следующем сезоне. Что изучить сначала?",
            )[count % 5]
            count += 1
            used.add(material.id)
            yield Scenario(name, Profile(goal=prompt, learning_topic=selected[0]), history,
                           frozenset({material.id}), control=old.id)
            if count == SUITE_SIZE // 2:
                break
