"""Возобновляемый совместный поиск параметров гибрида BM25 и recommend."""

import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from heapq import nlargest
from importlib.util import find_spec
from itertools import batched, chain, product
from math import prod
from pathlib import Path
from tempfile import TemporaryDirectory

from app.catalog import DEMO_CATALOG_PATH
from app.recommender.hybrid import B, DEFAULT_BM25_WEIGHT, FIELD_WEIGHTS, K1
from evaluation.runner import EVALUATION_DIR, FIXTURES_DIR, load_evaluation_inputs, load_real_catalog
from evaluation.tune_weights import QUALITY_LIMITS, _catalog_quality, _choice, _grid_row, _label, measure, quality_regressions, split_by_topic

REFERENCE = (DEFAULT_BM25_WEIGHT, FIELD_WEIGHTS, B, K1)
PREVIOUS = (.30, FIELD_WEIGHTS, .90, K1)
ANCHORS = (REFERENCE, PREVIOUS, (0., FIELD_WEIGHTS, B, K1), (.5, FIELD_WEIGHTS, B, K1))
LIMITS = QUALITY_LIMITS + (
    ("Базовая точность · следующий курс", "Hit@3", True),
    ("Педагогическая прогрессия", "Hit@3", True),
    ("Педагогическая прогрессия", "Откат к глоссарию ↓", False),
    ("Устойчивость к случайным кликам", "Ядро после мисклика ↑", True),
    ("Свежесть и новые эксперты", "Цель в топ-3", True),
)
RANGES = ((.05, .85), (0., 2.5), (0., 2.5), (0., 1.), (0., 3.5))
STEPS = (.08, .35, .35, .15, .5)
GRID_AXES = ((.1, .25, .5, .75, 1.), (0., 1., 2.5), (0., 1., 2.5), (0., .75, 1.), (0., 1.2, 3.5))
GRID_MAXIMA = (1000, 2500, 2500, 1000, 3500)
GRID_REPORT_LIMIT = 100
GRID_REFINE_STEPS = (100, 10, 1)
GRID_REFINE_SEEDS = 3


def _parse_step(raw):
    try:
        step = Decimal(raw)
    except InvalidOperation:
        step = Decimal(0)
    if not step.is_finite() or not Decimal(".001") <= step <= Decimal(1):
        raise argparse.ArgumentTypeError("Шаг должен быть от 0.001 до 1.")
    thousandths = step * 1000
    if thousandths != int(thousandths):
        raise argparse.ArgumentTypeError("Шаг должен быть кратен 0.001.")
    return int(thousandths)


def _grid_axes(step):
    if step is None:
        return GRID_AXES
    axes = []
    for index, maximum in enumerate(GRID_MAXIMA):
        values = list(range(step if index == 0 else 0, maximum, step))
        values.append(maximum)
        axes.append(tuple(value / 1000 for value in values))
    return tuple(axes)


def _full_grid(axes):
    anchors = [configuration for configuration in dict.fromkeys(ANCHORS) if not all(
        value in axis for value, axis in zip((configuration[0], *configuration[1][1:], *configuration[2:]),
                                           axes, strict=True)
    )]
    grid = ((weight, (FIELD_WEIGHTS[0], description, tags), length, saturation)
            for weight, description, tags, length, saturation in product(*axes))
    return chain(anchors, grid), prod(map(len, axes)) + len(anchors)


def _neighbors(configuration, step):
    weight, fields, length, saturation = configuration
    center = [round(value * 1000) for value in (weight, *fields[1:], length, saturation)]
    for index, maximum in enumerate(GRID_MAXIMA):
        minimum = 1 if index == 0 else 0
        for delta in (-step, step):
            values = center.copy()
            values[index] = max(minimum, min(maximum, center[index] + delta))
            if values != center:
                weight, description, tags, length, saturation = (value / 1000 for value in values)
                yield weight, (FIELD_WEIGHTS[0], description, tags), length, saturation


def _key(configuration):
    return json.dumps(configuration, separators=(",", ":"))


def _config(row):
    return row["bm25_weight"], tuple(row["field_weights"]), row["length_penalty"], row["saturation"]


