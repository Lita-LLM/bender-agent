"""Переезд из OpenClaw: личность, память, дневник, навыки, модели с ключами, история разговоров.

Как устроен OpenClaw на диске (замерено на 2026.10.5):

    ~/.openclaw/                      папка состояния (OPENCLAW_STATE_DIR, профили — ~/.openclaw-<имя>)
      openclaw.json                   конфиг в JSON5: агенты, модели, каналы, MCP, пульс
      state/openclaw.sqlite           общая база: расписания (cron_jobs), хэши заготовок, секреты
      agents/<id>/agent/openclaw-agent.sqlite
                                      база агента: стенограммы (transcript_events), сессии, ключи моделей
      workspace/                      рабочая папка агента: IDENTITY/SOUL/USER/MEMORY/AGENTS.md,
                                      memory/ГГГГ-ММ-ДД.md, skills/, avatars/ и файлы хозяина

Правила (общие для импортёров — importers/__init__.py): источник только читается; сначала план,
потом перенос; перед переносом — снимок данных Бендера; повторный перенос не плодит дубли.

Заготовки OpenClaw не переносятся: база хранит sha256 того, что OpenClaw сгенерировал сам
(workspace_generated_bootstrap_hashes) — файл с таким хэшем хозяин и агент не трогали.
"""

from __future__ import annotations

from ..core.pathutil import relative_path, within

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core import json5
from ..core.errors import HumanError
from ..core.fsutil import atomic_write_text
from ..core.ids import id_at
from ..core.paths import PROGRAM_DIR
from ..core.secrets import mask
from ..core.words import count
from ..memory import selfdir as sdm
from ..router.config import PRESETS, normalize_base_url

# Замеры OpenClaw 2026.10.5 на машине разработки: для ритуала, если
# живой OpenClaw сейчас не запущен и мерить нечего.
REFERENCE = {"version": "2026.10.5", "rss_mb": 1400, "install_mb": 619, "packages": 344, "startup_s": 22.7}

PERSONA = ("IDENTITY.md", "SOUL.md", "USER.md", "MEMORY.md", "AGENTS.md", "TOOLS.md", "HEARTBEAT.md", "DREAMS.md", "BOOT.md", "BOOTSTRAP.md")
HANDLED_DIRS = ("memory", "skills", "avatars")
SKIP_TOP = {".git", ".openclaw", ".DS_Store", "node_modules", "__pycache__", ".venv", ".cache"}
WORK_LIMIT_MB = 200

# Встроенные провайдеры OpenClaw (ключ есть, адреса в конфиге нет) → пресет или адрес Бендера.
OC_PROVIDERS = {
    "openrouter": "openrouter", "openai": "openai", "anthropic": "anthropic", "deepseek": "deepseek", "groq": "groq",
    "google": "gemini", "gemini": "gemini", "mistral": "mistral", "ollama": "ollama", "lmstudio": "lmstudio",
    "xai": {"name": "xAI", "kind": "openai", "base_url": "https://api.x.ai/v1"},
    "together": {"name": "Together", "kind": "openai", "base_url": "https://api.together.xyz/v1"},
    "fireworks": {"name": "Fireworks", "kind": "openai", "base_url": "https://api.fireworks.ai/inference/v1"},
    "cerebras": {"name": "Cerebras", "kind": "openai", "base_url": "https://api.cerebras.ai/v1"},
    "moonshot": {"name": "Moonshot", "kind": "openai", "base_url": "https://api.moonshot.ai/v1"},
    "perplexity": {"name": "Perplexity", "kind": "openai", "base_url": "https://api.perplexity.ai"},
    "nvidia": {"name": "NVIDIA", "kind": "openai", "base_url": "https://integrate.api.nvidia.com/v1"},
}
OC_API_KIND = {"openai-completions": "openai", "openai-responses": "openai", "openai": "openai", "anthropic-messages": "anthropic", "anthropic": "anthropic", "ollama": "openai"}
OAUTH_NOTE = "вход по подписке (OAuth) не переносится — в Бендере войдёшь заново"
SECRET_KEY = re.compile(r"(token|secret|password|passwd|api[_-]?key|apikey|credential|private[_-]?key)$", re.I)
SKIP_CREATED_VIA = {"cron", "spawn", "internal", "plugin"}


# --- где OpenClaw ------------------------------------------------------------------------------


@dataclass
class Agent:
    id: str
    name: str
    workspace: Path | None
    agent_dir: Path | None
    db: Path | None
    default: bool = False
    config: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "default": self.default, "workspace": str(self.workspace or ""), "has_db": bool(self.db)}


class Install:
    """Одна установка OpenClaw (папка состояния). Только чтение."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.config: dict = {}
        self.config_error = ""
        cfg = self.root / "openclaw.json"
        if cfg.is_file():
            try:
                data = json5.loads(cfg.read_text(encoding="utf-8"))
                self.config = data if isinstance(data, dict) else {}
            except (ValueError, OSError) as e:
                self.config_error = str(e)
        self.version = str((self.config.get("meta") or {}).get("lastTouchedVersion") or "")
        st = self.root / "state" / "openclaw.sqlite"
        self.state_db = st if st.is_file() else None
        self.agents = self._agents()

    def _remap(self, raw, fallbacks: list[Path]) -> Path | None:
        """Пути в конфиге — с машины и папки, где он писался. Сначала ищем внутри выбранной установки
        (хозяин мог указать копию или бэкап), потом по пути как есть."""
        cands: list[Path] = []
        if raw:
            p = Path(str(raw)).expanduser()
            for i, part in enumerate(p.parts):
                if part.startswith(".openclaw"):
                    cands.append(self.root.joinpath(*p.parts[i + 1:]))
                    break
            cands.append(p if p.is_absolute() else self.root / p)
        for c in cands + fallbacks:
            if c.exists():
                return c
        return None

    def _agents(self) -> list[Agent]:
        ag = self.config.get("agents") if isinstance(self.config.get("agents"), dict) else {}
        defaults = ag.get("defaults") if isinstance(ag.get("defaults"), dict) else {}
        entries: dict[str, dict] = {}
        if isinstance(ag.get("entries"), dict):
            entries.update({str(k): (v if isinstance(v, dict) else {}) for k, v in ag["entries"].items()})
        if isinstance(ag.get("list"), list):  # старая форма конфига
            for e in ag["list"]:
                if isinstance(e, dict) and e.get("id"):
                    entries.setdefault(str(e["id"]), e)
        if not entries:
            found = sorted(p.name for p in (self.root / "agents").glob("*") if (p / "agent").is_dir()) if (self.root / "agents").is_dir() else []
            entries = {a: {} for a in (found or ["main"])}
        out = []
        for aid, e in entries.items():
            ws_default = self.root / ("workspace" if aid == "main" or len(entries) == 1 else f"workspace-{aid}")
            ws = self._remap(e.get("workspace") or defaults.get("workspace"), [ws_default, self.root / "workspace", self.root / f"workspace-{aid}"])
            ad = self._remap(e.get("agentDir"), [self.root / "agents" / aid / "agent"])
            db = (ad / "openclaw-agent.sqlite") if ad else None
            ident = e.get("identity") if isinstance(e.get("identity"), dict) else {}
            out.append(Agent(aid, str(e.get("name") or ident.get("name") or aid), ws, ad, db if db and db.is_file() else None, bool(e.get("default")), e))
        if out and not any(a.default for a in out):
            (next((a for a in out if a.id == "main"), out[0])).default = True
        return out

    def agent(self, aid: str | None = None) -> Agent:
        if aid:
            for a in self.agents:
                if a.id == aid:
                    return a
            raise HumanError("no_agent", f"В OpenClaw нет агента «{aid}»", "Есть: " + ", ".join(a.id for a in self.agents))
        return next(a for a in self.agents if a.default)

    def public(self) -> dict:
        return {"path": str(self.root), "version": self.version, "config_error": self.config_error, "agents": [a.public() for a in self.agents]}


def _looks_like_install(p: Path) -> bool:
    return (p / "openclaw.json").is_file() or (p / "state" / "openclaw.sqlite").is_file()


def candidates(hint: str | None = None) -> list[Path]:
    out: list[Path] = []

    def add(p) -> None:
        if not p:
            return
        p = Path(str(p)).expanduser()
        if p.name == "openclaw.json":
            p = p.parent
        if not _looks_like_install(p) and (p / ".openclaw").is_dir():
            p = p / ".openclaw"
        if p not in out:
            out.append(p)

    if hint:
        add(hint)
        return out
    add(os.environ.get("OPENCLAW_STATE_DIR"))
    if os.environ.get("OPENCLAW_HOME"):
        add(Path(os.environ["OPENCLAW_HOME"]) / ".openclaw")
    home = Path.home()
    add(home / ".openclaw")
    for p in sorted(home.glob(".openclaw-*")):
        if p.is_dir():
            add(p)
    return out


def find_installs(hint: str | None = None, *, app=None) -> list[Install]:
    found=[]
    for p in candidates(hint):
        if not p.is_dir() or not _looks_like_install(p):continue
        if app is not None:
            from .sourceguard import check
            check(app,p)
            for file in (p/'openclaw.json',p/'state'/'openclaw.sqlite'):
                if file.exists():check(app,file,p)
        found.append(Install(p))
    if hint and not found:
        raise HumanError("no_openclaw", "Здесь нет OpenClaw", f"В {hint} нет ни openclaw.json, ни state/openclaw.sqlite. Укажи папку состояния OpenClaw — обычно ~/.openclaw.")
    return found


# --- чтение баз ------------------------------------------------------------------------------


class ReadCopies:
    """Базы OpenClaw только на чтение: согласованные копии (sqlite backup) во временной папке.

    Не в память: у активного хозяина база агента бывает на сотни мегабайт, а Бендер живёт и на телефоне.
    Источник не меняется. Копии удаляются при close().
    """

    def __init__(self, app=None):
        self.app=app
        self._tmp = tempfile.TemporaryDirectory(prefix="bender-oc-")
        self._open: list[sqlite3.Connection] = []

    def open(self, path: Path | None) -> sqlite3.Connection | None:
        if not path:
            return None
        if self.app is not None:
            from .sourceguard import check
            check(self.app,path)
        dst = Path(self._tmp.name) / f"{len(self._open)}-{Path(path).name}"
        out = sqlite3.connect(str(dst))
        try:
            src = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            try:
                src.backup(out)
            finally:
                src.close()
        except sqlite3.Error:
            # например, база в WAL на папке без права записи — читаем копию файлов
            out.close()
            for suffix in ("", "-wal"):
                f = Path(str(path) + suffix)
                if f.exists():
                    shutil.copy2(f, Path(str(dst) + suffix))
            out = sqlite3.connect(str(dst))
        out.row_factory = sqlite3.Row
        self._open.append(out)
        return out

    def close(self) -> None:
        for db in self._open:
            db.close()
        self._open.clear()
        self._tmp.cleanup()


def rows(db: sqlite3.Connection | None, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Запрос, которому всё равно, есть ли таблица: у разных версий OpenClaw разные схемы."""
    if db is None:
        return []
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _unzstd(blob: bytes) -> str | None:
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        return None
    try:
        return zstd.decompress(blob).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None


