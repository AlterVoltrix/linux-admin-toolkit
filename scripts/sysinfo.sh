#!/usr/bin/env bash
#
# sysinfo.sh — снимок состояния сервера: ресурсы, сервисы, сеть.
#
# Использование:
#   ./sysinfo.sh              # отчёт в консоль
#   ./sysinfo.sh --check      # только проблемы, код возврата 1 если что-то не так
#   ./sysinfo.sh --json       # машиночитаемый вывод для мониторинга
#
# Задача: одна команда вместо десяти при разборе «сервер тормозит».

set -uo pipefail            # без -e: часть проверок может отсутствовать на минимальных системах

DISK_THRESHOLD=85           # процент заполнения диска, выше которого это уже проблема
MEM_THRESHOLD=90            # процент занятой памяти
LOAD_MULTIPLIER=2           # load average выше (ядра * множитель) считаем перегрузкой

MODE="full"
PROBLEMS=0                  # счётчик найденных проблем -> код возврата

case "${1:-}" in
    --check) MODE="check" ;;
    --json)  MODE="json" ;;
    --help|-h) sed -n '3,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

# --- вспомогательные функции -------------------------------------------------
header() { [[ "$MODE" == "full" ]] && { echo; echo "--- $1 ${*:2}"; printf '%.0s-' {1..60}; echo; }; }
warn()   { PROBLEMS=$((PROBLEMS + 1)); [[ "$MODE" != "json" ]] && echo "  [!] $1"; }
info()   { [[ "$MODE" == "full" ]] && echo "  $1"; }

# --- общая информация --------------------------------------------------------
HOSTNAME=$(hostname -f 2>/dev/null || hostname)
UPTIME=$(uptime -p 2>/dev/null || uptime)
KERNEL=$(uname -r)
OS=$(grep -oP '(?<=^PRETTY_NAME=").*(?=")' /etc/os-release 2>/dev/null || echo "unknown")

if [[ "$MODE" == "full" ]]; then
    echo "=============================================================="
    echo " Состояние системы: $HOSTNAME"
    echo " $(date '+%Y-%m-%d %H:%M:%S')"
    echo "=============================================================="
    info "ОС:      $OS"
    info "Ядро:    $KERNEL"
    info "Аптайм:  $UPTIME"
fi

# --- процессор и нагрузка ----------------------------------------------------
header "Процессор"
CORES=$(nproc)
read -r LOAD1 LOAD5 LOAD15 _ < /proc/loadavg
LOAD_LIMIT=$(echo "$CORES * $LOAD_MULTIPLIER" | bc -l 2>/dev/null || echo "$((CORES * LOAD_MULTIPLIER))")

info "Ядер: $CORES"
info "Load average: $LOAD1 (1м) / $LOAD5 (5м) / $LOAD15 (15м)"

# bc может отсутствовать — сравниваем через awk, он есть везде.
if awk -v l="$LOAD1" -v m="$LOAD_LIMIT" 'BEGIN{exit !(l>m)}'; then
    warn "Высокая нагрузка: load $LOAD1 при $CORES ядрах"
fi

# --- память ------------------------------------------------------------------
header "Память"
MEM_TOTAL=$(awk '/MemTotal/{print $2}' /proc/meminfo)
MEM_AVAIL=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
MEM_USED=$((MEM_TOTAL - MEM_AVAIL))
MEM_PCT=$((MEM_USED * 100 / MEM_TOTAL))

info "Использовано: $((MEM_USED / 1024)) МБ из $((MEM_TOTAL / 1024)) МБ (${MEM_PCT}%)"
(( MEM_PCT > MEM_THRESHOLD )) && warn "Память почти исчерпана: ${MEM_PCT}%"

# Swap: активная подкачка на сервере почти всегда признак нехватки памяти.
SWAP_TOTAL=$(awk '/SwapTotal/{print $2}' /proc/meminfo)
if (( SWAP_TOTAL > 0 )); then
    SWAP_FREE=$(awk '/SwapFree/{print $2}' /proc/meminfo)
    SWAP_PCT=$(( (SWAP_TOTAL - SWAP_FREE) * 100 / SWAP_TOTAL ))
    info "Swap: ${SWAP_PCT}% занято"
    (( SWAP_PCT > 50 )) && warn "Активное использование swap: ${SWAP_PCT}%"