def _from_dimensions(values):
    weight, description, tags, length, saturation = (
        round(max(low, min(high, value)), 3) for value, (low, high) in zip(values, RANGES, strict=True)
    )
    return weight, (3, description, tags), length, saturation


def _sample(count, rng):
    columns = []
    for low, high in RANGES:
        column = [low + (high - low) * (index + .5) / count for index in range(count)]
        rng.shuffle(column)
        columns.append(column)
    return [_from_dimensions(values) for values in zip(*columns, strict=True)]


def _refine(count, seeds, seen, rng):
    configurations = []
    for attempt in range(count * 30):
        if len(configurations) == count:
            break
        weight, (_, description, tags), length, saturation = seeds[attempt % len(seeds)]
        center = (weight, description, tags, length, saturation)
        candidate = _from_dimensions(value + rng.uniform(-step, step) for value, step in zip(center, STEPS, strict=True))
        if candidate not in seen:
            seen.add(candidate)
            configurations.append(candidate)
    return configurations


def _fingerprint(source, backup, seed):
    root = EVALUATION_DIR.parent
    files = [source, source.parent / "subscriptions.json", source.parent / "recommended_links.json",
             backup, DEMO_CATALOG_PATH, EVALUATION_DIR / "catalog.extra.json", FIXTURES_DIR / "recommended_links.json"]
    files.extend(sorted(EVALUATION_DIR.glob("profiles.*.json")))
    files.extend(sorted(FIXTURES_DIR.glob("history*.json")))
    for directory in (EVALUATION_DIR, root / "app" / "catalog", root / "app" / "recommender"):
        files.extend(sorted(directory.glob("*.py")))
    files.extend(sorted((root / "app" / "catalog").glob("*.json")))
    files.append(root / "app" / "contracts.py")
    hashes = [(path.name, hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None) for path in files]
    return hashlib.sha256(_key([seed, hashes]).encode()).hexdigest()


