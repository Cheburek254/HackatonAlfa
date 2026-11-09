"""Извлечение и нормализация каталога из PostgreSQL-бэкапа Альфа-Курса."""

import json
import re
import sqlite3
import struct
import zlib
from collections import defaultdict
from pathlib import Path

BASE_URL = "https://kurs.alfabank.ru"


def _read_int(buf: bytes, pos: int) -> tuple[int, int]:
    sign = buf[pos]
    val = struct.unpack("<I", buf[pos + 1 : pos + 5])[0]
    return (-val if sign else val), pos + 5


def _read_str(buf: bytes, pos: int) -> tuple[str | None, int]:
    length, pos = _read_int(buf, pos)
    if length < 0:
        return None, pos
    s = buf[pos : pos + length].decode("utf-8", "replace")
    return s, pos + length


def _read_offset(buf: bytes, pos: int) -> tuple[int, int, int]:
    state = buf[pos]
    val = struct.unpack("<Q", buf[pos + 1 : pos + 9])[0]
    return state, val, pos + 9


class PgDumpReader:
    """Читает таблицы напрямую из файла PostgreSQL custom backup (PGDMP)."""

    def __init__(self, backup_path: Path) -> None:
        self.data = backup_path.read_bytes()
        self.toc_tables: dict[str, tuple[list[str], int]] = {}
        self._parse_toc()

    def _parse_toc(self) -> None:
        pattern = re.compile(rb'COPY public\.("?[A-Za-z0-9_]+"?)\s*\((.*?)\)\s*FROM stdin;\n')
        for m in pattern.finditer(self.data[:220000]):
            table_name = m.group(1).decode("utf-8").strip('"')
            cols = [c.strip().strip('"') for c in m.group(2).decode("utf-8").split(",")]
            pos = m.end()
            for _ in range(5):
                _, pos = _read_str(self.data, pos)
            while True:
                dep, pos = _read_str(self.data, pos)
                if dep is None:
                    break
            _, offset, pos = _read_offset(self.data, pos)
            self.toc_tables[table_name] = (cols, offset)

    def load_rows(self, table_name: str) -> list[dict[str, str | None]]:
        if table_name not in self.toc_tables:
            return []
        cols, pos = self.toc_tables[table_name]
        pos += 1  # blk_type
        _, pos = _read_int(self.data, pos)  # dump_id
        chunks = []
        dobj = zlib.decompressobj()
        while True:
            chunk_len, pos = _read_int(self.data, pos)
            if chunk_len <= 0:
                break
            chunks.append(dobj.decompress(self.data[pos : pos + chunk_len]))
            pos += chunk_len
            if dobj.eof:
                break
        text = b"".join(chunks).decode("utf-8", "replace")
        rows = []
        for line in text.splitlines():
            if line == r"\." or not line:
                continue
            vals = [
                None
                if v == r"\N"
                else v.replace(r"\n", "\n").replace(r"\r", "\r").replace(r"\t", "\t")
                for v in line.split("\t")
            ]
            rows.append(dict(zip(cols, vals)))
        return rows


