#!/bin/sh
# Прогон UI-сценария: tests/ui/run.sh flow|chatgpt|memory|heart|channels|tasks|claudecode|mail|mcp|skills|search|sandbox|cliengines|modules|migration|roles|relocate|versions|exams|experience|workshoptools|metrics|finder [папка для снимков]
# Сам поднимает фальшивые модели (и Bot API), готовит папку данных, запускает Бендера, гоняет Chromium и всё гасит.
# Процессы гасятся по PID — не pkill -f (шаблон из той же команды убьёт саму оболочку).
set -u
NAME=${1:?flow, chatgpt, memory, heart, channels, tasks, claudecode, mail, mcp, skills, search, sandbox, cliengines, modules, migration, roles, relocate, versions, exams, experience, workshoptools или metrics}
OUT=${2:-$(mktemp -d)}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT" || exit 1
# OAuth-прогон использует свой localhost CA; TLS приложения остаётся проверяемым.
if [ "$NAME" = "userbot" ]; then BENDER_USERBOT_HELPER="$ROOT/tests/fixtures/fake_userbot.py"; export BENDER_USERBOT_HELPER; fi
if [ "$NAME" = "mcpoauth" ]; then SSL_CERT_FILE="$ROOT/tests/fixtures/mcp-oauth-cert.pem"; export SSL_CERT_FILE; fi
# Фальшивая почта занимает ещё два соседних порта: оставляем каждой группе десять.
FAKE_PORT=$((5290 + 10 * $(printf '%s' "$NAME" | wc -c)))
WEB_PORT=$((8790 + $(printf '%s' "$NAME" | wc -c)))
DATA=$(mktemp -d)/data
# Сценарий переезда меняет только свою копию исходной фикстуры.
if [ "$NAME" = "relocate" ]; then
  OC_PATH="$OUT/openclaw-source"
  export OC_PATH
  python3 - "$ROOT" "$OC_PATH" <<'PY'
import shutil, sys
from pathlib import Path
source = Path(sys.argv[1]) / "tests/fixtures/openclaw-2026.10.5/.openclaw"
shutil.copytree(source, sys.argv[2], dirs_exist_ok=True)
PY
fi
FPID=""
PID=""
cleanup() {
  if [ -n "$PID" ]; then kill "$PID" 2>/dev/null || true; fi
  if [ -n "$FPID" ]; then kill "$FPID" 2>/dev/null || true; fi
  if [ -n "$PID" ]; then wait "$PID" 2>/dev/null || true; fi
  if [ -n "$FPID" ]; then wait "$FPID" 2>/dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python3 -m bender fake-llm --port "$FAKE_PORT" > "$OUT/fake.log" 2>&1 &
FPID=$!
sleep 1
# без своей подготовки сценарий начинает с чистой папки (первый запуск)
if [ -f "tests/ui/${NAME}_setup.py" ] && ! python3 "tests/ui/${NAME}_setup.py" "$DATA" "$FAKE_PORT"; then exit 1; fi
# вход через ChatGPT — против фальшивого OpenAI
BENDER_CHATGPT_AUTH_URL="http://127.0.0.1:$FAKE_PORT"; BENDER_CHATGPT_BACKEND_URL="http://127.0.0.1:$FAKE_PORT/backend-api/codex"
export BENDER_CHATGPT_AUTH_URL BENDER_CHATGPT_BACKEND_URL
# сценарий может положить свои программы (например, фальшивый claude) в bin/ рядом с данными
if [ -d "$(dirname "$DATA")/bin" ]; then PATH="$(dirname "$DATA")/bin:$PATH"; export PATH; fi
WATCHDOG_FLAG=--no-watchdog
if [ "$NAME" = "versions" ] || [ "$NAME" = "control" ]; then WATCHDOG_FLAG=""; fi
python3 -m bender --data "$DATA" run $WATCHDOG_FLAG --no-browser --port "$WEB_PORT" > "$OUT/bender.log" 2>&1 &
PID=$!
# На нагруженном хосте двух секунд мало: ждём готовность входа, не печатая токен.
READY_TRIES=0
while [ ! -s "$DATA/local/web_token" ]; do
  if ! kill -0 "$PID" 2>/dev/null; then echo "Бендер завершился до запуска пульта; смотри $OUT/bender.log"; exit 1; fi
  READY_TRIES=$((READY_TRIES + 1))
  if [ "$READY_TRIES" -ge 80 ]; then echo "Пульт не успел запуститься; смотри $OUT/bender.log"; exit 1; fi
  sleep 0.25
done
BENDER_UI_TOKEN=$(cat "$DATA/local/web_token")
BENDER_URL="http://127.0.0.1:$WEB_PORT" BENDER_UI_TOKEN="$BENDER_UI_TOKEN" FAKE_URL="http://127.0.0.1:$FAKE_PORT" NODE_OPTIONS="${NODE_OPTIONS:-} --require=\"$ROOT/tests/ui/auth.cjs\"" timeout 180 node "tests/ui/$NAME.js" "$OUT"
RC=$?
echo "снимки и журналы: $OUT"
exit $RC