def _open_checkpoint(db, identity):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS results (
            phase TEXT NOT NULL, configuration TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY (phase, configuration)
        )""")
        db.execute("INSERT OR IGNORE INTO metadata VALUES (1, ?)", (identity,))
        saved = db.execute("SELECT fingerprint FROM metadata WHERE id = 1").fetchone()[0]
        if saved != identity:
            raise ValueError("Входные данные, код или seed изменились. Укажите новую папку через --output-dir.")


def _run(db, phase, configurations, evaluate, pool, jobs, *, offset=0, total=None):
    pending = list(dict.fromkeys(configurations))
    saved = {}
    for configuration in pending:
        key = _key(configuration)
        record = db.execute("SELECT result FROM results WHERE phase = ? AND configuration = ?", (phase, key)).fetchone()
        if record is not None:
            saved[key] = record[0]
    missing = [configuration for configuration in pending if _key(configuration) not in saved]
    done = offset + len(pending) - len(missing)
    total = len(pending) if total is None else total

    def save(configuration, result):
        nonlocal done
        key = _key(configuration)
        saved[key] = json.dumps(result, ensure_ascii=False)
        with db:
            db.execute("INSERT OR REPLACE INTO results VALUES (?, ?, ?)", (phase, key, saved[key]))
        done += 1
        print(f"{phase} · {done} из {total} · сохранён {key}", file=sys.stderr, flush=True)

    if not offset or missing:
        print(f"{phase} · готово {done} из {total}", file=sys.stderr, flush=True)
    if pool is None:
        for configuration in missing:
            save(configuration, evaluate(configuration))
    else:
        configurations_to_run = iter(missing[jobs:])
        futures = {pool.submit(evaluate, configuration): configuration
                   for configuration in missing[:jobs]}
        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                save(futures.pop(future), future.result())
                configuration = next(configurations_to_run, None)
                if configuration is not None:
                    futures[pool.submit(evaluate, configuration)] = configuration
    return [json.loads(saved[_key(configuration)]) for configuration in pending]


def _score(row):
    return _choice({**row, "field_weights": tuple(row["field_weights"])}, DEFAULT_BM25_WEIGHT)


def _compare(row, reference):
    issues = quality_regressions(row, reference, LIMITS)
    for suite, scores, current, metrics in (
        ("Синтетика", row["synthetic"], reference["synthetic"], ("hit_at_3",)),
        ("Экспертный граф", row["specialized"]["graph"], reference["specialized"]["graph"],
         ("Hit@3", "Первое место в ловушке")),
    ):
        for metric in metrics:
            if scores[metric] < current[metric]:
                issues.append({"suite": suite, "metric": metric, "reference": current[metric], "observed": scores[metric]})
    row["regressions"] = issues


def _control(configuration, catalog, validation, source):
    weight, fields, length, saturation = configuration
    return measure(weight, catalog, validation, source.parent, fields, length, saturation)


def _quality(configuration, backup, source):
    weight, fields, length, saturation = configuration
    return _catalog_quality(backup, source, {"bm25_weight": weight, "field_weights": fields,
                                            "length_penalty": length, "saturation": saturation})


def _refine_grid(run, evaluate, evaluated, reference):
    hybrids = [row for row in evaluated.values() if row["bm25_weight"]]
    qualified = nlargest(GRID_REFINE_SEEDS - 1, (row for row in hybrids if not row["regressions"]), key=_score)
    raw = max((row for row in hybrids if row not in qualified), key=_score, default=None)
    centers = [(row, True) for row in qualified]
    if raw is not None:
        centers.append((raw, not raw["regressions"]))

    stages = []
    new_rows = []
    for step in GRID_REFINE_STEPS:
        neighborhoods = [tuple(_neighbors(_config(row), step)) for row, _ in centers]
        missing = list(dict.fromkeys(candidate for neighbors in neighborhoods for candidate in neighbors
                                     if candidate not in evaluated))
        rows = run(f"Уточнение {step / 1000:g}", missing, evaluate)
        for row in rows:
            _compare(row, reference)
            evaluated[_config(row)] = row
        new_rows.extend(rows)

        for index, ((center, require_quality), neighbors) in enumerate(zip(centers, neighborhoods, strict=True)):
            candidates = (evaluated[config] for config in neighbors)
            best = max((row for row in candidates if not require_quality or not row["regressions"]),
                       key=_score, default=center)
            if _score(best) > _score(center):
                centers[index] = (best, require_quality)
        stages.append({"step": step / 1000, "configurations": len(rows),
                       "leaders": [_config(row) for row, _ in centers]})
    return new_rows, stages


def _grid_search(args, run, evaluate):
    axes = _grid_axes(args.grid_step)
    configurations, count = _full_grid(axes)
    phase = "Полная сетка" if args.grid_step is None else f"Полная сетка {args.grid_step / 1000:g}"
    reference = run(phase, [REFERENCE], evaluate, total=count)[0]
    selected = reference
    _compare(reference, reference)
    evaluated = {} if args.grid_step is None else None

    def assessed():
        nonlocal selected
        remaining = (configuration for configuration in configurations if configuration != REFERENCE)
        yield reference
        batch_size = max(32, args.jobs * 8)
        for index, batch in enumerate(batched(remaining, batch_size)):
            for row in run(phase, batch, evaluate, offset=1 + index * batch_size, total=count):
                _compare(row, reference)
                if not row["regressions"] and _score(row) > _score(selected):
                    selected = row
                yield row

    if evaluated is None:
        rows = nlargest(GRID_REPORT_LIMIT, assessed(), key=_score)
        return rows, count, reference, selected, rows[0], selected, [], []

    evaluated.update((_config(row), row) for row in assessed())
    grid_selected = selected
    refined, stages = _refine_grid(run, evaluate, evaluated, reference)
    rows = nlargest(GRID_REPORT_LIMIT, evaluated.values(), key=_score)
    selected = max((row for row in evaluated.values() if not row["regressions"]), key=_score)
    return rows, count, reference, selected, rows[0], grid_selected, refined, stages


def _report(args, db, pool, catalog, tuning, validation, topics):
    demo_catalog, demo = load_evaluation_inputs("demo")
    evaluate = partial(_grid_row, catalog=catalog, tuning=tuning, source=args.catalog, demo_catalog=demo_catalog,
                       demo=demo, backup=args.backup)
    run = partial(_run, db, pool=pool, jobs=args.jobs)
    grid_selected = None
    refined, stages = [], []
    if args.exhaustive:
        rows, count, reference, selected, unconstrained, grid_selected, refined, stages = _grid_search(args, run, evaluate)
    else:
        rows, reference = _sample_search(args, run, evaluate)
        count = len(rows)
        selected = max((row for row in rows if not row["regressions"]), key=_score)
        unconstrained = max(rows, key=_score)

    finalists = list({_config(row): row for row in (reference, grid_selected, selected, unconstrained)
                      if row is not None}.values())
    control = partial(_control, catalog=catalog, validation=validation, source=args.catalog)
    configurations = [_config(row) for row in finalists]
    for row, scores in zip(finalists, run("Отложенные темы", configurations, control), strict=True):
        row["validation"] = scores
    if not args.smoke:
        quality = partial(_quality, backup=args.backup, source=args.catalog)
        for row, scores in zip(finalists, run("Качество каталога", configurations, quality), strict=True):
            row["catalog_quality"] = scores
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "seed": args.seed, "smoke": args.smoke,
        "tuning_cases": len(tuning), "validation_cases": len(validation), "tuning_topics": topics[0],
        "validation_topics": topics[1], "specialized_limits": LIMITS, "rows": rows,
        "configuration_count": count + len(refined), "refinement_stages": stages, "refinement_rows": refined,
        "grid_configuration_count": count if args.exhaustive else None, "grid_selected": grid_selected,
        "search": "exhaustive" if args.exhaustive else "sampled",
        "grid_step": args.grid_step / 1000 if args.grid_step is not None else None,
        "grid_axes": _grid_axes(args.grid_step) if args.exhaustive else None,
        "selected": selected, "unconstrained": unconstrained, "finalists": finalists,
    }


def _sample_search(args, run, evaluate):
    rng = random.Random(args.seed)
    starting = list(ANCHORS[:2] if args.smoke else ANCHORS)
    if not args.smoke:
        starting.extend(_sample(args.samples, rng))
    rows = run("Поиск", starting, evaluate)
    reference = next(row for row in rows if _config(row) == REFERENCE)
    for row in rows:
        _compare(row, reference)

    if not args.smoke and args.refine:
        eligible = sorted((row for row in rows if not row["regressions"]), key=_score, reverse=True)
        leaders = sorted(rows, key=_score, reverse=True)
        seeds = list(dict.fromkeys(_config(row) for row in eligible[:5] + leaders[:3] if row["bm25_weight"]))
        extra = _refine(args.refine, seeds, {_config(row) for row in rows}, rng)
        for row in run("Уточнение", extra, evaluate):
            _compare(row, reference)
            rows.append(row)

    return rows, reference


def _save_reports(directory, report):
    selected = report["selected"]
    lines = [
        "# Совместный подбор параметров BM25", "",
        f"Конфигураций {report['configuration_count']}. Для настройки {report['tuning_cases']} сценариев, "
        f"для проверки {report['validation_cases']}.", "",
        f"Выбран {_label(selected)} по nDCG@3 на настройке и порогам специализированных метрик.", "",
    ]
    if report["refinement_stages"]:
        lines.extend([
            f"Полная сетка — {report['grid_configuration_count']} конфигураций. "
            f"Лучший вариант до уточнения — {_label(report['grid_selected'])}.", "",
            "| Шаг уточнения | Новых конфигураций |", "| ---: | ---: |",
            *(f"| {stage['step']:g} | {stage['configurations']} |" for stage in report["refinement_stages"]), "",
        ])
    lines.extend([
        "## Отложенные сценарии", "",
        "| Конфигурация | Hit@3 | MRR@3 | Precision@3 | nDCG@3 | MAP@3 | Просадки |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in report["finalists"]:
        scores = row["validation"]
        lines.append(f"| {_label(row)} | {scores['hit_at_3']:.2%} | {scores['mrr_at_3']:.4f} | "
                     f"{scores['precision_at_3']:.4f} | {scores['ndcg_at_3']:.4f} | {scores['map_at_3']:.4f} | "
                     f"{len(row['regressions'])} |")
    section = "Все конфигурации" if len(report["rows"]) == report["configuration_count"] else (
        f"Первые {len(report['rows'])} конфигураций по метрикам. Полные результаты в `progress.sqlite`"
    )
    lines.extend(("", f"## {section}", "",
                  "| Конфигурация | Hit@3 | MRR@3 | nDCG@3 | Просадки |",
                  "| --- | ---: | ---: | ---: | ---: |"))
    for row in sorted(report["rows"], key=_score, reverse=True):
        scores = row["tuning"]
        lines.append(f"| {_label(row)} | {scores['hit_at_3']:.2%} | {scores['mrr_at_3']:.4f} | "
                     f"{scores['ndcg_at_3']:.4f} | {len(row['regressions'])} |")
    with TemporaryDirectory(dir=directory) as temporary:
        staging = Path(temporary)
        for name, content in (("report.json", json.dumps(report, ensure_ascii=False, indent=2)),
                              ("report.md", "\n".join(lines))):
            target = staging / name
            target.write_text(content + "\n", encoding="utf-8")
            target.replace(directory / name)
    print(f"Отчёт сохранён в {directory / 'report.md'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--samples", type=int, default=180)
    parser.add_argument("--refine", type=int, default=60)
    parser.add_argument("--seed", default="alfahack-bm25-v1")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true", help="Проверить весь маршрут на двух конфигурациях")
    mode.add_argument("--exhaustive", action="store_true", help="Проверить все комбинации значений сетки")
    parser.add_argument("--grid-step", type=_parse_step, help="Шаг всех осей вместо предустановленной полной сетки")
    parser.add_argument("--count-only", action="store_true", help="Посчитать конфигурации полной сетки без запуска")
    args = parser.parse_args()
    if args.smoke and (args.grid_step is not None or args.count_only):
        parser.error("Параметры сетки несовместимы с --smoke.")
    args.exhaustive = args.exhaustive or args.grid_step is not None or args.count_only
    if args.count_only:
        axes = _grid_axes(args.grid_step)
        _, count = _full_grid(axes)
        print(f"Размеры осей (BM25, описание, теги, b, k1) — {', '.join(map(str, map(len, axes)))}")
        print(f"Конфигураций с учётом контрольных — {count:,}".replace(",", " "))
        if args.grid_step is None:
            print(f"Затем до {GRID_REFINE_SEEDS * 2 * len(GRID_MAXIMA) * len(GRID_REFINE_STEPS)} "
                  "новых конфигураций на этапе уточнения.")
        return
    if args.catalog is None or args.backup is None or args.output_dir is None:
        parser.error("Для прогона укажите --catalog, --backup и --output-dir.")
    if not args.catalog.is_file() or not args.backup.is_file() or args.jobs < 1 or args.samples < 1 or args.refine < 0:
        parser.error("Проверьте пути к данным и число конфигураций и процессов.")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    try:
        if find_spec("numpy") is None:
            raise ModuleNotFoundError("Установите numpy==2.2.6 в используемое окружение.")
        catalog, cases = load_evaluation_inputs("real", load_real_catalog(args.catalog))
        tuning, validation, train_topics, test_topics = split_by_topic(cases, .30, args.seed)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(args.output_dir / "progress.sqlite")) as db:
            _open_checkpoint(db, _fingerprint(args.catalog, args.backup, args.seed))
            if args.jobs == 1:
                report = _report(args, db, None, catalog, tuning, validation, (train_topics, test_topics))
            else:
                pool = ProcessPoolExecutor(max_workers=args.jobs)
                try:
                    report = _report(args, db, pool, catalog, tuning, validation, (train_topics, test_topics))
                finally:
                    pool.shutdown(cancel_futures=True)
        _save_reports(args.output_dir, report)
    except KeyboardInterrupt:
        parser.exit(130, "Завершённые конфигурации сохранены. Повторите команду для продолжения.\n")
    except ModuleNotFoundError as error:
        parser.exit(1, f"Для запуска нужен NumPy. {error}\n")
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
