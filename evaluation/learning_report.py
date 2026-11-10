"""Чтение отчёта расширенных проверок на сайте."""

import json

REPORT_NAME = "learning-evaluation.json"
REPORT_VERSION = 1


def read_report(path, fingerprint):
    if path is None or fingerprint is None:
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (isinstance(report, dict) and report.get("version") == REPORT_VERSION
            and report.get("catalog_fingerprint") == fingerprint
            and isinstance(report.get("suites"), list) and len(report["suites"]) == 5):
        return report
    return None
