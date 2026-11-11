#!/usr/bin/env bash
# Установка Бендера: Termux (Android), Linux, macOS. Запускается из папки программы:  bash install.sh
# Автозапуск вместе с системой (systemd у пользователя или Termux:Boot):  bash install.sh --service
# Зависимостей нет — нужен только Python 3.10+. Повторный запуск безопасен.

set -u

SERVICE=0
for arg in "$@"; do
  case "$arg" in
    --service) SERVICE=1 ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HERE/install.log"
REPORT="$HERE/install-report.txt"
: > "$LOG"

# --- вывод ----------------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
  TTY=1
  AMBER=$'\e[38;2;240;170;60m'; STEEL=$'\e[38;2;170;182;196m'; RED=$'\e[38;2;232;90;101m'; GREEN=$'\e[38;2;110;201;140m'
  DIM=$'\e[2m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
else
  TTY=0; AMBER=""; STEEL=""; RED=""; GREEN=""; DIM=""; BOLD=""; RESET=""
fi

banner() {
  if [ "$TTY" = 1 ]; then
    printf '\e[?25l'
    for f in "◉ ◉" "◉ ◉" "— —" "◉ ◉" "◉ ◉"; do
      printf '\r  %s%s%s' "$AMBER$BOLD" "$f" "$RESET"
      sleep 0.12
    done
    printf '\r\e[2K  %s◉ ◉%s  %sБ Е Н Д Е Р%s  %sустановка%s\n\n' "$AMBER$BOLD" "$RESET" "$STEEL$BOLD" "$RESET" "$DIM" "$RESET"
    printf '\e[?25h'
  else
    echo "== Бендер: установка =="
  fi
}

# step "Описание" команда аргументы…  — выполняет команду, пишет вывод в лог, рисует спиннер.
step() {
  local title="$1"; shift
  echo "" >> "$LOG"
  echo "=== $title: $*" >> "$LOG"
  if [ "$TTY" = 1 ]; then
    "$@" >> "$LOG" 2>&1 &
    local pid=$!
    local frames=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
    local i=0
    printf '\e[?25l'
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r\e[2K  %s%s%s %s' "$AMBER" "${frames[$((i % 10))]}" "$RESET" "$title"
      i=$((i + 1))
      sleep 0.1
    done
    wait "$pid"
    local code=$?
    printf '\e[?25h'
    if [ $code -eq 0 ]; then
      printf '\r\e[2K  %s✓%s %s\n' "$GREEN" "$RESET" "$title"
    else
      printf '\r\e[2K  %s✗%s %s\n' "$RED" "$RESET" "$title"
    fi
    return $code
  else
    echo "  • $title…"
    "$@" >> "$LOG" 2>&1
  fi
}

copy_to_clipboard() {
  local file="$1"
  for tool in "termux-clipboard-set" "wl-copy" "xclip -selection clipboard" "xsel --clipboard --input" "pbcopy" "clip.exe"; do
    if command -v ${tool%% *} >/dev/null 2>&1; then
      if $tool < "$file" 2>/dev/null; then return 0; fi
    fi
  done
  return 1
}

fail() {
  local what="$1"; local hint="$2"
  {
    echo "=== Отчёт об ошибке установки Бендера ==="
    echo "Что сломалось: $what"
    echo "Подсказка: $hint"
    echo "Платформа: ${PLATFORM:-неизвестно}"
    echo "Система: $(uname -a 2>/dev/null)"
    if [ -f /etc/os-release ]; then echo "Дистрибутив: $(. /etc/os-release; echo "${PRETTY_NAME:-}")"; fi
    [ -n "${TERMUX_VERSION:-}" ] && echo "Termux: $TERMUX_VERSION"
    echo "Python: $(${PY:-python3} --version 2>&1 || echo 'не найден')"
    echo "Папка: $HERE"
    echo ""
    echo "--- последние строки лога ($LOG) ---"
    tail -n 60 "$LOG"
    echo "=== конец отчёта ==="
  } > "$REPORT"
  echo ""
  echo "  ${RED}${BOLD}✗ $what${RESET}"
  echo "    $hint"
  echo ""
  echo "  Отчёт сохранён: $REPORT"
  echo "  Его можно целиком вставить в любой ИИ-чат или отправить разработчику."
  if [ "$TTY" = 1 ] && [ -t 0 ]; then
    printf '  %sНажми Enter, чтобы скопировать отчёт в буфер обмена (или Ctrl+C, чтобы выйти)%s ' "$AMBER" "$RESET"
    read -r _
    if copy_to_clipboard "$REPORT"; then
      echo "  ${GREEN}✓ Отчёт скопирован.${RESET}"
    else
      echo "  Скопировать не получилось (нет программы для буфера обмена). Вот отчёт — выдели и скопируй:"
      echo ""
      cat "$REPORT"
    fi
  else
    echo ""
    cat "$REPORT"
  fi
  exit 1
}

py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }

# --- платформа ------------------------------------------------------------------------

banner

if [ -n "${TERMUX_VERSION:-}" ] || { [ -d /data/data/com.termux/files/usr ] && command -v pkg >/dev/null 2>&1; }; then
  PLATFORM="termux"
elif [ "$(uname -s)" = "Linux" ]; then
  PLATFORM="linux"
elif [ "$(uname -s)" = "Darwin" ]; then
  PLATFORM="macos"
else
  PLATFORM="$(uname -s)"
fi
echo "  ${DIM}Платформа: $PLATFORM · папка: $HERE${RESET}"

if [ ! -f "$HERE/bender/__main__.py" ]; then
  fail "Установщик запущен не из папки Бендера" "Распакуй архив целиком (или склонируй репозиторий) и запусти install.sh из получившейся папки."
fi

# --- Python ---------------------------------------------------------------------------

PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand"; then
    PY="$(command -v "$cand")"; break
  fi
done

if [ -z "$PY" ] && [ "$PLATFORM" = "termux" ]; then
  export DEBIAN_FRONTEND=noninteractive
  APT_OPTS=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
  # Полное обновление обязательно: частично обновлённый Termux не может поставить python («unmet dependencies»).
  step "Обновляю Termux (может занять несколько минут)" pkg upgrade "${APT_OPTS[@]}" \
    || fail "Не удалось обновить Termux" "Проверь интернет. Если в отчёте ошибки про зеркало или «unmet dependencies» — выполни termux-change-repo, выбери другое зеркало и запусти установку снова."
  if ! step "Ставлю Python" pkg install "${APT_OPTS[@]}" python; then
    step "Пробую ещё раз после обновления списка пакетов" bash -c 'pkg update -y && pkg install -y python' \
      || fail "Не удалось поставить Python" "Чаще всего виновато несинхронизированное зеркало: выполни termux-change-repo, выбери другое зеркало и запусти установку снова."
  fi
  PY="$(command -v python)"
  py_ok "$PY" || PY=""
fi

if [ -z "$PY" ]; then
  case "$PLATFORM" in
    macos) HINT="Поставь Python с python.org или через Homebrew: brew install python — и запусти установку снова." ;;
    termux) HINT="Выполни pkg install python и запусти установку снова." ;;
    *) HINT="Debian/Ubuntu — sudo apt install python3; Fedora — sudo dnf install python3; Arch — sudo pacman -S python. Потом запусти установку снова." ;;
  esac
  fail "Не найден Python 3.10 или новее" "$HINT"
fi
echo "  ${DIM}Python: $("$PY" --version 2>&1) ($PY)${RESET}"

step "Проверяю, что в Python есть всё нужное (sqlite3, ssl, asyncio)" "$PY" -c 'import sqlite3, ssl, asyncio, zlib, hashlib, json' \
  || fail "В этом Python не хватает стандартных модулей" "Обычно это сборка без sqlite3 или ssl. Debian/Ubuntu: sudo apt install python3 libsqlite3-0 libssl3; в Termux: pkg reinstall python."