# --- мелочи ----------------------------------------------------------------------------------


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def meaningful(text: str) -> bool:
    """Есть ли в файле что-то кроме заголовков, комментариев и пустых полей анкеты."""
    t = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    for ln in t.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or s in ("---", "***"):
            continue
        if re.fullmatch(r"[-*]?\s*\**[^:*]{1,40}:\**\s*(_?\(.*\)_?)?", s):
            continue
        return True
    return False


def body_without_title(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def section(text: str, title: str) -> str:
    """Текст раздела «## title» (без заголовка) или пусто."""
    m = re.search(rf"^##\s+{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", text, flags=re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def dir_size(p: Path, limit_files: int = 50000) -> int:
    total = 0
    n = 0
    if p.is_file():
        return p.stat().st_size
    for root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
            n += 1
            if n > limit_files:
                return total
    return total


def parse_duration_minutes(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v) / 60000
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*", str(v or ""))
    if not m:
        return None
    n = float(m.group(1))
    return n * {"ms": 1 / 60000, "s": 1 / 60, "m": 1, "h": 60, "d": 1440}[m.group(2) or "m"]


def resolve_secret(v, warnings: list[str], where: str) -> str | None:
    """apiKey OpenClaw: строка, "${ENV}" или SecretRef {source: env|file|exec, id}."""
    if isinstance(v, str):
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", v.strip())
        if m:
            val = os.environ.get(m.group(1))
            if not val:
                warnings.append(f"{where}: ключ берётся из переменной {m.group(1)}, а её сейчас нет — перенесу без ключа")
            return val
        return v.strip() or None
    if isinstance(v, dict):
        src = v.get("source")
        if src == "env":
            val = os.environ.get(str(v.get("id") or ""))
            if not val:
                warnings.append(f"{where}: ключ в переменной {v.get('id')}, её сейчас нет — перенесу без ключа")
            return val
        warnings.append(f"{where}: ключ хранится через {src or 'ссылку'} — такие не переношу, вставишь в Бендере")
    return None


def redact(obj, key: str = ""):
    """Копия конфига для архива: значения секретных ключей замаскированы."""
    if isinstance(obj, dict):
        return {k: redact(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, key) for v in obj]
    if isinstance(obj, str) and SECRET_KEY.search(key or ""):
        return mask(obj)
    return obj


def walk_secrets(obj, path: str = ""):
    """Пары (путь, значение) для строковых секретов внутри конфига."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, str) and SECRET_KEY.search(k) and v.strip():
                yield p, v
            else:
                yield from walk_secrets(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_secrets(v, f"{path}[{i}]")


# --- стенограммы -----------------------------------------------------------------------------


def _content(c) -> tuple[str, list[tuple[str, str]]]:
    if isinstance(c, str):
        return c, []
    texts: list[str] = []
    tools: list[tuple[str, str]] = []
    for b in c if isinstance(c, list) else []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            texts.append(str(b.get("text") or ""))
        elif t in ("toolCall", "tool_use", "toolUse"):
            args = b.get("arguments", b.get("input", ""))
            args = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            tools.append((str(b.get("name") or "?"), args[:120] + ("…" if len(args) > 120 else "")))
        elif t == "image":
            texts.append("[картинка]")
    return "\n".join(x for x in texts if x), tools


def _ts(e: dict, m: dict) -> float:
    v = m.get("timestamp")
    if isinstance(v, (int, float)) and v > 0:
        return v / 1000 if v > 1e11 else float(v)
    raw = e.get("timestamp")
    if isinstance(raw, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def convert_event(e: dict) -> dict | None:
    """Событие стенограммы OpenClaw (формат pi-agent) → сообщение Бендера или None.

    Тулы переносятся строчками «⚙️ имя(аргументы)» в тексте ответа, без пар вызов/результат:
    у Бендера другие тулы, и сломанная пара испортила бы историю для провайдеров.
    """
    if e.get("type") != "message":
        return None
    m = e.get("message") if isinstance(e.get("message"), dict) else {}
    role = m.get("role")
    if role not in ("user", "assistant"):
        return None
    text, tools = _content(m.get("content"))
    oc = m.get("__openclaw") if isinstance(m.get("__openclaw"), dict) else {}
    if role == "user" and (oc.get("heartbeat") or text.lstrip().startswith("Read HEARTBEAT.md")):
        return {"skip": "heartbeat"}
    if role == "assistant":
        if text.strip() in ("HEARTBEAT_OK", "NO_REPLY"):
            return {"skip": "heartbeat"}
        if tools:
            text = (text + "\n\n" if text else "") + "\n".join(f"⚙️ {n}({a})" for n, a in tools)
    if not text.strip():
        return None
    return {"role": role, "content": text, "ts": _ts(e, m), "oc_id": e.get("id") or ""}


def events_to_messages(events: list[dict]) -> list[dict]:
    """События одной ветки (по порядку) → сообщения Бендера. Пульс (вопрос и ответ HEARTBEAT_OK) отбрасывается."""
    out: list[dict] = []
    skip_next_reply = False
    for e in events:
        m = convert_event(e) if isinstance(e, dict) else None
        if not m:
            continue
        if "skip" in m:
            skip_next_reply = m["skip"] == "heartbeat"
            continue
        if skip_next_reply and m["role"] == "assistant":
            skip_next_reply = False
            continue
        skip_next_reply = False
        out.append(m)
    return out


def read_transcript(adb: sqlite3.Connection, sid: str) -> tuple[list[dict], str]:
    """Сообщения одной сессии OpenClaw по активной ветке. Возвращает (сообщения, откуда взяты)."""
    order = [r[0] for r in rows(adb, "SELECT event_seq FROM session_transcript_active_events WHERE session_id = ? ORDER BY active_position", (sid,))]
    evs = {r["seq"]: r for r in rows(adb, "SELECT seq, event_json, event_zstd FROM transcript_events WHERE session_id = ? ORDER BY seq", (sid,))}
    seqs = [s for s in order if s in evs] or sorted(evs)
    events: list[dict] = []
    undecoded = 0
    for s in seqs:
        ev = evs[s]
        raw = ev["event_json"]
        if raw is None and ev["event_zstd"] is not None:
            raw = _unzstd(ev["event_zstd"])
            if raw is None:
                undecoded += 1
                continue
        try:
            e = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(e, dict):
            events.append(e)
    out = events_to_messages(events)
    if undecoded:
        # Сжатые события (zstd) без Python 3.14: текст сообщений есть в полнотекстовом индексе OpenClaw.
        fts = rows(adb, "SELECT c0, c2, c3, c4 FROM session_transcript_fts_content WHERE c1 = ? ORDER BY id", (sid,))
        if fts:
            out = [{"role": r[2], "content": str(r[0] or ""), "ts": float(r[3] or 0) / 1000, "oc_id": r[1] or ""} for r in fts if r[2] in ("user", "assistant") and r[0]]
            return out, "fts"
        return out, "partial"
    return out, "events"


# --- старые стенограммы JSONL (до переезда OpenClaw на SQLite) ---------------------------------
#
# agents/<агент>/sessions/: sessions.json (ключ сессии → {sessionId, sessionFile?, displayName?, …})
# и <sessionId>.jsonl — построчные события того же формата, что и в базе. Архивы:
# <sessionId>.jsonl.reset.<время>[.<хеш>][.zst] — история до /new (берём), .deleted. — удалённое
# хозяином (не берём), .bak, .trajectory.jsonl, .checkpoint.<uuid>.jsonl — служебное (не берём).
# После `openclaw doctor --fix` файлы уезжают в session-sqlite-import-archive/ — их история уже в базе.

RESET_ARCHIVE = re.compile(r"^(.+)\.jsonl\.reset\.(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:\.\d{3})?Z)(?:\.[0-9a-f]{32})?(\.zst)?$")


def legacy_sessions_dir(install: "Install", agent: "Agent") -> Path | None:
    cands = []
    if agent.agent_dir:
        cands.append(Path(agent.agent_dir).parent / "sessions")
    cands.append(install.root / "agents" / agent.id / "sessions")
    if agent.default:
        cands.append(install.root / "sessions")
    for c in cands:
        if c.is_dir():
            return c
    return None


def active_branch(events: list[dict]) -> list[dict]:
    """Стенограмма — дерево (id/parentId), ветку переключают события leaf (targetId). Берётся активная ветка:
    от последнего листа к корню. Старые файлы без id — как есть, по порядку."""
    byid = {e["id"]: e for e in events if isinstance(e.get("id"), str) and e.get("type") not in ("session", "leaf")}
    if not byid:
        return [e for e in events if e.get("type") != "session"]
    leaf = None
    for e in events:
        t = e.get("type")
        if t == "session":
            continue
        if t == "leaf":
            leaf = e.get("targetId") or leaf
        elif isinstance(e.get("id"), str):
            leaf = e["id"]
    chain, seen, cur = [], set(), leaf
    while cur and cur in byid and cur not in seen:
        seen.add(cur)
        chain.append(byid[cur])
        cur = byid[cur].get("parentId")
    chain.reverse()
    return chain


def read_jsonl(path: Path) -> tuple[list[dict], str]:
    """Сообщения из файла стенограммы. Возвращает (сообщения, «jsonl» или «partial» — не прочитался)."""
    try:
        data = path.read_bytes()
        if path.name.endswith(".zst"):
            text = _unzstd(data)
            if text is None:
                return [], "partial"
        else:
            text = data.decode("utf-8", errors="replace")
    except OSError:
        return [], "partial"
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            events.append(e)
    return events_to_messages(active_branch(events)), "jsonl"


def legacy_sessions(sdir: Path) -> tuple[dict[str, list[Path]], dict[str, dict]]:
    """Файлы стенограмм по ключам сессий: ({ключ: [файлы, старые архивы раньше]}, {ключ: запись sessions.json})."""
    store: dict = {}
    try:
        raw = json.loads((sdir / "sessions.json").read_text(encoding="utf-8"))
        store = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        pass
    by_name: dict[str, str] = {}
    by_sid: dict[str, str] = {}
    for key, entry in store.items():
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("sessionFile"), str) and entry["sessionFile"].strip():
            by_name[Path(entry["sessionFile"].strip()).name] = key
        if isinstance(entry.get("sessionId"), str) and entry["sessionId"].strip():
            by_sid[entry["sessionId"].strip()] = key
            by_name.setdefault(entry["sessionId"].strip() + ".jsonl", key)
    groups: dict[str, list[tuple[int, float, Path]]] = {}
    for p in sorted(sdir.iterdir()) if sdir.is_dir() else []:
        if not p.is_file():
            continue
        name = p.name
        if name.endswith(".jsonl") and not name.endswith(".trajectory.jsonl") and ".checkpoint." not in name:
            sid, live = name[:-6], 1
        else:
            m = RESET_ARCHIVE.match(name)
            if not m:
                continue
            sid, live = m.group(1), 0
        key = by_name.get(name) if live else None
        key = key or by_sid.get(sid) or f"?:{sid}"
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        groups.setdefault(key, []).append((live, mtime, p))
    return {k: [p for _, _, p in sorted(v, key=lambda x: (x[0], x[1]))] for k, v in groups.items()}, {k: v for k, v in store.items() if isinstance(v, dict)}


def read_sources(adb: sqlite3.Connection | None, sources: list) -> tuple[list[dict], set[str]]:
    """Сообщения из нескольких источников одной сессии (окна в базе, файлы JSONL) без повторов, по времени."""
    msgs: list[dict] = []
    how: set[str] = set()
    seen: set[str] = set()
    for kind, ref in sources:
        if kind == "db":
            if adb is None:
                continue
            m, h = read_transcript(adb, ref)
        else:
            m, h = read_jsonl(Path(ref))
        how.add(h)
        for x in m:
            if x["oc_id"] and x["oc_id"] in seen:
                continue
            seen.add(x["oc_id"])
            msgs.append(dict(x, oc_session=ref if kind == "db" else Path(ref).name))
    msgs.sort(key=lambda x: x["ts"] or 0)  # sort устойчивый: без времени — в исходном порядке
    return msgs, how


# --- план ------------------------------------------------------------------------------------


@dataclass
class Item:
    id: str
    kind: str        # self | diary | skill | avatar | work | provider | role | setting | session | secret | archive | ritual
    title: str
    action: str      # replace | append | set | copy | create | merge | archive | skip
    detail: str = ""
    note: str = ""
    selected: bool = True
    secret: bool = False
    payload: dict = field(default_factory=dict, repr=False)

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "action": self.action, "detail": self.detail,
                "note": self.note, "selected": self.selected, "secret": self.secret}


@dataclass
class Plan:
    install: Install
    agent: Agent
    items: list[Item]
    warnings: list[str]
    stats: dict

    def public(self) -> dict:
        return {"source": self.install.public(), "agent": self.agent.public(), "items": [i.public() for i in self.items],
                "warnings": self.warnings, "stats": self.stats}

    def item(self, iid: str) -> Item | None:
        return next((i for i in self.items if i.id == iid), None)


ACTION_WORDS = {"replace": "заменить", "append": "дописать", "set": "записать", "copy": "скопировать", "create": "создать",
                "merge": "добавить к своему", "archive": "в архив", "skip": "пропустить"}


class Planner:
    def __init__(self, install: Install, agent: Agent, app):
        self.inst = install
        self.agent = agent
        self.app = app
        self.items: list[Item] = []
        self.warnings: list[str] = []
        self.stats: dict = {}
        self.copies = ReadCopies(app)
        self.adb = self.copies.open(agent.db)
        self.sdb = self.copies.open(install.state_db)
        self.ws = agent.workspace
        self.cfg = install.config
        ag = self.cfg.get("agents") if isinstance(self.cfg.get("agents"), dict) else {}
        self.defaults = ag.get("defaults") if isinstance(ag.get("defaults"), dict) else {}
        self.template_hashes: dict[str, set[str]] = {}
        for r in rows(self.sdb, "SELECT filename, sha256 FROM workspace_generated_bootstrap_hashes"):
            self.template_hashes.setdefault(r[0], set()).add(r[1])

    def add(self, *a, **kw) -> Item:
        it = Item(*a, **kw)
        self.items.append(it)
        return it

    def opt(self, key: str):
        """Настройка агента: своя в agents.entries.<id>, иначе agents.defaults."""
        v = self.agent.config.get(key)
        return v if v not in (None, "") else self.defaults.get(key)

    # личность

    def ws_file(self, name: str) -> str | None:
        if not self.ws:
            return None
        p = self.ws / name
        if not p.is_file():
            return None
        from .sourceguard import check
        check(self.app,p,self.ws)
        raw = p.read_bytes()
        if sha256_bytes(raw) in self.template_hashes.get(name, set()):
            return None  # заготовка OpenClaw, никто не трогал
        text = raw.decode("utf-8", errors="replace")
        return text if meaningful(text) else None

    def self_item(self, iid: str, title: str, target: str, text: str, *, note: str = "") -> None:
        sd = self.app.selfdir
        cur = sd.read(target)
        body = text.strip()
        if body and body in cur:
            self.add(iid, "self", title, "skip", detail=f"→ self/{target}", note="уже перенесено")
            return
        action = "replace" if sd.is_template(target) else "append"
        self.add(iid, "self", title, action, detail=f"→ self/{target} · {len(body)} симв.", note=note or ("у Бендера уже есть свой — допишу разделом «Из OpenClaw»" if action == "append" else ""),
                 payload={"target": target, "text": body})

    def plan_persona(self) -> None:
        sd = self.app.selfdir
        ident_text = self.ws_file("IDENTITY.md")
        if ident_text:
            fields = sdm.parse_identity(ident_text)
            if fields.get("name"):
                now = sd.identity()
                cur = now.get("name")
                show = " ".join(x for x in (fields.get("name"), fields.get("emoji")) if x)
                if all(now.get(k) == v for k, v in fields.items()):
                    self.add("self:identity", "self", f"Имя и облик: {show}", "skip", detail="→ self/IDENTITY.md", note="уже перенесено")
                    return self._persona_rest()
                self.add("self:identity", "self", f"Имя и облик: {show}", "set", detail="→ self/IDENTITY.md · " + ", ".join(f"{k}: {v}" for k, v in fields.items() if k != "name"),
                         note=(f"сейчас Бендера зовут {cur} — имя сменится" if cur and cur != fields["name"] else ""), payload={"fields": fields})
        self._persona_rest()

    def _persona_rest(self) -> None:
        sd = self.app.selfdir
        for name, title in (("SOUL.md", "Душа"), ("USER.md", "Что агент знает о хозяине"), ("MEMORY.md", "Долговременная память"), ("DREAMS.md", "Дневник снов")):
            text = self.ws_file(name)
            if text:
                self.self_item(f"self:{name}", f"{title} ({name})", name, text)
        # пульс: в 2026.10 инструкции пульса — в «блокноте» задачи heartbeat, раньше — HEARTBEAT.md
        hb = self.ws_file("HEARTBEAT.md") or ""
        for r in rows(self.sdb, "SELECT s.content FROM cron_job_scratch s JOIN cron_jobs j ON j.store_key = s.store_key AND j.job_id = s.job_id WHERE j.declaration_key = ?", (f"heartbeat:{self.agent.id}",)):
            if r[0] and meaningful(r[0]):
                hb = r[0]
        if hb:
            self.self_item("self:HEARTBEAT.md", "Чек-лист пульса (HEARTBEAT.md)", "HEARTBEAT.md", hb)
        # заметки об окружении: TOOLS.md (до 2026.10) или раздел «## Tools» в AGENTS.md
        env = []
        tools = self.ws_file("TOOLS.md")
        if tools:
            env.append(body_without_title(tools))
        agents = self.ws_file("AGENTS.md")
        if agents and section(agents, "Tools"):
            env.append(section(agents, "Tools"))
        env_text = "\n\n".join(x for x in env if x.strip())
        if env_text:
            if env_text in sd.read("AGENTS.md"):
                self.add("self:env", "self", "Заметки об окружении (TOOLS.md)", "skip", detail="→ self/AGENTS.md, раздел «Окружение»", note="уже перенесено")
            else:
                self.add("self:env", "self", "Заметки об окружении (TOOLS.md)", "append", detail=f"→ self/AGENTS.md, раздел «Окружение» · {len(env_text)} симв.", payload={"text": env_text})
        if agents:
            self.add("self:agents-ref", "archive", "Правила работы из OpenClaw (AGENTS.md)", "archive",
                     detail="→ архив переезда", note="правила Бендера свои; агент при Переселении предложит, что из старых взять", payload={"name": "AGENTS.md", "text": agents})
        boot = self.ws_file("BOOT.md")
        if boot:
            self.add("self:boot-ref", "archive", "Чек-лист запуска (BOOT.md)", "archive", detail="→ архив переезда", payload={"name": "BOOT.md", "text": boot})
        # лимиты: большая память OpenClaw не должна обрезаться молча
        for name, key, top in (("MEMORY.md", "core_limit", 50000), ("USER.md", "user_limit", 20000)):
            it = self.find(f"self:{name}")
            if it and it.action in ("replace", "append"):
                cur_text = "" if it.action == "replace" else sd.read(name)
                need = len(cur_text) + len(it.payload["text"]) + 200
                limit = int(self.app.settings.get("memory", key, 3000))
                if need > limit:
                    new = min(top, ((int(need * 1.2) // 500) + 1) * 500)
                    self.add(f"setting:memory.{key}", "setting", f"Поднять лимит {name}: {limit} → {new} символов", "set",
                             detail="иначе память не влезет", note=("даже так не влезет — агент ужмёт при Переселении" if need > top else ""),
                             payload={"group": "memory", "changes": {key: new}})

    def find(self, iid: str) -> Item | None:
        return next((i for i in self.items if i.id == iid), None)

    # дневник, навыки, аватары, файлы

    def plan_files(self) -> None:
        app = self.app
        if not self.ws or not self.ws.is_dir():
            self.warnings.append("Рабочая папка агента OpenClaw не найдена — личность и дневник не перенесутся")
            return
        mem = self.ws / "memory"
        if mem.is_dir():
            files = []
            for p in sorted(mem.rglob("*.md")):
                rel = p.relative_to(mem)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                dated = len(rel.parts) == 1 and re.match(r"\d{4}-\d{2}-\d{2}", p.stem)
                target = (app.dirs.diary / rel.name) if dated else (app.dirs.diary / "openclaw" / rel)
                if target.is_file() and p.read_text(encoding="utf-8", errors="replace").strip() in target.read_text(encoding="utf-8", errors="replace"):
                    continue  # уже перенесено
                files.append((str(p), str(target)))
            if files:
                days = sorted(Path(s).stem for s, _ in files)
                self.add("diary", "diary", f"Дневник: {count(len(files), 'файл', 'файла', 'файлов')} ({days[0]} … {days[-1]})", "copy", detail="memory/*.md → diary/",
                         note="совпадающие дни допишутся", payload={"files": files})
        skills: dict[str, Path] = {}
        for base in (self.inst.root / "skills", self.ws / "skills"):
            if base.is_dir():
                for d in sorted(base.iterdir()):
                    if d.is_dir() and (d / "SKILL.md").is_file() and not d.name.startswith("."):
                        skills[d.name] = d
        for name, d in skills.items():
            target = app.dirs.skills / name
            if target.exists():
                ours = (target / ".quarantine").is_file() and '"openclaw"' in (target / ".quarantine").read_text(encoding="utf-8", errors="replace")
                self.add(f"skill:{name}", "skill", f"Навык {name}", "skip", detail=f"→ skills/{name}", note="уже перенесено" if ours else "у Бендера уже есть навык с таким именем")
            else:
                self.add(f"skill:{name}", "skill", f"Навык {name}", "copy", detail=f"→ skills/{name} · {dir_size(d) // 1024 + 1} КБ",
                         note="в карантине: просмотри пакет и одобри его на экране «Навыки»", payload={"src": str(d), "dst": str(target)})
        av = self.ws / "avatars"
        if av.is_dir() and any(av.iterdir()):
            dst = app.dirs.self_dir / "avatars"
            if all((dst / f.relative_to(av)).exists() for f in av.rglob("*") if f.is_file()):
                self.add("avatars", "avatar", "Аватары", "skip", detail="avatars/ → self/avatars/", note="уже перенесено")
            else:
                self.add("avatars", "avatar", "Аватары", "copy", detail="avatars/ → self/avatars/", payload={"src": str(av), "dst": str(dst)})
        for p in sorted(self.ws.iterdir()):
            if p.name in PERSONA or p.name in HANDLED_DIRS:
                continue
            if p.name in SKIP_TOP or p.name.startswith("."):
                continue
            target = app.dirs.work / p.name
            size = dir_size(p)
            mb = size / 1e6
            if target.exists():
                self.add(f"work:{p.name}", "work", f"{'Папка' if p.is_dir() else 'Файл'} {p.name}", "skip", detail="→ work/", note="в рабочей папке Бендера уже есть такое имя")
                continue
            self.add(f"work:{p.name}", "work", f"{'Папка' if p.is_dir() else 'Файл'} {p.name}", "copy", detail=f"→ work/{p.name} · {mb:.1f} МБ" if mb >= 0.1 else f"→ work/{p.name}",
                     selected=mb <= WORK_LIMIT_MB, note=(f"больше {WORK_LIMIT_MB} МБ — отметь, если нужно" if mb > WORK_LIMIT_MB else ""), payload={"src": str(p), "dst": str(target)})

    # модели

    def plan_models(self) -> None:
        provs: dict[str, dict] = {}
        mcfg = self.cfg.get("models") if isinstance(self.cfg.get("models"), dict) else {}
        for pid, p in (mcfg.get("providers") or {}).items():
            if not isinstance(p, dict):
                continue
            kind = OC_API_KIND.get(str(p.get("api") or "openai-completions"))
            base = p.get("baseUrl") or p.get("baseURL")
            preset = OC_PROVIDERS.get(pid) if isinstance(OC_PROVIDERS.get(pid), str) else None
            if not kind:
                self.warnings.append(f"Провайдер {pid}: вид API «{p.get('api')}» Бендер пока не умеет — пропускаю")
                continue
            if str(p.get("api")) == "ollama" and base and not str(base).rstrip("/").endswith("/v1"):
                base = str(base).rstrip("/") + "/v1"
            if not base and preset:
                base = PRESETS[preset]["base_url"]
            if not base:
                self.warnings.append(f"Провайдер {pid}: нет адреса — пропускаю")
                continue
            prices, models = {}, []
            for m in p.get("models") or []:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                c = m.get("cost") if isinstance(m.get("cost"), dict) else {}
                entry = {"id": str(m["id"])}
                if m.get("contextWindow"):
                    entry["context"] = m["contextWindow"]
                if c:
                    prices[str(m["id"])] = {"in": float(c.get("input") or 0), "out": float(c.get("output") or 0)}
                    entry["price"] = prices[str(m["id"])]
                    entry["free"] = not c.get("input") and not c.get("output")
                models.append(entry)
            keys = []
            raw_key = p.get("apiKey")
            provs[pid] = {"oc_id": pid, "name": (PRESETS[preset]["name"] if preset else pid), "kind": kind, "base_url": str(base), "preset": preset,
                          "keys": keys, "prices": prices, "models": models, "headers": p.get("headers") if isinstance(p.get("headers"), dict) else {}, "notes": [],
                          "_raw_key": raw_key}
        # ключи из базы агента (и общей, куда их переносит doctor)
        profiles: dict[str, dict] = {}
        for db in (self.adb, self.sdb):
            for r in rows(db, "SELECT store_json FROM auth_profile_store"):
                try:
                    store = json.loads(r[0])
                except (TypeError, ValueError):
                    continue
                for prof_id, prof in (store.get("profiles") or {}).items() if isinstance(store, dict) else []:
                    if isinstance(prof, dict):
                        profiles.setdefault(prof_id, prof)
        for prof_id, prof in profiles.items():
            pid = str(prof.get("provider") or prof_id.split(":")[0])
            typ = prof.get("type")
            if typ == "oauth":
                self.warnings.append(f"{prof_id}: {OAUTH_NOTE}")
                continue
            val = prof.get("key") if typ == "api_key" else prof.get("token") if typ == "token" else None
            if not val:
                val = resolve_secret(prof.get("keyRef") or prof.get("tokenRef"), self.warnings, prof_id)
            if not val:
                continue
            if pid not in provs:
                known = OC_PROVIDERS.get(pid)
                if isinstance(known, str):
                    pr = PRESETS[known]
                    provs[pid] = {"oc_id": pid, "name": pr["name"], "kind": pr["kind"], "base_url": pr["base_url"], "preset": known, "keys": [], "prices": {}, "models": [], "headers": {}, "notes": []}
                elif isinstance(known, dict):
                    provs[pid] = {"oc_id": pid, **known, "preset": None, "keys": [], "prices": {}, "models": [], "headers": {}, "notes": []}
                else:
                    self.warnings.append(f"Ключ {prof_id}: провайдера «{pid}» Бендер не знает — ключ не перенесу (добавь провайдера по адресу вручную)")
                    continue
            provs[pid]["keys"].append({"label": prof_id, "value": str(val)})
        for pid, pv in provs.items():
            raw = pv.pop("_raw_key", None)
            if raw:
                if isinstance(raw, str) and raw in profiles:
                    pass  # apiKey называет профиль — ключ уже взят из профилей
                else:
                    val = resolve_secret(raw, self.warnings, f"Провайдер {pid}")
                    if val and val not in [k["value"] for k in pv["keys"]]:
                        pv["keys"].insert(0, {"label": f"{pid} (конфиг)", "value": val})
        # модель эмбеддингов для поиска по памяти: memory.search (2026.10) или memorySearch (раньше)
        self.embed_ref = None
        mem = self.cfg.get("memory") if isinstance(self.cfg.get("memory"), dict) else {}
        msearch = next((c for c in (mem.get("search"), self.opt("memorySearch"), self.cfg.get("memorySearch")) if isinstance(c, dict) and c.get("model")), None)
        if msearch:
            epid = str(msearch.get("provider") or "openai")
            remote = msearch.get("remote") if isinstance(msearch.get("remote"), dict) else {}
            if remote.get("baseUrl"):
                epid = "memory-search"
                key = resolve_secret(remote.get("apiKey"), self.warnings, "memory.search.remote.apiKey")
                provs[epid] = {"oc_id": epid, "name": "Эмбеддинги (из OpenClaw)", "kind": "openai", "base_url": str(remote["baseUrl"]), "preset": None,
                               "keys": [{"label": "memory.search", "value": key}] if key else [], "prices": {}, "models": [], "headers": {}, "notes": []}
            elif epid not in provs and isinstance(OC_PROVIDERS.get(epid), str) and PRESETS[OC_PROVIDERS[epid]].get("is_local"):
                pr = PRESETS[OC_PROVIDERS[epid]]
                provs[epid] = {"oc_id": epid, "name": pr["name"], "kind": pr["kind"], "base_url": pr["base_url"], "preset": OC_PROVIDERS[epid], "keys": [], "prices": {}, "models": [], "headers": {}, "notes": []}
            if epid in provs and provs[epid]["kind"] == "openai":
                self.embed_ref = [epid, str(msearch["model"])]
            else:
                self.warnings.append(f"Эмбеддинги памяти ({epid}/{msearch.get('model')}): провайдер не переносится — поиск по смыслу настроишь в «Моделях»")
        self.providers = provs
        existing = self.app.models.providers().values()
        for pid, pv in provs.items():
            base = normalize_base_url(pv["base_url"])
            same = next((p for p in existing if normalize_base_url(p.get("base_url", "")) == base and p.get("kind") == pv["kind"]), None)
            keys = ", ".join(mask(k["value"]) for k in pv["keys"]) or "без ключа"
            title = f"Провайдер {pv['name']}" + (f" ({pid})" if pv["name"] != pid else "")
            detail = f"{pv['kind']} · {base} · ключ {keys}" + (f" · моделей {len(pv['models'])}" if pv["models"] else "")
            if same and not ({k["value"] for k in pv["keys"]} - {self.app.secrets.get(n) for n in same.get("keys") or []}):
                self.add(f"provider:{pid}", "provider", title, "skip", detail=detail, note=f"уже есть у Бендера ({same.get('name')})", payload=pv)
            elif same:
                self.add(f"provider:{pid}", "provider", title, "merge", detail=detail, note=f"у Бендера уже есть {same.get('name')} — добавлю ключи, которых там нет",
                         secret=bool(pv["keys"]), payload=pv)
            else:
                self.add(f"provider:{pid}", "provider", title, "create", detail=detail, secret=bool(pv["keys"]), payload=pv)
        # роли
        aliases = {}
        effort_params={}
        defaults_models=self.defaults.get('models') if isinstance(self.defaults.get('models'),dict) else {}
        own_models=self.agent.config.get('models') if isinstance(self.agent.config.get('models'),dict) else {}
        from ..router.effort import normalize
        for ref,meta in {**defaults_models,**own_models}.items():
            params=meta.get('params') if isinstance(meta,dict) else None
            if not isinstance(params,dict) or 'thinking' not in params:continue
            value=params['thinking']
            try:effort_params[ref]={'effort':normalize('off' if value is False or value=='disabled' else value)}
            except HumanError:self.warnings.append('Усилия модели OpenClaw '+str(ref)+': неизвестный уровень; выбери в «Модели».')
        for ref, meta in (self.defaults.get("models") or {}).items() if isinstance(self.defaults.get("models"), dict) else []:
            if isinstance(meta, dict) and meta.get("alias"):
                aliases[str(meta["alias"])] = ref

        def refs(v) -> list[str]:
            if isinstance(v, str):
                return [v] if v else []
            if isinstance(v, dict):
                return [x for x in [v.get("primary")] + list(v.get("fallbacks") or []) if isinstance(x, str) and x]
            return []

        def split(ref: str):
            ref = aliases.get(ref, ref)
            if "/" not in ref:
                return None
            pid, model = ref.split("/", 1)
            return pid, model

        roles = self.app.models.roles()
        for role, key, title in (("main", "model", "Главная"), ("fast", "utilityModel", "Быстрая"), ("vision", "imageModel", "Зрение"), ("embed", None, "Эмбеддинги")):
            entries, missing = [], []
            for ref in (["/".join(self.embed_ref)] if role == "embed" and self.embed_ref else [] if role == "embed" else refs(self.opt(key))):
                sp = split(ref)
                if not sp:
                    missing.append(ref)
                elif sp[0] in provs:
                    entries.append(list(sp))
                else:
                    missing.append(ref)
            if missing:
                self.warnings.append(f"Роль «{title}»: {', '.join(missing)} — провайдер не переносится, пропускаю")
            if not entries:
                continue
            show = " → ".join(f"{p}/{m}" for p, m in entries)
            have = {(e.get("provider"), e.get("model")) for e in roles.get(role) or []}
            if have and all((self._bender_pid(p), m) in have for p, m in entries):
                self.add(f"role:{role}", "role", f"Модель «{title}»: {show}", "skip", note="уже выбрана")
                continue
            if roles.get(role):
                self.add(f"role:{role}", "role", f"Модель «{title}»: {show}", "append", note="у Бендера уже выбрана — модели из OpenClaw встанут запасными", payload={"role": role, "entries": entries,'effort_params':effort_params})
            else:
                self.add(f"role:{role}", "role", f"Модель «{title}»: {show}", "set", payload={"role": role, "entries": entries,'effort_params':effort_params})

    def _bender_pid(self, oc_pid: str) -> str | None:
        pv = getattr(self, "providers", {}).get(oc_pid)
        if not pv:
            return None
        base = normalize_base_url(pv["base_url"])
        return next((p["id"] for p in self.app.models.providers().values() if normalize_base_url(p.get("base_url", "")) == base and p.get("kind") == pv["kind"]), None)

    # сессии

    def plan_sessions(self) -> None:
        sdir = legacy_sessions_dir(self.inst, self.agent)
        files, store = legacy_sessions(sdir) if sdir else ({}, {})
        if not self.adb and not files:
            if self.agent.agent_dir:
                self.warnings.append("База агента OpenClaw не найдена — история разговоров не перенесётся")
            return
        nodes = {r["session_key"]: dict(r) for r in rows(self.adb, "SELECT session_key, created_via, display_name, label FROM session_nodes")} if self.adb else {}
        groups: dict[str, list] = {}
        if self.adb:
            for r in rows(self.adb, "SELECT session_id, session_key FROM session_windows ORDER BY created_at"):
                groups.setdefault(r[1], []).append(("db", r[0]))
            known = {ref for v in groups.values() for _, ref in v}
            for r in rows(self.adb, "SELECT DISTINCT session_id FROM transcript_events"):
                if r[0] not in known:
                    groups.setdefault(f"?:{r[0]}", []).append(("db", r[0]))
        for key, paths in files.items():
            # старые файлы — раньше окон из базы: в базе после переезда OpenClaw та же история (повторы отсеются по id)
            groups[key] = [("jsonl", str(p)) for p in paths] + groups.get(key, [])
        mapping = self.app.store.meta_get("import:openclaw:sessions", {}) or {}
        main_key = f"agent:{self.agent.id}:main"
        main_sid = self.app.main_session()
        main_busy = self.app.store.message_count(main_sid, exclude_meta_key="oc_id") > 0
        total_msgs = 0
        for key, sources in groups.items():
            msgs, how = read_sources(self.adb, sources)
            if not msgs:
                continue
            node = nodes.get(key) or {}
            entry = store.get(key) or {}
            is_main = key == main_key or key in ("main", "agent:main:main") and len(groups) == 1
            via = node.get("created_via")
            skip = via in SKIP_CREATED_VIA or any(x in key for x in (":cron:", ":subagent:", ":heartbeat")) or key.startswith(("cron:", "hook:"))
            first = time.strftime("%Y-%m-%d", time.localtime(min(m["ts"] for m in msgs) or time.time()))
            name = node.get("display_name") or node.get("label") or entry.get("displayName") or entry.get("label") or entry.get("subject") \
                or (f"старый разговор от {first}" if key.startswith("?:") else key)
            seen = self._seen(mapping.get(key))
            new = [m for m in msgs if m["oc_id"] not in seen]
            if is_main:
                target = mapping.get(key) or (main_sid if not main_busy else None)
                title = "История главного разговора"
                where = "в главный разговор Бендера" if target == main_sid else "отдельным разговором (в главном у Бендера уже своя история)"
            else:
                target = mapping.get(key)
                title = f"Разговор {name}"
                where = "отдельным разговором"
            src = (" · из старых файлов JSONL" if "jsonl" in how and not ({"events", "fts"} & how) else "") + (" · текст из индекса OpenClaw (сжатые события)" if "fts" in how else "")
            detail = f"{len(msgs)} сообщ. с {first} · {where}{src}"
            if not new:
                self.add(f"session:{key}", "session", title, "skip", detail=detail, note="уже перенесено")
                continue
            note = f"новых {len(new)} из {len(msgs)}" if len(new) != len(msgs) else ""
            if "partial" in how:
                note = (note + "; " if note else "") + "часть событий сжата и не читается — перенесено не всё"
            if skip:
                note = (note + "; " if note else "") + "служебный (расписание или подагент) — по умолчанию не переношу"
            total_msgs += len(new)
            self.add(f"session:{key}", "session", title, "copy", detail=detail, note=note, selected=not skip,
                     payload={"key": key, "sources": [list(x) for x in sources], "target": target, "main": is_main, "title": f"OpenClaw: {name}"})
        self.stats["messages"] = total_msgs

    def _seen(self, bender_sid: str | None) -> set[str]:
        if not bender_sid:
            return set()
        return {r[0] for r in self.app.store.q("SELECT json_extract(meta, '$.oc_id') FROM messages WHERE session_id = ? AND json_extract(meta, '$.oc_id') IS NOT NULL", (bender_sid,))}

    # пульс, расписания, каналы, MCP, секреты

    def plan_rest(self) -> None:
        thinking=self.opt('thinkingDefault')
        if thinking is not None:
            from ..router.effort import normalize
            try:effort=normalize(thinking)
            except HumanError:
                self.warnings.append('Усилия OpenClaw не перенесены: неизвестный уровень или режим самого harness. Выбери /effort в Бендере.')
            else:
                current=self.app.settings.get('reasoning','effort')
                self.add('setting:reasoning','setting','Усилия моделей: '+effort,'set' if current=='default' else 'skip',
                         note='Умолчание выбранного агента; фактический уровень зависит от модели и API. Существующий выбор сохраняется.',
                         payload={'group':'reasoning','changes':{'effort':effort}})
        jobs = []
        for r in rows(self.sdb, "SELECT job_id, declaration_key, name, enabled, agent_id, job_json FROM cron_jobs ORDER BY sort_order"):
            try:
                job = json.loads(r["job_json"])
            except (TypeError, ValueError):
                job = {}
            jobs.append({"id": r["job_id"], "declaration": r["declaration_key"], "name": r["name"], "enabled": bool(r["enabled"]), "agent": r["agent_id"], "job": job})
        legacy = self.inst.root / "cron" / "jobs.json"
        if not jobs and legacy.is_file():
            try:
                data = json.loads(legacy.read_text(encoding="utf-8"))
                for j in data.get("jobs", []) if isinstance(data, dict) else []:
                    jobs.append({"id": j.get("id"), "declaration": j.get("declarationKey"), "name": j.get("name"), "enabled": j.get("enabled", True), "agent": j.get("agentId"), "job": j})
            except (OSError, ValueError):
                pass
        # пульс
        hb_cfg = self.opt("heartbeat") if isinstance(self.opt("heartbeat"), dict) else {}
        minutes = parse_duration_minutes(hb_cfg.get("every")) if hb_cfg.get("every") is not None else None
        if minutes is None:
            for j in jobs:
                if j["declaration"] == f"heartbeat:{self.agent.id}":
                    sch = j["job"].get("schedule") or {}
                    if sch.get("everyMs"):
                        minutes = sch["everyMs"] / 60000
        changes = {}
        if minutes is not None:
            if minutes <= 0:
                changes["enabled"] = False
            else:
                changes["interval_minutes"] = max(1, min(1440, round(minutes)))
        ah = hb_cfg.get("activeHours") if isinstance(hb_cfg.get("activeHours"), dict) else None
        if ah and ah.get("start") and ah.get("end"):
            def hhmm(x):
                return "00:00" if str(x) in ("24:00", "24") else str(x)
            changes["quiet_hours"] = f"{hhmm(ah['end'])}-{hhmm(ah['start'])}"
        heart = self.app.settings.group("heart")
        changes = {k: v for k, v in changes.items() if heart.get(k) != v}
        if changes:
            words = []
            if changes.get("enabled") is False:
                words.append("выключен (как было)")
            if "interval_minutes" in changes:
                words.append(f"каждые {changes['interval_minutes']} мин")
            if "quiet_hours" in changes:
                words.append(f"тихие часы {changes['quiet_hours']}")
            self.add("setting:heart", "setting", "Пульс: " + ", ".join(words), "set", payload={"group": "heart", "changes": changes})
        user_jobs = [j for j in jobs if not j["declaration"] and (not j["agent"] or j["agent"] == self.agent.id)]
        if user_jobs:
            names = ", ".join(f"«{j['name']}»" for j in user_jobs[:5]) + (" …" if len(user_jobs) > 5 else "")
            have = {j.get("id") for j in self.app.store.meta_get("import:openclaw:cron", []) or [] if isinstance(j, dict)}
            if all(j["id"] in have for j in user_jobs):
                self.add("archive:cron", "archive", f"Расписания: {len(user_jobs)} ({names})", "skip", note="уже перенесено")
            else:
                self.add("archive:cron", "archive", f"Расписания: {len(user_jobs)} ({names})", "archive",
                         note="станут задачами Бендера — выключенными: включишь после просмотра на экране «Сердце»", payload={"jobs": user_jobs})
        intents = [dict(r) for r in rows(self.adb, "SELECT id, description, trigger_keywords, status, expires_at FROM standing_intents WHERE status IN ('pending', 'armed')")]
        if intents:
            have = {j.get("id") for j in self.app.store.meta_get("import:openclaw:intents", []) or [] if isinstance(j, dict)}
            if all(i["id"] in have for i in intents):
                self.add("archive:intents", "archive", f"Отложенные намерения: {len(intents)}", "skip", note="уже перенесено")
            else:
                self.add("archive:intents", "archive", f"Отложенные намерения: {len(intents)}", "archive", note="станут намерениями Бендера (срабатывают на слова, только напоминают)", payload={"intents": intents})
        ch = self.cfg.get("channels") if isinstance(self.cfg.get("channels"), dict) else {}
        if ch:
            tg_ids: list[int] = []
            if isinstance(ch.get("telegram"), dict):
                from ..channels.adopt import owner_ids

                pairing = [r[0] for r in rows(self.sdb, "SELECT entry FROM channel_pairing_allow_entries WHERE channel_key = 'telegram' ORDER BY sort_order")]
                tg_ids = owner_ids(ch["telegram"], pairing)
            note = "Telegram станет каналом Бендера (выключенным: включишь в «Связи», когда остановишь OpenClaw); остальные — потом" if "telegram" in ch \
                else "подключатся позже (Связь); токены — в сейф, если разрешишь"
            if isinstance(ch.get('matrix'),dict):note+='; Matrix: предложение адреса/комнат в Связях, выключено до просмотра и новой привязки; E2EE не переносится'
            self.add("archive:channels", "archive", "Каналы: " + ", ".join(ch), "archive", note=note, payload={"channels": redact(ch), "telegram_owner_ids": tg_ids})
            for path, val in walk_secrets(ch, "channels"):
                v = resolve_secret(val, self.warnings, path)
                if v:
                    self.add(f"secret:{path}", "secret", f"Секрет {path}", "set", detail=f"→ сейф: openclaw:{path} · {mask(v)}", secret=True, payload={"name": f"openclaw:{path}", "value": v})
        mcp = self.cfg.get("mcp") if isinstance(self.cfg.get("mcp"), dict) else {}
        servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else self.cfg.get("mcpServers") or {}
        if servers:
            self.add("mcp:servers", "mcp", "MCP-серверы: " + ", ".join(servers), "create",
                     note="перенесутся выключенными; определения и права одобряются отдельно в «MCP»", payload={"servers": servers})
        for r in rows(self.sdb, "SELECT name, value, kind, allowed_hosts FROM secret_store_entries WHERE deleted_at_ms IS NULL"):
            hosts = []
            raw = r["allowed_hosts"]
            if raw:
                try:
                    hosts = [str(h) for h in json.loads(raw)]
                except (TypeError, ValueError):
                    hosts = [h.strip() for h in str(raw).split(",") if h.strip()]
            self.add(f"secret:store:{r['name']}", "secret", f"Секрет {r['name']}", "set", detail=f"→ сейф: openclaw:{r['name']} · {mask(r['value'])}" + (f" · только для {', '.join(hosts)}" if hosts else ""),
                     secret=True, payload={"name": f"openclaw:{r['name']}", "value": r["value"], "hosts": hosts})
        plugins = (self.cfg.get("plugins") or {}).get("entries") if isinstance(self.cfg.get("plugins"), dict) else None
        if isinstance(plugins, dict) and plugins:
            self.stats["plugins"] = sorted(plugins)

    def plan_ritual(self) -> None:
        moved = [i for i in self.items if i.kind in ("self", "diary", "session") and i.action != "skip"]
        if moved:
            self.add("ritual", "ritual", "Ритуал Переселения", "create", detail="→ self/RELOCATION.md",
                     note="при первом разговоре агент осмотрит новое тело, перечитает себя и поправит устаревшее")

    def build(self) -> Plan:
        if self.inst.config_error:
            self.warnings.append(f"openclaw.json не разобрался ({self.inst.config_error}) — модели и настройки не перенесутся")
        try:
            self.plan_persona()
            self.plan_files()
            self.plan_models()
            self.plan_sessions()
            self.plan_rest()
            self.plan_ritual()
        finally:
            self.copies.close()
        self.stats.update({"items": len(self.items), "agent_db": bool(self.adb), "state_db": bool(self.sdb)})
        return Plan(self.inst, self.agent, self.items, self.warnings, self.stats)


def make_plan(app, path: str | None = None, agent: str | None = None) -> Plan:
    installs = find_installs(path,app=app)
    if not installs:
        raise HumanError("no_openclaw", "OpenClaw не найден", "Обычно он живёт в ~/.openclaw. Если в другом месте — укажи папку (--from).")
    inst = installs[0]
    return Planner(inst, inst.agent(agent), app).build()


# --- замеры старого и нового тела ------------------------------------------------------------


def measure_openclaw(root: Path | None = None) -> dict:
    """Сколько весит живой OpenClaw на этой машине: память процессов, установка, состояние."""
    from ..body.sensors import rss_mb

    out: dict = {"rss_mb": None, "processes": 0, "install_mb": None, "packages": None, "state_mb": None}
    me = {os.getpid(), os.getppid()}
    total = 0.0
    proc = Path("/proc")
    if proc.is_dir():
        for d in proc.iterdir():
            if not d.name.isdigit() or int(d.name) in me:
                continue
            try:
                argv = (d / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            exe = os.path.basename(argv[0].decode("utf-8", "replace")) if argv and argv[0] else ""
            head = " ".join(a.decode("utf-8", "replace") for a in argv[:3]).lower()
            if "openclaw" not in head or not (exe.startswith("node") or exe.startswith("openclaw")):
                continue
            rss = rss_mb(int(d.name))
            if rss:
                total += rss
                out["processes"] += 1
        if out["processes"]:
            out["rss_mb"] = round(total)
    exe = shutil.which("openclaw")
    if exe:
        p = Path(os.path.realpath(exe))
        for parent in list(p.parents)[:6]:
            pj = parent / "package.json"
            try:
                if not (pj.is_file() and json.loads(pj.read_text(encoding="utf-8")).get("name") == "openclaw"):
                    continue
            except (OSError, ValueError):
                continue
            out["version"] = json.loads(pj.read_text(encoding="utf-8")).get("version")

            def count(nm: Path) -> int:
                if not nm.is_dir():
                    return 0
                return sum(1 for x in nm.iterdir() if (x / "package.json").is_file()) + sum(
                    1 for sc in nm.glob("@*") if sc.is_dir() for x in sc.iterdir() if (x / "package.json").is_file())

            nested = count(parent / "node_modules")
            if nested < 20 and parent.parent.name == "node_modules":
                # локальная установка: зависимости подняты на уровень выше, рядом с самим openclaw
                out["packages"] = count(parent.parent)
                out["install_mb"] = round(dir_size(parent.parent, 2_000_000) / 1e6)
            else:
                # глобальная установка (npm -g): зависимости внутри пакета
                out["packages"] = nested + 1
                out["install_mb"] = round(dir_size(parent, 2_000_000) / 1e6)
            break
    if root and Path(root).is_dir():
        out["state_mb"] = round(dir_size(Path(root)) / 1e6, 1)
    return out


def measure_bender() -> dict:
    from ..body.sensors import rss_mb

    code = PROGRAM_DIR / "bender"
    size = 0
    for r, dirs, files in os.walk(code):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        size += sum(os.path.getsize(os.path.join(r, f)) for f in files)
    return {"rss_mb": rss_mb(), "install_mb": round(size / 1e6, 1), "packages": 0}


def body_lines(old: dict, new: dict) -> list[str]:
    def num(v, unit):
        return f"{v} {unit}" if v is not None else "?"

    ref = REFERENCE
    if old.get("rss_mb"):
        o = f"OpenClaw{' ' + old['version'] if old.get('version') else ''} на этой машине сейчас занимает {old['rss_mb']} МБ памяти ({count(old['processes'], 'процесс', 'процесса', 'процессов')})"
    else:
        o = f"OpenClaw {ref['version']} в покое занимал ~{ref['rss_mb']} МБ памяти и запускался {ref['startup_s']} с (замер при разработке Бендера)"
    disk = old.get("install_mb") or ref["install_mb"]
    pk = old.get("packages") or ref["packages"]
    o += f"; установка — {disk} МБ на диске, {count(pk, 'пакет', 'пакета', 'пакетов')} Node.js."
    n = f"Бендер сейчас: {num(new.get('rss_mb'), 'МБ')} памяти, {new.get('install_mb')} МБ кода, зависимостей — {new.get('packages', 0)}."
    return [o, n]


RELOCATION = """# RELOCATION.md — Переселение

_Ты переехал. Раньше ты жил в OpenClaw, теперь твой дом — Бендер. Память, личность и записи перенесены; тело новое._

**Сначала дело хозяина.** Если хозяин пришёл с задачей — сделай её. Ритуал — в спокойную минуту, но не откладывай его надолго.

## Что перенесено
{moved}

## Старое и новое тело
{body}

## 1. Осмотрись
Твои файлы — выше в этом промпте. Загляни в дневник за последние дни (diary/) и осмотри машину (тул status). Ты на той же машине или на новой?

## 2. Почувствуй новое тело
Скажи хозяину пару слов о переезде — своим голосом, как в SOUL.md. Можно сравнить, насколько похудел.

## 3. Наведи порядок
Файлы писались для OpenClaw: там могут быть его команды, тулы и пути (openclaw …, HEARTBEAT_OK, sessions_spawn, workspace/…). Устаревшее поправь: память — тулом memory, себя — update_self. Если часть написана по-английски — спроси хозяина, переводить ли.
Старые правила работы (AGENTS.md OpenClaw) лежат в архиве переезда: {archive}. Если там есть что-то важное, чего нет в твоих правилах, — предложи хозяину добавить (правила меняются только с его одобрения).

## 4. Закончи
Вызови update_self с file="done" — ритуал закончится, этот файл исчезнет.
"""


# --- перенос ---------------------------------------------------------------------------------


@dataclass
class Report:
    dir: str = ""
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    def public(self) -> dict:
        old, new = self.counts.get("old_body"), self.counts.get("new_body")
        return {"dir": self.dir, "done": self.done, "skipped": self.skipped, "errors": self.errors, "warnings": self.warnings, "counts": self.counts,
                "body": body_lines(old, new) if old is not None and new is not None else []}


def _append_once(cur: str, text: str, heading: str) -> str:
    if text.strip() in cur:
        return cur
    return cur.rstrip("\n") + f"\n\n## {heading}\n\n" + text.strip() + "\n"


def add_to_section(text: str, title: str, addition: str) -> str:
    """Дописать в конец раздела «## title» (или создать раздел в конце)."""
    m = re.search(rf"^##\s+{re.escape(title)}\s*$", text, flags=re.M)
    if not m:
        return text.rstrip("\n") + f"\n\n## {title}\n\n" + addition.strip() + "\n"
    nxt = re.search(r"^##\s", text[m.end():], flags=re.M)
    cut = m.end() + nxt.start() if nxt else len(text)
    head, tail = text[:cut].rstrip("\n"), text[cut:]
    return head + "\n\n" + addition.strip() + "\n" + ("\n" + tail if tail else "")


def _copy_tree(src: Path, dst: Path) -> int:
    """Копировать без перезаписи существующего. Возвращает число скопированных файлов."""
    n = 0
    if src.is_file():
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
        return n
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        rel = Path(root).relative_to(src)
        for f in files:
            relative_path((rel / f).as_posix())
            s = Path(root) / f
            if not within(s, src):
                raise HumanError("import_path", "Файл вышел за папку переноса", "Убери ссылки и повтори предпросмотр.")
            if s.is_symlink():
                continue
            d = dst / rel / f
            if d.exists():
                continue
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            n += 1
    return n


def apply(plan: Plan, app, *, skip: set[str] | None = None, only: set[str] | None = None, secrets: bool = True, progress=None, adopt_channels=True, deduplicate=False) -> Report:
    """Перенести выбранное. skip — ID пунктов, которые хозяин снял; only — явный список (из пульта)."""
    say = progress or (lambda s: None)
    rep = Report(warnings=list(plan.warnings))
    skip = set(skip or ())

    def chosen(it: Item) -> bool:
        if it.action == "skip":
            return False
        if only is not None:
            return it.id in only
        if it.id in skip:
            return False
        if it.secret and not secrets and it.kind == "secret":
            return False
        return it.selected

    stamp = time.strftime("%Y%m%d-%H%M%S")
    adir = app.dirs.imports / f"openclaw-{stamp}"
    i = 1
    while adir.exists():
        i += 1
        adir = app.dirs.imports / f"openclaw-{stamp}-{i}"
    adir.mkdir(parents=True)
    rep.dir = str(adir)

    say("Снимок Бендера до переезда")
    before = adir / "before"
    before.mkdir()
    app.store.backup_to(before / "bender.db")
    if app.dirs.self_dir.is_dir():
        shutil.copytree(app.dirs.self_dir, before / "self")
    if app.dirs.secrets.is_file():
        shutil.copy2(app.dirs.secrets, before / "secrets.json")
        os.chmod(before / "secrets.json", 0o600)

    sd = app.selfdir
    moved: list[str] = []
    pmap: dict[str, str] = {}  # провайдер OpenClaw → провайдер Бендера
    copies = ReadCopies(app)
    adb = None

    for it in plan.items:
        if not chosen(it):
            if it.action != "skip" or it.note:
                rep.skipped.append(f"{it.title}" + (f" — {it.note}" if it.action == "skip" and it.note else ""))
            continue
        try:
            p = it.payload
            if it.kind == "self":
                say(it.title)
                if it.id == "self:identity":
                    sd.set_identity(p["fields"])
                elif it.id == "self:env":
                    sd.write("AGENTS.md", add_to_section(sd.read("AGENTS.md"), "Окружение", p["text"]))
                elif it.action == "replace":
                    sd.write(p["target"], p["text"])
                else:
                    sd.write(p["target"], _append_once(sd.read(p["target"]), p["text"], "Из OpenClaw"))
                moved.append(it.title)
            elif it.kind == "diary":
                say("Дневник")
                n = 0
                for src, dst in p["files"]:
                    from .sourceguard import check
                    check(app,src,plan.agent.workspace)
                    text = Path(src).read_text(encoding="utf-8", errors="replace")
                    d = Path(dst)
                    d.parent.mkdir(parents=True, exist_ok=True)
                    if d.exists():
                        cur = d.read_text(encoding="utf-8")
                        if text.strip() in cur:
                            continue
                        atomic_write_text(d, cur.rstrip("\n") + "\n\n<!-- из OpenClaw -->\n" + text.strip() + "\n")
                    else:
                        atomic_write_text(d, text)
                    n += 1
                rep.counts["diary"] = n
                moved.append(f"дневник — {count(n, 'файл', 'файла', 'файлов')}")
            elif it.kind == "skill":
                say(it.title)
                result = app.skills.import_path(p["src"], source="openclaw")
                rep.errors.extend(result["errors"])
                rep.counts["skills"] = rep.counts.get("skills", 0) + len(result["imported"])
            elif it.kind in ("avatar", "work"):
                say(it.title)
                n = _copy_tree(Path(p["src"]), Path(p["dst"]))
                rep.counts[it.kind] = rep.counts.get(it.kind, 0) + n
                if it.kind=='work' and app.settings.get('attachments','enabled',True):
                    from ..channels import media
                    dest=Path(p['dst'])
                    paths=[dest] if dest.is_file() else sorted(dest.rglob('*'))
                    for copied in paths[:2000]:
                        if copied.is_file() and not copied.is_symlink() and copied.stat().st_size<=media.UPLOAD_LIMIT:
                            media.register(app,copied,kind=media.outgoing_kind(copied),source='openclaw')
            elif it.kind == "provider":
                say(it.title)
                pmap[p["oc_id"]] = _apply_provider(app, p, with_keys=secrets)
            elif it.kind == "role":
                say(it.title)
                ok = _apply_role(app, p, pmap, plan, rep)
                if not ok:
                    continue
            elif it.kind == "setting":
                app.settings.set(p["group"], p["changes"], by="owner")
            elif it.kind == "mcp":
                result = app.mcp.import_servers(p, source="openclaw", secrets=secrets)
                rep.warnings.extend(result["errors"])
                rep.warnings.extend(result["warnings"])
                rep.counts["mcp"] = len(result["imported"])
            elif it.kind == "session":
                say(it.title)
                if adb is None and plan.agent.db and any(src[0] == "db" for src in p.get("sources") or [["db"]]):
                    adb = copies.open(plan.agent.db)
                n = _apply_session(app, adb, p)
                rep.counts["messages"] = rep.counts.get("messages", 0) + n
                if n:
                    moved.append(f"{it.title.lower()} — {count(n, 'сообщение', 'сообщения', 'сообщений')}")
            elif it.kind == "secret":
                save=app.secrets.reuse if deduplicate else app.secrets.set
                actual=save(p["name"], p["value"], note="из OpenClaw", allow=({"hosts": p["hosts"], "tools": []} if p.get("hosts") else None))
                if actual and actual!=p['name']:rep.warnings.append(f"Секрет {p['name']} переиспользован как {actual}; для собственных скриптов используй это имя.")
            elif it.kind == "archive":
                _apply_archive(app, adir, it)
            elif it.kind == "ritual":
                continue  # после всех
            rep.done.append(it.title + (f" ({ACTION_WORDS.get(it.action, it.action)})" if it.action in ("append", "merge") else ""))
        except HumanError as e:
            rep.errors.append(f"{it.title}: {e.title}. {e.hint}".strip())
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"{it.title}: {type(e).__name__}: {e}")

    copies.close()

    # Telegram — после секретов (токен бота кладётся отдельным пунктом)
    chi = plan.item("archive:channels")
    if adopt_channels and chi and chosen(chi) and chi.action != "skip" and getattr(app, "telegram", None) is not None:
        from ..channels.adopt import adopt as adopt_telegram

        try:
            tg = adopt_telegram(app, list(chi.payload.get("telegram_owner_ids") or []))
            if tg.get("token") or tg.get("owner"):
                rep.done.append("Telegram готов к включению в «Связи»")
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"Telegram: {e}")

    # если агент уже знает себя — пробуждение не нужно, нужен ритуал Переселения
    if sd.identity().get("name"):
        sd.finish_awakening()
    ritual = plan.item("ritual")
    old = measure_openclaw(plan.install.root)
    new = measure_bender()
    rep.counts["old_body"] = old
    rep.counts["new_body"] = new
    if ritual and chosen(ritual):
        lines = "\n".join(f"- {m}" for m in moved) or "- (ничего)"
        text = RELOCATION.format(moved=lines, body="\n".join(f"- {b}" for b in body_lines(old, new)), archive=str(adir / "originals"))
        sd.write("RELOCATION.md", text)
        rep.done.append("Ритуал Переселения")

    # архив: оригиналы файлов личности, конфиг без секретов, план, отчёт
    orig = adir / "originals"
    orig.mkdir(exist_ok=True)
    if plan.agent.workspace and plan.agent.workspace.is_dir():
        for name in PERSONA:
            src = plan.agent.workspace / name
            if src.is_file():
                shutil.copy2(src, orig / name)
    atomic_write_text(orig / "openclaw.redacted.json", json.dumps(redact(plan.install.config), ensure_ascii=False, indent=2))
    atomic_write_text(adir / "plan.json", json.dumps(plan.public(), ensure_ascii=False, indent=2))
    atomic_write_text(adir / "report.md", report_md(plan, rep))

    mapping_note = {"at": time.time(), "source": str(plan.install.root), "agent": plan.agent.id, "dir": str(adir), "done": len(rep.done), "errors": len(rep.errors)}
    app.store.meta_set("import:openclaw:last", mapping_note)
    app.journal.write("import", f"Переезд из OpenClaw: перенесено {len(rep.done)}, ошибок {len(rep.errors)}", actor="owner",
                      data={"source": str(plan.install.root), "agent": plan.agent.id, "dir": str(adir), "messages": rep.counts.get("messages", 0)})
    app.bus.publish({"type": "identity", "identity": sd.identity()})
    app.bus.publish({"type": "settings", "group": "providers"})
    if rep.counts.get("messages") or rep.counts.get("diary"):
        say("Индексирую перенесённое для поиска")
        try:
            app.index.sync(None)  # вся история сразу, а не по 2 секунды на поиск
        except Exception as e:  # noqa: BLE001 — индекс доделается сам при поиске
            rep.warnings.append(f"Индекс памяти досчитается позже: {e}")
    app.bus.publish({"type": "imported", "source": "openclaw", "messages": rep.counts.get("messages", 0)})
    return rep