fi

# --- диски -------------------------------------------------------------------
header "Дисковое пространство"
while read -r fs size used avail pct mount; do
    [[ "$fs" == "Filesystem" ]] && continue           # пропускаем заголовок
    PCT_NUM=${pct%\%}
    info "$mount: $used / $size ($pct)"
    (( PCT_NUM > DISK_THRESHOLD )) && warn "Диск $mount заполнен на $pct"
done < <(df -hP -x tmpfs -x devtmpfs -x squashfs 2>/dev/null)

# Иноды кончаются незаметно: место есть, а файл создать нельзя.
while read -r fs inodes iused ifree ipct mount; do
    [[ "$fs" == "Filesystem" ]] && continue
    IPCT_NUM=${ipct%\%}
    [[ "$IPCT_NUM" =~ ^[0-9]+$ ]] || continue
    (( IPCT_NUM > DISK_THRESHOLD )) && warn "Инодов на $mount осталось мало: $ipct"
done < <(df -iP -x tmpfs -x devtmpfs -x squashfs 2>/dev/null)

# --- сервисы -----------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1; then
    header "Сервисы systemd"
    FAILED=$(systemctl list-units --state=failed --no-legend --no-pager 2>/dev/null | wc -l)
    if (( FAILED > 0 )); then
        warn "Упавших юнитов: $FAILED"
        [[ "$MODE" != "json" ]] && systemctl list-units --state=failed --no-legend --no-pager 2>/dev/null \
            | awk '{print "      - " $1}'
    else
        info "Все юниты работают штатно"
    fi
fi

# --- сеть --------------------------------------------------------------------
header "Сеть"
if command -v ip >/dev/null 2>&1; then
    while read -r iface state; do
        info "$iface: $state"
    done < <(ip -br link show 2>/dev/null | awk '$1!="lo"{print $1, $2}')

    DEFAULT_GW=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
    if [[ -n "$DEFAULT_GW" ]]; then
        info "Шлюз по умолчанию: $DEFAULT_GW"
        # Пингуем шлюз только если ping установлен — иначе получим ложную тревогу.
        if command -v ping >/dev/null 2>&1; then
            ping -c1 -W2 "$DEFAULT_GW" >/dev/null 2>&1 || warn "Шлюз $DEFAULT_GW не отвечает"
        fi
    else
        warn "Маршрут по умолчанию не настроен"
    fi
else
    info "Утилита ip не найдена (пакет iproute2) — сетевые проверки пропущены"
fi

# Количество установленных соединений — резкий рост часто означает утечку или атаку.
if command -v ss >/dev/null 2>&1; then
    ESTAB=$(ss -tn state established 2>/dev/null | tail -n +2 | wc -l)
    LISTEN=$(ss -tln 2>/dev/null | tail -n +2 | wc -l)
    info "Соединений: $ESTAB установлено, портов слушается: $LISTEN"
fi

# --- топ процессов -----------------------------------------------------------
header "Топ-5 процессов по памяти"
if [[ "$MODE" == "full" ]]; then
    ps -eo pid,comm,%cpu,%mem --sort=-%mem 2>/dev/null | head -6 | awk 'NR>1{printf "  %-8s %-20s cpu %-6s mem %s\n", $1, $2, $3, $4}'
fi

# --- итог --------------------------------------------------------------------
if [[ "$MODE" == "json" ]]; then
    cat <<EOF
{
  "hostname": "$HOSTNAME",
  "timestamp": "$(date -Iseconds)",
  "load_1m": $LOAD1,
  "cores": $CORES,
  "memory_percent": $MEM_PCT,
  "problems": $PROBLEMS
}
EOF
elif [[ "$MODE" == "check" ]]; then
    (( PROBLEMS == 0 )) && echo "OK: проблем не обнаружено"
else
    echo
    printf '%.0s=' {1..62}; echo
    if (( PROBLEMS == 0 )); then
        echo " Итог: проблем не обнаружено"
    else
        echo " Итог: обнаружено проблем — $PROBLEMS"
    fi
fi

exit $(( PROBLEMS > 0 ? 1 : 0 ))
