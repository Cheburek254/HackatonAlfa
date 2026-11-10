"""Экспериментальный BM25 и объединение рангов для офлайн-оценки."""

import re
from collections import Counter, defaultdict
from functools import cache
from math import log1p

from app.catalog import material_format
from app.contracts import Recommendation, RecommendationResult
from app.recommender.engine import STOPWORDS, _normalize_text, _stem_ru

FIELD_WEIGHTS = (3, 1, 1)
K1, B = 1.2, 0.75
FUSION_DEPTH, RRF_OFFSET = 50, 20

FORMAT_WORDS = {
    "курс": r"курс(?:а|у|е|ы|ов)?",
    "вебинар": r"вебинар(?:а|у|е|ы|ов)?",
    "подкаст": r"подкаст(?:а|у|е|ы|ов)?",
    "калькулятор": r"калькулятор(?:а|у|е|ы|ов)?",
    "чек-лист": r"чек ?лист(?:а|у|е|ы|ов)?",
    "шаблон": r"(?:шаблон(?:а|у|е|ы|ов)?|бланк(?:а|у|е|и|ов)?|образец)",
    "статья": r"стать(?:я|ю|и|е|ей)",
}
FORMAT_TERMS = "|".join(f"(?P<f{i}>{pattern})" for i, pattern in enumerate(FORMAT_WORDS.values()))
MODIFIERS = r"(?:(?:именно|онлайн|практическ\w*|пошагов\w*|коротк\w*|готов\w*|редактируем\w*|бесплатн\w*|платн\w*|пуст\w*|скачиваем\w*|любой|любую)\s+){0,4}"
FORMAT_REQUEST = re.compile(
    r"(?:^|\b(?:только|нуж\w*|ищу|посовет\w*|подойдет|дайте|дайте мне|хочу|хотелось бы|хочу пройти|хочу послушать|готов купить|в формате|подберите|дайте скачать|скачать|пришлите)\s+)"
    + f"(?P<modifiers>{MODIFIERS})(?:{FORMAT_TERMS})\\b"
)
MAX_BUDGET = re.compile(r"(?:до|не дороже|не больше|не более|в пределах)\s*(\d[\d\s]*)\s*(?:₽|руб\w*|р\.?(?=\s|$|[,.!?]))", re.I)

def constraints(goal):
    norm = _normalize_text(goal or "")
    match = FORMAT_REQUEST.search(norm)
    requested_format = None
    modifiers = ()
    if match:
        requested_format = next(fmt for i, fmt in enumerate(FORMAT_WORDS) if match.group(f"f{i}"))
        modifiers = match.group("modifiers").split()
    # Отмечаем цену самого материала, когда она названа в запросе к формату или явно задана для подборки.
    budget_match = MAX_BUDGET.search(goal or "")
    budget = int(re.sub(r"\s+", "", budget_match[1])) if budget_match else None
    only_free = (
        any(word.startswith("бесплатн") for word in modifiers)
        or bool(re.search(r"\bтолько бесплатно\b|\bтолько бесплатн\w*\s+материал\w*\b", norm))
        or budget == 0
    )
    only_paid = any(word.startswith("платн") for word in modifiers) or bool(
        re.search(r"\bтолько платн\w*\s+материал\w*\b", norm)
    )
    access = "free" if only_free else "paid" if only_paid else None
    return requested_format, budget, access

def acceptable(material, filters):
    requested_format, budget, access = filters
    if requested_format and material_format(material) != requested_format:
        return False
    if access and material.access != access:
        return False
    if budget is not None and material.access != "free":
        return material.cost is not None and material.cost <= budget
    return True

def tokens(text):
    normalized = _normalize_text(re.sub("<[^>]*>", " ", text))
    return [_stem_ru(word) for word in normalized.split() if word not in STOPWORDS and len(word) >= 2]

def make_bm25(catalog):
    indexes = []
    for field, weight in zip((lambda m: m.title, lambda m: m.description, lambda m: " ".join(m.topics)), FIELD_WEIGHTS):
        docs = [tokens(field(m)) for m in catalog]
        avg_len = sum(map(len, docs)) / len(docs) or 1
        frequencies = Counter(term for doc in docs for term in set(doc))
        index = defaultdict(list)
        for material, doc in zip(catalog, docs):
            normalizer = K1 * (1 - B + B * len(doc) / avg_len)
            for term, count in Counter(doc).items():
                df = frequencies[term]
                idf = log1p((len(docs) - df + 0.5) / (df + 0.5))
                score = idf * (count * (K1 + 1)) / (count + normalizer)
                index[term].append((material.id, score * weight))
        indexes.append(index)

    @cache
    def order(goal):
        scores = defaultdict(float)
        for term in sorted(set(tokens(goal or ""))):
            for index in indexes:
                for mid, score in index.get(term, ()):
                    scores[mid] += score
        return tuple(sorted(scores, key=lambda mid: (-scores[mid], mid)))

    return order

def make_result(ids, limit, note):
    return RecommendationResult(tuple(Recommendation(mid, note, "Откройте карточку.") for mid in ids[:limit]), note)

def merge_orders(orders):
    scores = defaultdict(float)
    best_rank = {}
    for order in orders:
        for rank, mid in enumerate(order[:FUSION_DEPTH], 1):
            scores[mid] += 1 / (RRF_OFFSET + rank)
            best_rank[mid] = min(best_rank.get(mid, rank), rank)
    return sorted(scores, key=lambda mid: (-scores[mid], best_rank[mid], mid))