def _apply_provider(app, p: dict, *, with_keys: bool) -> str:
    base = normalize_base_url(p["base_url"])
    keys = [k["value"] for k in p.get("keys") or []] if with_keys else []
    for ex in app.models.providers().values():
        if normalize_base_url(ex.get("base_url", "")) == base and ex.get("kind") == p["kind"]:
            have = {app.secrets.get(n) for n in ex.get("keys") or []}
            for k in keys:
                if k not in have:
                    app.models.update_provider(ex["id"], {"api_key": k})
            if p.get("prices"):
                app.models.update_provider(ex["id"], {"prices": {**(ex.get("prices") or {}), **p["prices"]}})
            return ex["id"]
    extra = {"imported": "openclaw", "oc_id": p["oc_id"]}
    if p.get("prices"):
        extra["prices"] = p["prices"]
    if p.get("models"):
        extra["models"] = p["models"]
    if p.get("headers"):
        extra["headers"] = {k: str(v) for k, v in p["headers"].items()}
    new = app.models.add_provider(name=p["name"], kind=p["kind"], base_url=base, preset=p.get("preset"), api_key=keys[0] if keys else None, extra=extra)
    for k in keys[1:]:
        app.models.update_provider(new["id"], {"api_key": k})
    return new["id"]


def _apply_role(app, p: dict, pmap: dict, plan: Plan, rep: Report) -> bool:
    entries = []
    provs = app.models.providers()
    for oc_pid, model in p["entries"]:
        pid = pmap.get(oc_pid)
        if not pid:
            # провайдер не переносили в этот раз — ищем такой же у Бендера по адресу
            pv = plan.item(f"provider:{oc_pid}")
            if pv:
                base = normalize_base_url(pv.payload["base_url"])
                pid = next((x["id"] for x in provs.values() if normalize_base_url(x.get("base_url", "")) == base), None)
        if pid:
            params=(p.get('effort_params') or {}).get(oc_pid+'/'+model)
            entries.append({"provider": pid, "model": model,**({'params':params} if params else {})})
        else:
            rep.warnings.append(f"Модель {oc_pid}/{model}: провайдер не перенесён — пропущена")
    if not entries:
        return False
    cur = app.models.roles().get(p["role"]) or []
    if cur:
        have = {(e["provider"], e["model"]) for e in cur}
        entries = cur + [e for e in entries if (e["provider"], e["model"]) not in have]
    app.models.set_role(p["role"], entries)
    return True