def _unique(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    res: list[str] = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            res.append(x)
    return res


def _infer_ui_topics(raw_topics: list[str], title: str, desc: str) -> list[str]:
    """Добавляет канонические темы из UI профиля (запуск бизнеса, маркетинг, продажи, финансы, операционная работа)."""
    combined = " ".join(raw_topics + [title, desc]).casefold().replace("ё", "е")
    canonical: list[str] = []
    if any(k in combined for k in ("старт", "запуск", "открыт", "регистрац", "идеи для бизнеса", "выбрать нишу", "выбор ниши", "самозанят", "начинающ", "франшиз", "франчайзинг")):
        canonical.append("запуск бизнеса")
    if any(k in combined for k in ("маркетинг", "продвижен", "реклам", "бренд", "целевая аудитория", "контент", "соцсет", "трафик", "смм", "smm", "яндекс")):
        canonical.append("маркетинг")
    if any(k in combined for k in ("продаж", "клиент", "переговор", "воронк", "лид", "маркетплейс", "маркeтплейс", "wildberries", "ozon", "озон", "чеки", "покупател", "госзакуп", "тендер")):
        canonical.append("продажи")
    if any(k in combined for k in ("финанс", "налог", "бухгалтер", "учет", "ндс", "ндфл", "усн", "бюджет", "прибыл", "рентабел", "касс", "эквайринг", "кредит", "взнос", "отчетност", "115-фз", "деньги", "зарплат")):
        canonical.append("финансы")
    if any(k in combined for k in ("операцион", "команд", "сотрудник", "найм", "наем", "увольнен", "мотивац", "управлен", "процесс", "планирован", "автоматизац", "ии ", "нейросет", "персданн", "договор", "безопасност", "проверк", "акт ", "приказ")):
        canonical.append("операционная работа")
    return _unique(canonical + raw_topics)


def _infer_stages(topics: list[str], title: str, desc: str, exps: list[str]) -> list[str]:
    """Определяет стадии бизнеса (идея, запуск, работающий бизнес, рост) по темам и описанию."""
    combined = " ".join(topics + [title, desc]).casefold().replace("ё", "е")
    stages: list[str] = []
    if any(k in combined for k in ("идея", "идеи", "нишу", "ниши", "выбрать бизнес", "генерация идей", "с чего начать")):
        stages.append("идея")
    if any(k in combined for k in ("старт", "запуск", "открыть", "регистрац", "перв", "начинающ", "выбор системы налогообложения")):
        stages.append("запуск")
    if any(k in combined for k in ("масштабирован", "рост", "франчайзинг", "франшиз", "за рубежом", "вэд", "импорт", "экспорт", "инвестиц", "привлечение финансирования", "повторн", "удержан", "стратег", "лидерств")):
        stages.append("рост")
    if any(k in combined for k in ("учет", "отчетност", "сотрудник", "увольнен", "касс", "договор", "акт ", "проверк", "115-фз", "ндс", "ндфл", "зарплат", "мотивац", "операцион")):
        stages.append("работающий бизнес")
    if not stages:
        if "начинающий" in exps:
            stages.extend(["запуск", "работающий бизнес"])
        elif "опытный" in exps:
            stages.extend(["работающий бизнес", "рост"])
        else:
            stages.append("работающий бизнес")
    return _unique(stages)


def _infer_industries(raw_inds: list[str], topics: list[str], title: str, desc: str) -> tuple[list[str], list[str]]:
    """Нормализует сферы бизнеса и уровни опыта под значения UI и каталога."""
    exps: list[str] = []
    inds: list[str] = []
    for item in raw_inds:
        if item == "Без опыта":
            exps.extend(["начинающий", "Без опыта"])
        elif item == "С опытом":
            exps.extend(["есть опыт", "опытный", "С опытом"])
        elif item == "Розница":
            inds.extend(["розница", "Розница"])
        elif item == "Услуги":
            inds.extend(["услуги", "Услуги"])
        elif item in ("ECom", "Marketplace"):
            inds.extend(["интернет-торговля", "розница", item])
        elif item in ("HoReСa", "HoReCa"):
            inds.extend(["общепит", "HoReCa"])
        else:
            inds.append(item)

    combined = " ".join(topics + [title, desc]).casefold().replace("ё", "е")
    keyword_map = {
        "розница": ("розниц", "магазин", "торговл", "ретейл", "ритейл", "товар"),
        "интернет-торговля": ("маркетплейс", "маркeтплейс", "интернет-магазин", "онлайн-торгов", "wildberries", "ozon", "озон", "ecom"),
        "услуги": ("услуг", "сервис", "салон", "консалтинг", "агентств"),
        "общепит": ("horeca", "хорека", "общепит", "кафе", "ресторан", "кофейн", "бар "),
        "производство": ("производств", "цех", "фабрик", "завод", "маркировк"),
        "строительство и недвижимость": ("строител", "недвижимост", "аренд", "помещен", "ремонт"),
        "информационные технологии": (" ит ", "it-", "разработк", "программ", "нейросет", "ии ", "искусственн", "автоматизац", "сайт"),
        "логистика": ("логистик", "доставк", "перевоз", "склад", "транспорт", "тамож", "импорт"),
        "образование": ("образован", "обучен", "школ", "курс"),
        "туризм": ("туризм", "отел", "гостиниц", "турбизнес"),
    }
    for ui_ind, kw_list in keyword_map.items():
        if any(k in combined for k in kw_list):
            inds.append(ui_ind)

    if not exps:
        if any(k in combined for k in ("с нуля", "для начинающих", "как открыть", "первые шаги", "простыми словами", "базовый")):
            exps.append("начинающий")
        elif any(k in combined for k in ("масштабирован", "продвинут", "оптимизац", "холдинг", "вэд", "аннулировать")):
            exps.extend(["есть опыт", "опытный"])

    return _unique(exps), _unique(inds)


def build_normalized_catalog(backup_path: Path, out_dir: Path) -> dict[str, int]:
    """Извлекает из .backup нормализованный каталог, популярность и связи RecommendedItems."""
    reader = PgDumpReader(backup_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    statuses = {r["Id"]: r["Title"] for r in reader.load_rows("Statuses") if r.get("Id") and r.get("Title")}
    course_types = {r["Id"]: r["Title"] for r in reader.load_rows("CourseTypes") if r.get("Id") and r.get("Title")}
    doc_types = {r["Id"]: r["Title"] for r in reader.load_rows("DocumentTypes") if r.get("Id") and r.get("Title")}
    art_subjects = {r["Id"]: r["Title"] for r in reader.load_rows("ArticleSubjects") if r.get("Id") and r.get("Title")}
    topics_dict = {r["Id"]: r["Title"].strip() for r in reader.load_rows("Topics") if r.get("Id") and r.get("Title")}
    tags_dict = {r["Id"]: r["Title"].strip() for r in reader.load_rows("Tags") if r.get("Id") and r.get("Title")}
    industries_dict = {r["Id"]: r["Title"].strip() for r in reader.load_rows("Industries") if r.get("Id") and r.get("Title")}
    authors_dict = {r["Id"]: r["Title"].strip() for r in reader.load_rows("Authors") if r.get("Id") and r.get("Title")}

    # Популярность из CourseUserBindings и UserRatings
    course_starts: dict[str, int] = defaultdict(int)
    course_completions: dict[str, int] = defaultdict(int)
    for r in reader.load_rows("CourseUserBindings"):
        if r.get("IsDeleted") == "t":
            continue
        cid = r.get("CourseId")
        if cid:
            course_starts[cid] += 1
            if r.get("IsCompleted") == "t":
                course_completions[cid] += 1

    entity_ratings: dict[str, list[int]] = defaultdict(list)
    for r in reader.load_rows("UserRatings"):
        if r.get("IsDeleted") == "t":
            continue
        eid = r.get("EntityId")
        val = r.get("Value")
        if eid and val and val.isdigit():
            entity_ratings[eid].append(int(val))

    article_snippets: dict[str, str] = {}
    for r in reader.load_rows("ArticleEmbedItems"):
        aid = r.get("ArticleId")
        if aid and aid not in article_snippets and r.get("HtmlContent"):
            clean = re.sub(r"<[^>]+>", " ", r["HtmlContent"])
            clean = re.sub(r"\s+", " ", clean).strip()
            if len(clean) > 30:
                article_snippets[aid] = clean[:240].rstrip(" ,.;:") + "..."

    cs_rows = reader.load_rows("ContentSettings")
    cs_to_entity = {r["Id"]: r["EntityId"] for r in cs_rows if r.get("IsDeleted") != "t" and r.get("Id") and r.get("EntityId")}

    entity_topics: dict[str, list[str]] = defaultdict(list)
    for r in reader.load_rows("ContentSettingsTopics"):
        ent_id = cs_to_entity.get(r.get("ContentSettingsId") or "")
        t_title = topics_dict.get(r.get("TopicId") or "")
        if ent_id and t_title:
            entity_topics[ent_id].append(t_title)

    entity_tags: dict[str, list[str]] = defaultdict(list)
    for r in reader.load_rows("ContentSettingsTags"):
        ent_id = cs_to_entity.get(r.get("ContentSettingsId") or "")
        t_title = tags_dict.get(r.get("TagId") or "")
        if ent_id and t_title:
            entity_tags[ent_id].append(t_title)

    entity_industries: dict[str, list[str]] = defaultdict(list)
    for r in reader.load_rows("ContentSettingsIndustries"):
        ent_id = cs_to_entity.get(r.get("ContentSettingsId") or "")
        i_title = industries_dict.get(r.get("IndustryId") or "")
        if ent_id and i_title:
            entity_industries[ent_id].append(i_title)

    entity_authors: dict[str, list[str]] = defaultdict(list)
    for table, key in [
        ("CourseAuthorBindings", "CourseId"),
        ("ArticleAuthorBindings", "ArticleId"),
        ("WebinarAuthorBindings", "WebinarId"),
        ("PodcastAuthorBindings", "PodcastId"),
    ]:
        for r in reader.load_rows(table):
            ent_id = r.get(key)
            a_name = authors_dict.get(r.get("AuthorId") or "")
            if ent_id and a_name and a_name not in entity_authors[ent_id]:
                entity_authors[ent_id].append(a_name)

    for r in reader.load_rows("WebinarAuthorsLegacy"):
        ent_id = r.get("WebinarId")
        a_name = (r.get("Title") or "").strip()
        if ent_id and a_name and a_name not in entity_authors[ent_id]:
            entity_authors[ent_id].append(a_name)

    for r in reader.load_rows("PodcastExperts"):
        ent_id = r.get("PodcastId")
        a_name = (r.get("Title") or "").strip()
        if ent_id and a_name and a_name not in entity_authors[ent_id]:
            entity_authors[ent_id].append(a_name)

    course_tariffs: dict[str, list[int]] = defaultdict(list)
    for r in reader.load_rows("CourseTariffBindings"):
        cid = r.get("CourseId")
        cost = int(r["Cost"]) if r.get("Cost") and str(r["Cost"]).isdigit() else 0
        if cid and cost > 0:
            course_tariffs[cid].append(cost)

    def calc_pop(mid: str, table_rating: str | None = None) -> tuple[int, int, int, float | None, float]:
        starts = course_starts.get(mid, 0)
        comps = course_completions.get(mid, 0)
        r_list = entity_ratings.get(mid, [])
        r_count = len(r_list)
        if r_list:
            avg_r = round(sum(r_list) / r_count, 2)
        elif table_rating and str(table_rating).replace(".", "", 1).isdigit():
            avg_r = round(float(table_rating), 2)
        else:
            avg_r = None
        pop_score = round(starts * 1.0 + comps * 2.0 + r_count * 3.0 + (avg_r or 0.0), 2)
        return starts, comps, r_count, avg_r, pop_score

    materials: list[dict] = []

    # 1. Courses
    for r in reader.load_rows("Courses"):
        if r.get("IsDeleted") == "t":
            continue
        status = statuses.get(r.get("StatusId") or "", "")
        if status in ("Черновик", "Архив", "Снято с публикации"):
            continue
        mid = r["Id"]
        if not mid:
            continue
        cost_val = int(r["Cost"]) if r.get("Cost") and str(r["Cost"]).lstrip("-").isdigit() else None
        ctype = course_types.get(r.get("TypeId") or "", "Открытый")
        if (cost_val and cost_val > 0) or ctype in ("Платный", "По подписке") or mid in course_tariffs:
            access = "paid"
            if not cost_val and mid in course_tariffs:
                cost_val = min(course_tariffs[mid])
        else:
            access = "free"
        slug = (r.get("Url") or mid).strip("/")
        url = slug if slug.startswith("http") else f"{BASE_URL}/courses/{slug}"
        title = (r.get("Title") or "").strip()
        desc = (r.get("Description") or r.get("DescriptionTag") or "").strip()
        raw_topics = _unique(entity_topics.get(mid, []) + entity_tags.get(mid, []))
        topics = _infer_ui_topics(raw_topics, title, desc)
        exps, inds = _infer_industries(entity_industries.get(mid, []), topics, title, desc)
        stages = _infer_stages(topics, title, desc, exps)
        starts, comps, r_count, avg_r, pop_score = calc_pop(mid, r.get("Rating"))
        materials.append({
            "id": mid,
            "entity_type": "course",
            "format": "курс",
            "format_detail": f"Курс ({ctype})",
            "title": title,
            "description": desc,
            "url": url,
            "access": access,
            "cost": cost_val,
            "authors": entity_authors.get(mid, []),
            "seller": "Партнёр Альфа-Курса" if r.get("IsPartner") == "t" else "Альфа-Курс",
            "topics": topics,
            "industries": inds,
            "experience_levels": exps,
            "stages": stages,
            "duration": r.get("Duration"),
            "lesson_count": int(r["LessonQuantity"]) if r.get("LessonQuantity") and str(r["LessonQuantity"]).isdigit() else None,
            "views_count": starts,
            "completions_count": comps,
            "ratings_count": r_count,
            "avg_rating": avg_r,
            "popularity_score": pop_score,
        })

    # 2. Articles
    for r in reader.load_rows("Articles"):
        if r.get("IsDeleted") == "t":
            continue
        status = statuses.get(r.get("StatusId") or "", "")
        if status in ("Черновик", "Архив", "Снято с публикации"):
            continue
        mid = r["Id"]
        if not mid:
            continue
        subj = art_subjects.get(r.get("SubjectId") or "", "")
        ctype = course_types.get(r.get("TypeId") or "", "Открытый")
        access = "paid" if (r.get("IsClosed") == "t" or ctype in ("Платный", "По подписке")) else "free"
        slug = (r.get("Url") or mid).strip("/")
        url = slug if slug.startswith("http") else f"{BASE_URL}/articles/{slug}"
        title = (r.get("Title") or "").strip()
        desc = (r.get("Description") or r.get("CardDescription") or r.get("DescriptionTag") or article_snippets.get(mid) or "").strip()
        raw_topics = _unique(([subj] if subj else []) + entity_topics.get(mid, []) + entity_tags.get(mid, []))
        topics = _infer_ui_topics(raw_topics, title, desc)
        exps, inds = _infer_industries(entity_industries.get(mid, []), topics, title, desc)
        stages = _infer_stages(topics, title, desc, exps)
        authors = list(entity_authors.get(mid, []))
        if r.get("AuthorId") and authors_dict.get(r["AuthorId"] or ""):
            a_direct = authors_dict[r["AuthorId"] or ""]
            if a_direct not in authors:
                authors.insert(0, a_direct)
        fmt = "чек-лист" if any(k in title.casefold() or k in desc.casefold() for k in ("чек-лист", "чеклист", "памятк", "пошагов")) else "статья"
        starts, comps, r_count, avg_r, pop_score = calc_pop(mid)
        materials.append({
            "id": mid,
            "entity_type": "article",
            "format": fmt,
            "format_detail": "Чек-лист / Статья" if fmt == "чек-лист" else "Статья",
            "title": title,
            "description": desc,
            "url": url,
            "access": access,
            "cost": None,
            "authors": authors,
            "seller": "Альфа-Курс",
            "topics": topics,
            "industries": inds,
            "experience_levels": exps,
            "stages": stages,
            "duration": r.get("Duration"),
            "lesson_count": None,
            "views_count": starts,
            "completions_count": comps,
            "ratings_count": r_count,
            "avg_rating": avg_r,
            "popularity_score": pop_score,
        })

    # 3. Documents
    for r in reader.load_rows("Documents"):
        if r.get("IsDeleted") == "t":
            continue
        status = statuses.get(r.get("StatusId") or "", "")
        if status in ("Черновик", "Архив", "Снято с публикации"):
            continue
        mid = r["Id"]
        if not mid:
            continue
        dtype = doc_types.get(r.get("TypeId") or "", "")
        slug = (r.get("Url") or mid).strip("/")
        url = slug if slug.startswith("http") else f"{BASE_URL}/documents/{slug}"
        title = (r.get("Title") or "").strip()
        desc = (r.get("Description") or r.get("DescriptionTag") or "").strip()
        raw_topics = _unique(([dtype] if dtype else []) + entity_topics.get(mid, []) + entity_tags.get(mid, []))
        topics = _infer_ui_topics(raw_topics, title, desc)
        exps, inds = _infer_industries(entity_industries.get(mid, []), topics, title, desc)
        stages = _infer_stages(topics, title, desc, exps)
        fmt = "чек-лист" if "чек-лист" in title.casefold() or "памятка" in title.casefold() else "шаблон"
        starts, comps, r_count, avg_r, pop_score = calc_pop(mid)
        materials.append({
            "id": mid,
            "entity_type": "document",
            "format": fmt,
            "format_detail": f"Шаблон ({r.get('Extension') or 'документ'})",
            "title": title,
            "description": desc,
            "url": url,
            "access": "free",
            "cost": None,
            "authors": entity_authors.get(mid, []),
            "seller": "Альфа-Курс",
            "topics": topics,
            "industries": inds,
            "experience_levels": exps,
            "stages": stages,
            "duration": None,
            "lesson_count": None,
            "views_count": starts,
            "completions_count": comps,
            "ratings_count": r_count,
            "avg_rating": avg_r,
            "popularity_score": pop_score,
        })

    # 4. Webinars
    for r in reader.load_rows("Webinars"):
        if r.get("IsDeleted") == "t":
            continue
        status = statuses.get(r.get("StatusId") or "", "")
        if status in ("Черновик", "Архив", "Снято с публикации"):
            continue
        mid = r["Id"]
        if not mid:
            continue
        ctype = course_types.get(r.get("TypeId") or "", "Открытый")
        access = "paid" if (r.get("IsClosed") == "t" or ctype in ("Платный", "По подписке")) else "free"
        slug = (r.get("Url") or mid).strip("/")
        url = slug if slug.startswith("http") else f"{BASE_URL}/webinars/{slug}"
        title = (r.get("Title") or "").strip()
        desc = (r.get("Description") or r.get("DescriptionExt") or r.get("DescriptionTag") or "").strip()
        raw_topics = _unique(entity_topics.get(mid, []) + entity_tags.get(mid, []))
        topics = _infer_ui_topics(raw_topics, title, desc)
        exps, inds = _infer_industries(entity_industries.get(mid, []), topics, title, desc)
        stages = _infer_stages(topics, title, desc, exps)
        starts, comps, r_count, avg_r, pop_score = calc_pop(mid, r.get("Rating"))
        materials.append({
            "id": mid,
            "entity_type": "webinar",
            "format": "вебинар",
            "format_detail": "Вебинар",
            "title": title,
            "description": desc,
            "url": url,
            "access": access,
            "cost": None,
            "authors": entity_authors.get(mid, []),
            "seller": "Альфа-Курс",
            "topics": topics,
            "industries": inds,
            "experience_levels": exps,
            "stages": stages,
            "duration": r.get("Duration"),
            "lesson_count": None,
            "views_count": starts,
            "completions_count": comps,
            "ratings_count": r_count,
            "avg_rating": avg_r,
            "popularity_score": pop_score,
        })

    # 5. Podcasts
    for r in reader.load_rows("Podcasts"):
        if r.get("IsDeleted") == "t":
            continue
        status = statuses.get(r.get("StatusId") or "", "")
        if status in ("Черновик", "Архив", "Снято с публикации"):
            continue
        mid = r["Id"]
        if not mid:
            continue
        cost_val = int(r["Cost"]) if r.get("Cost") and str(r["Cost"]).lstrip("-").isdigit() else None
        ctype = course_types.get(r.get("TypeId") or "", "Открытый")
        access = "paid" if ((cost_val and cost_val > 0) or r.get("IsClosed") == "t" or ctype in ("Платный", "По подписке")) else "free"
        slug = (r.get("Url") or mid).strip("/")
        url = slug if slug.startswith("http") else f"{BASE_URL}/podcasts/{slug}"
        title = (r.get("Title") or "").strip()
        desc = (r.get("Description") or r.get("DescriptionExt") or "").strip()
        raw_topics = _unique(entity_topics.get(mid, []) + entity_tags.get(mid, []))
        topics = _infer_ui_topics(raw_topics, title, desc)
        exps, inds = _infer_industries(entity_industries.get(mid, []), topics, title, desc)
        stages = _infer_stages(topics, title, desc, exps)
        starts, comps, r_count, avg_r, pop_score = calc_pop(mid, r.get("Rating"))
        materials.append({
            "id": mid,
            "entity_type": "podcast",
            "format": "подкаст",
            "format_detail": "Подкаст",
            "title": title,
            "description": desc,
            "url": url,
            "access": access,
            "cost": cost_val,
            "authors": entity_authors.get(mid, []),
            "seller": "Альфа-Курс",
            "topics": topics,
            "industries": inds,
            "experience_levels": exps,
            "stages": stages,
            "duration": r.get("Duration"),
            "lesson_count": None,
            "views_count": starts,
            "completions_count": comps,
            "ratings_count": r_count,
            "avg_rating": avg_r,
            "popularity_score": pop_score,
        })

    # 6. Calculators
    for r in reader.load_rows("Calculators"):
        if r.get("IsDeleted") == "t":
            continue
        mid = r["Id"]
        if not mid:
            continue
        slug = (r.get("Url") or mid).strip("/")
        url = slug if slug.startswith("http") else f"{BASE_URL}/calculators/{slug}"
        title = (r.get("Title") or "").strip()
        desc = (r.get("Description") or r.get("DescriptionTag") or f"Интерактивный инструмент «{title}» для расчётов в бизнесе.").strip()
        raw_topics = _unique(["финансы", "расчёты"] + entity_topics.get(mid, []) + entity_tags.get(mid, []))
        topics = _infer_ui_topics(raw_topics, title, desc)
        exps, inds = _infer_industries(entity_industries.get(mid, []), topics, title, desc)
        stages = _infer_stages(topics, title, desc, exps)
        starts, comps, r_count, avg_r, pop_score = calc_pop(mid)
        materials.append({
            "id": mid,
            "entity_type": "calculator",
            "format": "шаблон",
            "format_detail": "Калькулятор",
            "title": title,
            "description": desc,
            "url": url,
            "access": "free",
            "cost": None,
            "authors": [],
            "seller": "Альфа-Курс",
            "topics": topics,
            "industries": inds,
            "experience_levels": exps,
            "stages": stages,
            "duration": None,
            "lesson_count": None,
            "views_count": starts,
            "completions_count": comps,
            "ratings_count": r_count,
            "avg_rating": avg_r,
            "popularity_score": pop_score,
        })

    materials.sort(key=lambda m: m["popularity_score"], reverse=True)
    valid_ids = {m["id"] for m in materials}

    # Связи RecommendedItems (Id/CourseId <-> ItemId)
    rec_links: dict[str, list[str]] = defaultdict(list)
    for r in reader.load_rows("RecommendedItems"):
        src_id = r.get("CourseId") or r.get("Id")
        iid = r.get("ItemId")
        if src_id in valid_ids and iid in valid_ids and src_id != iid:
            if iid not in rec_links[src_id]:
                rec_links[src_id].append(iid)
            if src_id not in rec_links[iid]:
                rec_links[iid].append(src_id)

    (out_dir / "recommended_links.json").write_text(
        json.dumps(rec_links, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "catalog_slim.json").write_text(
        json.dumps(materials, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    norm_materials = [
        {
            "id": m["id"],
            "title": m["title"],
            "url": m["url"],
            "description": m["description"],
            "topics": m["topics"],
            "format": m["format"],
            "access": m["access"],
            "stages": m["stages"],
            "industries": m["industries"],
            "experience_levels": m["experience_levels"],
            "popularity": m["popularity_score"],
            "rating": m["avg_rating"],
            "cost": m["cost"],
        }
        for m in materials
    ]
    (out_dir / "catalog.norm.json").write_text(
        json.dumps(norm_materials, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    sqlite_path = out_dir / "catalog_slim.sqlite"
    if sqlite_path.exists():
        sqlite_path.unlink()
    conn = sqlite3.connect(sqlite_path)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE materials (
        id TEXT PRIMARY KEY,
        entity_type TEXT,
        format TEXT,
        format_detail TEXT,
        title TEXT,
        description TEXT,
        url TEXT,
        access TEXT,
        cost INTEGER,
        seller TEXT,
        authors_json TEXT,
        topics_json TEXT,
        industries_json TEXT,
        experience_levels_json TEXT,
        stages_json TEXT,
        duration TEXT,
        lesson_count INTEGER,
        views_count INTEGER,
        completions_count INTEGER,
        ratings_count INTEGER,
        avg_rating REAL,
        popularity_score REAL
    )
    """)
    for m in materials:
        cur.execute(
            "INSERT INTO materials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m["id"],
                m["entity_type"],
                m["format"],
                m["format_detail"],
                m["title"],
                m["description"],
                m["url"],
                m["access"],
                m["cost"],
                m["seller"],
                json.dumps(m["authors"], ensure_ascii=False),
                json.dumps(m["topics"], ensure_ascii=False),
                json.dumps(m["industries"], ensure_ascii=False),
                json.dumps(m["experience_levels"], ensure_ascii=False),
                json.dumps(m["stages"], ensure_ascii=False),
                m["duration"],
                m["lesson_count"],
                m["views_count"],
                m["completions_count"],
                m["ratings_count"],
                m["avg_rating"],
                m["popularity_score"],
            ),
        )
    conn.commit()
    conn.close()
    return {"total": len(materials), "links": sum(len(v) for v in rec_links.values())}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2] / "data"
    stats = build_normalized_catalog(root / "alfa_course_hackathon.backup", root)
    print(f"Нормализация завершена: {stats['total']} материалов, {stats['links']} связей.")
