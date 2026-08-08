#!/usr/bin/env python3
"""
host_check.py — проверка доступности хостов и открытых портов.

Читает список узлов из YAML/JSON-файла, параллельно проверяет ICMP и TCP-порты,
выводит отчёт в консоль или JSON. Пишется под задачу «утром понять, что упало».

Пример hosts.json:
{
  "switch-01": {"host": "10.0.0.11", "ports": [22, 80]},
  "tftp":      {"host": "10.0.0.5",  "ports": [69]},
  "db":        {"host": "10.0.0.20", "ports": [3306]}
}

Использование:
    ./host_check.py -f hosts.json
    ./host_check.py -f hosts.json --json > report.json
    ./host_check.py -f hosts.json --quiet      # только проблемные узлы

Код возврата: 0 — всё доступно, 1 — есть недоступные (удобно для cron/мониторинга).
"""

import argparse
import json
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime

# ANSI-цвета. Отключаются автоматически, если вывод перенаправлен в файл.
COLOR = sys.stdout.isatty()
GREEN = "\033[92m" if COLOR else ""
RED = "\033[91m" if COLOR else ""
YELLOW = "\033[93m" if COLOR else ""
RESET = "\033[0m" if COLOR else ""


@dataclass
class CheckResult:
    """Результат проверки одного узла."""
    name: str
    host: str
    icmp_ok: bool = False
    latency_ms: float | None = None          # средняя задержка, None если хост не ответил
    ports: dict[int, bool] = field(default_factory=dict)
    error: str | None = None

    @property
    def healthy(self) -> bool:
        """
        Узел здоров, если все заявленные порты открыты и (если ICMP доступен) он пингуется.

        Когда утилиты ping в системе нет, судим только по портам — иначе получим
        ложную тревогу по всем узлам сразу.
        """
        ports_ok = all(self.ports.values()) if self.ports else True
        icmp_ok = self.icmp_ok or not PING_AVAILABLE
        return icmp_ok and ports_ok


# Проверяем наличие ping один раз при старте: если утилиты нет, честно говорим
# «не смог проверить» вместо того, чтобы объявить все узлы упавшими.
PING_AVAILABLE = shutil.which("ping") is not None


def ping(host: str, count: int = 2, timeout: int = 2) -> tuple[bool, float | None]:
    """
    ICMP-проверка через системный ping.

    Не используем raw-сокеты, чтобы не требовать root — системный ping имеет suid
    и работает от обычного пользователя.
    Возвращает (доступен, средняя_задержка_мс).
    """
    if not PING_AVAILABLE:
        return False, None

    try:
        proc = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout), host],
            capture_output=True, text=True, timeout=count * timeout + 2,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, None

    if proc.returncode != 0:
        return False, None

    # Строка вида: rtt min/avg/max/mdev = 0.045/0.052/0.060/0.007 ms
    for line in proc.stdout.splitlines():
        if "min/avg/max" in line:
            try:
                avg = float(line.split("=")[1].strip().split("/")[1])
                return True, round(avg, 2)
            except (IndexError, ValueError):
                pass
    return True, None      # пинг прошёл, но задержку распарсить не вышло


def check_port(host: str, port: int, timeout: float = 3.0) -> bool:
    """TCP-проверка порта: успешный connect означает, что сервис слушает."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, socket.error, OSError):
        return False


def check_host(name: str, cfg: dict) -> CheckResult:
    """Полная проверка одного узла: ICMP + все порты из конфига."""
    host = cfg.get("host")
    result = CheckResult(name=name, host=host or "")

    if not host:
        result.error = "в конфиге не указано поле 'host'"
        return result

    result.icmp_ok, result.latency_ms = ping(host)

    # Порты проверяем независимо от пинга: ICMP часто закрыт файрволом,
    # но сервис при этом жив.
    for port in cfg.get("ports", []):
        result.ports[port] = check_port(host, port)

    return result


def print_report(results: list[CheckResult], quiet: bool = False) -> None:
    """Человекочитаемый отчёт в консоль."""
    print(f"\nПроверка узлов — {datetime.now():%Y-%m-%d %H:%M:%S}")
    if not PING_AVAILABLE:
        print(f"{YELLOW}Внимание: утилита ping не найдена, ICMP-проверка пропущена "
              f"(установите iputils-ping){RESET}")
    print("=" * 62)

    shown = 0
    for r in sorted(results, key=lambda x: (x.healthy, x.name)):
        if quiet and r.healthy:
            continue           # в тихом режиме печатаем только проблемы
        shown += 1

        if r.error:
            print(f"{YELLOW}[?]{RESET} {r.name:<16} {r.error}")
            continue

        mark = f"{GREEN}[OK]{RESET}" if r.healthy else f"{RED}[!!]{RESET}"
        latency = f"{r.latency_ms} ms" if r.latency_ms is not None else "—"
        if not PING_AVAILABLE:
            icmp = f"{YELLOW}icmp н/д{RESET}"        # утилита ping не установлена
        else:
            icmp = "icmp ok" if r.icmp_ok else f"{RED}icmp fail{RESET}"
        print(f"{mark} {r.name:<16} {r.host:<16} {icmp:<20} {latency}")

        for port, ok in sorted(r.ports.items()):
            state = f"{GREEN}открыт{RESET}" if ok else f"{RED}закрыт{RESET}"
            print(f"       порт {port:<6} {state}")

    if quiet and shown == 0:
        print(f"{GREEN}Все узлы доступны.{RESET}")

    total = len(results)
    bad = sum(1 for r in results if not r.healthy)
    print("=" * 62)
    print(f"Всего узлов: {total} | С проблемами: {bad}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка доступности хостов и портов")
    parser.add_argument("-f", "--file", required=True, help="JSON-файл со списком узлов")
    parser.add_argument("-w", "--workers", type=int, default=10,
                        help="число параллельных проверок (по умолчанию 10)")
    parser.add_argument("--json", action="store_true", help="вывод в формате JSON")
    parser.add_argument("--quiet", action="store_true", help="показывать только проблемные узлы")
    args = parser.parse_args()

    try:
        with open(args.file, encoding="utf-8") as fh:
            hosts = json.load(fh)
    except FileNotFoundError:
        print(f"Файл не найден: {args.file}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"Ошибка разбора JSON: {exc}", file=sys.stderr)
        return 2

    # Проверки сетевые и медленные — распараллеливаем потоками (GIL здесь не мешает,
    # потоки почти всё время ждут ответа сети).
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda kv: check_host(*kv), hosts.items()))

    if args.json:
        print(json.dumps(
            {"timestamp": datetime.now().isoformat(),
             "results": [asdict(r) for r in results]},
            ensure_ascii=False, indent=2,
        ))
    else:
        print_report(results, quiet=args.quiet)

    # Ненулевой код возврата при проблемах — чтобы cron или Zabbix могли среагировать.
    return 1 if any(not r.healthy for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