def _apply_session(app, adb: sqlite3.Connection | None, p: dict) -> int:
    sources = p.get("sources") or [["db", sid] for sid in p.get("sids") or []]
    msgs, _ = read_sources(adb, sources)
    mapping = app.store.meta_get("import:openclaw:sessions", {}) or {}
    target = mapping.get(p["key"]) or p.get("target")
    if not target or not app.store.session(target):
        target = app.store.new_session("imported", p["title"], meta={"imported": "openclaw", "oc_key": p["key"]})
    seen = {r[0] for r in app.store.q("SELECT json_extract(meta, '$.oc_id') FROM messages WHERE session_id = ? AND json_extract(meta, '$.oc_id') IS NOT NULL", (target,))}
    out = []
    last_ms = 0
    for m in msgs:
        if m["oc_id"] and m["oc_id"] in seen:
            continue
        ms = int((m["ts"] or time.time()) * 1000)
        ms = max(ms, last_ms + 1)
        last_ms = ms
        out.append((id_at(ms), {"role": m["role"], "content": m["content"]}, ms / 1000, {"imported": "openclaw", "oc_id": m["oc_id"], "oc_session": m["oc_session"]}))
    n = app.store.import_messages(target, out)
    mapping[p["key"]] = target
    app.store.meta_set("import:openclaw:sessions", mapping)
    return n


