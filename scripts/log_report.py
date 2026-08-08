#!/usr/bin/env python3
"""
log_report.py — разбор системных логов и сводка по ошибкам.

Читает лог-файл (или stdin), группирует записи по уровню и источнику,
находит повторяющиеся ошибки и всплески активности. Задача — за 5 секунд
понять, что происходило в системе, вместо ручного grep по 200 тысячам строк.

Использование:
    ./log_report.py /var/log/syslog
    ./log_report.py /var/log/syslog --level ERROR --top 20
    journalctl -n 5000 --no-pager | ./log_report.py -
    ./log_report.py /var/log/nginx/error.log --pattern "timeout"
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

# Уровни логирования в порядке возрастания важности.
LEVELS = ["DEBUG", "INFO", "NOTICE", "WARNING", "WARN", "ERROR", "ERR", "CRITICAL", "CRIT", "ALERT", "EMERG"]

# Формат syslog: "Aug  8 19:30:15 hostname service[1234]: сообщение"
SYSLOG_RE = re.compile(
    r"^(?P<month>\w{3})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+(?P<service>[\w\-./]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<message>.*)$"
)

# Формат ISO: "2026-08-08T19:30:15 ERROR service: сообщение"
ISO_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})\S*\s+"
    r"(?P<level>\w+)\s+(?P<service>[\w\-./]+)[:\s]\s*(?P<message>.*)$"
)


def detect_level(text: str) -> str:
    """
    Определяем уровень записи по ключевым словам.

    Логи в реальности редко имеют единый формат, поэтому ищем уровень
    как отдельное слово в верхнем регистре по всей строке.
    """
    upper = text.upper()
    for level in reversed(LEVELS):        # с конца — чтобы CRITICAL победил INFO в одной строке
        if re.search(rf"\b{level}\b", upper):
            return "WARNING" if level == "WARN" else \
                   "ERROR" if level == "ERR" else \
                   "CRITICAL" if level == "CRIT" else level
    return "INFO"                          # по умолчанию считаем запись информационной


def normalize(message: str) -> str:
    """
    Приводим сообщение к шаблону, чтобы сгруппировать однотипные ошибки.

    'Connection refused from 10.0.0.5:4212' и 'Connection refused from 10.0.0.9:5511'
    должны попасть в одну группу — иначе счётчик покажет тысячу уникальных ошибок
    вместо одной, повторившейся тысячу раз.
    """
    msg = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<IP>", message)          # IPv4
    msg = re.sub(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", "<MAC>", msg, flags=re.I)
    msg = re.sub(r"/[\w./\-]{4,}", "<PATH>", msg)                          # пути
    msg = re.sub(r"\b\d+\b", "<N>", msg)                                    # любые числа
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg[:120]                                                        # обрезаем длинные хвосты


def parse_line(line: str) -> dict | None:
    """Разбираем строку лога. Возвращаем None, если строка пустая."""
    line = line.rstrip("\n")
    if not line.strip():
        return None

    match = ISO_RE.match(line) or SYSLOG_RE.match(line)
    if match:
        data = match.groupdict()
        return {
            "service": data.get("service") or "unknown",
            "message": data.get("message") or line,
            "level": (data.get("level") or "").upper() if data.get("level") in LEVELS
                     else detect_level(line),
            "hour": (data.get("time") or data.get("ts", ""))[-8:-6] or "??",
        }

    # Строка нестандартного формата — сохраняем как есть, уровень определяем эвристикой.
    return {"service": "unparsed", "message": line, "level": detect_level(line), "hour": "??"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Сводка по логам")
    parser.add_argument("logfile", help="путь к лог-файлу или '-' для чтения из stdin")
    parser.add_argument("--level", help="показывать только записи этого уровня и выше")
    parser.add_argument("--pattern", help="фильтр по подстроке (регистр не важен)")
    parser.add_argument("--top", type=int, default=10, help="сколько топ-записей показать")
    args = parser.parse_args()

    # Открываем файл или читаем stdin — это позволяет использовать скрипт в пайпе.
    try:
        stream = sys.stdin if args.logfile == "-" else open(args.logfile, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print(f"Файл не найден: {args.logfile}", file=sys.stderr)
        return 2
    except PermissionError:
        print(f"Нет прав на чтение: {args.logfile} (нужен sudo?)", file=sys.stderr)
        return 2

    level_counts = Counter()
    service_counts = Counter()
    error_patterns = Counter()
    hourly = defaultdict(int)
    total = 0
    min_index = LEVELS.index(args.level.upper()) if args.level and args.level.upper() in LEVELS else None

    try:
        for line in stream:
            entry = parse_line(line)
            if entry is None:
                continue

            if args.pattern and args.pattern.lower() not in entry["message"].lower():
                continue

            if min_index is not None:
                idx = LEVELS.index(entry["level"]) if entry["level"] in LEVELS else 0
                if idx < min_index:
                    continue

            total += 1
            level_counts[entry["level"]] += 1
            service_counts[entry["service"]] += 1
            hourly[entry["hour"]] += 1

            if entry["level"] in ("ERROR", "CRITICAL", "ALERT", "EMERG"):
                error_patterns[normalize(entry["message"])] += 1
    finally:
        if stream is not sys.stdin:
            stream.close()

    # --- вывод отчёта --------------------------------------------------------
    print(f"\nОтчёт по логу: {args.logfile}")
    print(f"Сформирован: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 70)

    if total == 0:
        print("Подходящих записей не найдено.\n")
        return 0

    print(f"Обработано записей: {total}\n")

    print("Распределение по уровням:")
    for level, count in level_counts.most_common():
        share = count / total * 100
        bar = "#" * int(share / 2)                     # простая текстовая гистограмма
        print(f"  {level:<10} {count:>7}  {share:5.1f}%  {bar}")

    print(f"\nТоп-{args.top} источников:")
    for service, count in service_counts.most_common(args.top):
        print(f"  {service:<30} {count:>7}")

    if error_patterns:
        print(f"\nТоп-{args.top} повторяющихся ошибок:")
        for pattern, count in error_patterns.most_common(args.top):
            print(f"  [{count:>5}x] {pattern}")
    else:
        print("\nОшибок уровня ERROR и выше не обнаружено.")

    # Всплеск активности часто указывает на момент инцидента.
    if len(hourly) > 1 and "??" not in hourly:
        peak_hour, peak_count = max(hourly.items(), key=lambda kv: kv[1])
        avg = total / len(hourly)
        if peak_count > avg * 2:
            print(f"\nВсплеск активности: {peak_hour}:00 — {peak_count} записей "
                  f"при среднем {avg:.0f}/час")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