step "Проверяю, что Бендер запускается" "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import bender.core.app, bender.web.api, bender.importers.openclaw; from bender.__main__ import main; sys.argv = ["bender", "--version"]; main()' "$HERE" \
  || fail "Бендер не запускается" "Посмотри хвост лога в отчёте — там будет ошибка Python."

# --- команда bender -------------------------------------------------------------------

# Путь к bash прописывается явно: в Termux нет /usr/bin/env без подмены termux-exec.
BASH_PATH="$(command -v bash)"
# Папка программы — первой в пути импорта: так не важно, из какой папки запускают.
cat > "$HERE/bender.sh" <<EOF
#!$BASH_PATH
exec "$PY" -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from bender.__main__ import main; sys.exit(main())' "$HERE" "\$@"
EOF
chmod +x "$HERE/bender.sh"

if [ "$PLATFORM" = "termux" ]; then
  LAUNCHER="$PREFIX/bin/bender"
else
  LAUNCHER="$HOME/.local/bin/bender"
fi
mkdir -p "$(dirname "$LAUNCHER")" 2>/dev/null
if cp "$HERE/bender.sh" "$LAUNCHER" 2>/dev/null && chmod +x "$LAUNCHER"; then
  LAUNCH_CMD="bender"
  case ":$PATH:" in
    *":$(dirname "$LAUNCHER"):"*) ;;
    *) LAUNCH_CMD="$LAUNCHER"; PATH_HINT=1 ;;
  esac
else
  LAUNCHER="$HERE/bender.sh"
  LAUNCH_CMD="$HERE/bender.sh"
fi
echo "  ${GREEN}✓${RESET} Команда: $LAUNCH_CMD"

# --- автозапуск -----------------------------------------------------------------------

SERVICE_NOTE=""
if [ "$SERVICE" = 1 ]; then
  # Та же реализация доступна после установки как bender service install|remove|status.
  if ! SERVICE_NOTE=$("$LAUNCHER" --data "${BENDER_HOME:-$HERE/data}" service install 2>> "$LOG"); then
    fail "Автозапуск не настроен" "$SERVICE_NOTE Программа установлена; её можно запустить вручную: $LAUNCH_CMD"
  fi
  printf '%s\n' "$SERVICE_NOTE" >> "$LOG"
fi

# --- готово ---------------------------------------------------------------------------

echo ""
echo "  ${GREEN}${BOLD}✓ Готово!${RESET}"
echo ""
echo "  Запуск:            ${AMBER}${BOLD}$LAUNCH_CMD${RESET}"
echo "  Переезд из OpenClaw: ${AMBER}$LAUNCH_CMD import openclaw${RESET}  ${DIM}(или кнопка «Переселить» в пульте)${RESET}"
echo "  Разговор в терминале: $LAUNCH_CMD chat · Полигон: $LAUNCH_CMD try"
if [ -n "${PATH_HINT:-}" ]; then
  echo "  ${DIM}Чтобы писать просто «bender», добавь в ~/.bashrc: export PATH=\"$(dirname "$LAUNCHER"):\$PATH\"${RESET}"
fi
if [ -n "$SERVICE_NOTE" ]; then
  echo ""
  echo "  $SERVICE_NOTE"
fi
echo ""
if [ "$PLATFORM" = "termux" ]; then
  echo "  Пульт откроется в браузере телефона: http://127.0.0.1:8770"
  echo "  ${DIM}Данные лежат в $HERE/data (или в BENDER_HOME). При удалении Termux они сотрутся.${RESET}"
  echo "  ${DIM}Чтобы Android не усыплял Бендера: termux-wake-lock.${RESET}"
else
  echo "  Браузер с пультом откроется сам. ${DIM}Данные лежат в $HERE/data (или в BENDER_HOME).${RESET}"
fi
echo ""