def _apply_archive(app, adir: Path, it: Item) -> None:
    p = it.payload
    if "text" in p:
        d = adir / "originals"
        d.mkdir(exist_ok=True)
        atomic_write_text(d / p["name"], p["text"])
        return
    name = it.id.split(":", 1)[1]
    atomic_write_text(adir / f"{name}.json", json.dumps(p, ensure_ascii=False, indent=2))
    # то, что заберут будущие вехи: расписания и намерения — в meta (выключенными)
    if name in ("cron", "intents", "channels", "mcp"):
        cur = app.store.meta_get(f"import:openclaw:{name}", []) or []
        items = p.get("jobs") or p.get("intents") or [p.get("channels") or p.get("servers")]
        app.store.meta_set(f"import:openclaw:{name}", items if name in ("channels", "mcp") else _merge_by_id(cur, items))
        if name in ("cron", "intents") and getattr(app, "heart", None) is not None:
            from ..heart.adopt import adopt

            adopt(app)  # расписания — задачами (выключенными), намерения — намерениями


def _merge_by_id(cur: list, new: list) -> list:
    have = {x.get("id") for x in cur if isinstance(x, dict)}
    return cur + [x for x in new if isinstance(x, dict) and x.get("id") not in have]


def report_md(plan: Plan, rep: Report) -> str:
    L = [f"# Переезд из OpenClaw — {time.strftime('%Y-%m-%d %H:%M')}", "",
         f"Откуда: `{plan.install.root}` (OpenClaw {plan.install.version or '?'}), агент `{plan.agent.id}` ({plan.agent.name}).", "",
         "Снимок Бендера до переезда — в папке `before/` рядом с этим файлом: `bender.db`, `self/`, `secrets.json`.",
         "Оригиналы файлов личности и конфиг без секретов — в `originals/`.", ""]
    if rep.done:
        L += ["## Перенесено", ""] + [f"- {x}" for x in rep.done] + [""]
    if rep.skipped:
        L += ["## Не переносилось", ""] + [f"- {x}" for x in rep.skipped] + [""]
    if rep.warnings:
        L += ["## Предупреждения", ""] + [f"- {x}" for x in rep.warnings] + [""]
    if rep.errors:
        L += ["## Ошибки", ""] + [f"- {x}" for x in rep.errors] + [""]
    old, new = rep.counts.get("old_body") or {}, rep.counts.get("new_body") or {}
    if old or new:
        L += ["## Тело", ""] + [f"- {b}" for b in body_lines(old, new)] + [""]
    return "\n".join(L)
