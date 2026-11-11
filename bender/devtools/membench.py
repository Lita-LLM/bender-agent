"""Замер памяти агента на большом объёме: `bender try membench [сообщений] [--vectors]`.

Временная папка, синтетический год жизни: дневник по дням и разговоры. Меряет время первой
индексации, размер базы, скорость поиска словами (и по смыслу — со случайными векторами 768),
прирост памяти процесса. Ничего не трогает в настоящих данных.
"""

from __future__ import annotations

import random
import shutil
import tempfile
import time
from pathlib import Path

WORDS = ("проект бендер агент память дневник хозяин казань чай кофе релиз комета сервер телефон термукс питон "
         "сон пульс таймер задача модель ключ провайдер сеть файл папка код тест ошибка исправил запустил купил "
         "встреча врач школа работа отпуск море поезд билет погода дождь снег кот собака книга фильм музыка").split()
QUERIES = ("релиз кометы", "купил билет на поезд", "что с сервером", "кот и собака", "ошибка в коде питон",
           "встреча с врачом", "погода дождь", "отпуск на море", "ключ провайдера", "сон агента")


def _rss_mb() -> float | None:
    try:
        for ln in Path("/proc/self/status").read_text().splitlines():
            if ln.startswith("VmRSS:"):
                return int(ln.split()[1]) / 1024
    except OSError:
        return None
    return None


SYL = "ба ва га да жа за ка ла ма на па ра са та фа ха ца ча ша бо во го до ко ло мо но по ро со то бе ве ге де ке ле ме не пе ре се те ни ми ли ки ти ру ну му ку ту ры ны мы".split()
ENDS = ("", "а", "ы", "е", "у", "ом", "ой", "ами", "ах", "ть", "л", "ла", "ли", "ет", "ют")


def _vocab(rnd: random.Random, n: int = 6000) -> tuple[list[str], list[float]]:
    """Словарь как в живой речи: несколько тысяч слов с частотами по Ципфу (частое — служебное и бытовое,
    редкое — имена, места, предметы). Слова темы (WORDS) — в середине частотного списка."""
    words = list(dict.fromkeys("".join(rnd.choice(SYL) for _ in range(rnd.randint(2, 4))) + rnd.choice(ENDS) for _ in range(n * 2)))[:n]
    stop = "и в не на я что с как а то он она так но да ты к у же вы за по от из для мы о это".split()
    for i, w in enumerate(WORDS):
        words.insert(200 + i * 40, w)
    words = stop + words
    weights = [1.0 / (r + 1) ** 1.05 for r in range(len(words))]
    return words, weights


_VOCAB: tuple[list[str], list[float]] | None = None


def _sentence(rnd: random.Random, n: int) -> str:
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = _vocab(random.Random(1))
    return " ".join(rnd.choices(_VOCAB[0], weights=_VOCAB[1], k=n)).capitalize() + "."


def generate(root: Path, messages: int, days: int = 365, seed: int = 7) -> None:
    """Синтетический год жизни в папке данных: дневник по дням (5 записей) и один длинный разговор."""
    import asyncio

    from ..core.app import App
    from ..core.ids import id_at

    rnd = random.Random(seed)
    app = App(root)
    try:
        start = time.time() - days * 86400
        for d in range(days):
            ts = start + d * 86400
            p = app.dirs.diary / (time.strftime("%Y-%m-%d", time.localtime(ts)) + ".md")
            p.write_text(f"# {p.stem}\n\n" + "".join(f"- {9 + i}:00 {_sentence(rnd, 12)}\n" for i in range(5)), encoding="utf-8")
        sid = app.main_session()
        step = days * 86400 / max(1, messages)
        for part in range(0, messages, 5000):
            rows = []
            for i in range(part, min(messages, part + 5000)):
                ts = start + i * step
                role = "user" if i % 2 == 0 else "assistant"
                rows.append((id_at(int(ts * 1000)), {"role": role, "content": _sentence(rnd, 18 if role == "user" else 40)}, ts, {}))
            app.store.import_messages(sid, rows)
    finally:
        asyncio.run(app.close())


def child(root: Path, vectors: bool) -> dict:
    """Чистый процесс: открыть папку, проиндексировать, искать. Память процесса — честно, без генератора."""
    import asyncio
    import gc

    from ..core.app import App
    from ..memory import vectors as vmod

    out: dict = {}
    rss_start = _rss_mb()
    app = App(root)
    try:
        out["rss_open_mb"] = round(_rss_mb() - rss_start, 1) if rss_start else None
        db = app.store.path
        out["db_mb_messages_only"] = round(db.stat().st_size / 1e6, 1)
        t0 = time.monotonic()
        out["chunks_added"] = app.index.sync(None)
        out["index_seconds"] = round(time.monotonic() - t0, 2)
        out["chunks"] = app.index.stats()["chunks"]
        gc.collect()
        out["rss_after_index_mb"] = round(_rss_mb() or 0, 1)
        t0 = time.monotonic()
        out["resync_added"] = app.index.sync()
        out["resync_ms"] = round((time.monotonic() - t0) * 1000, 1)
        lat = []
        for q in QUERIES * 2:
            t0 = time.monotonic()
            app.index.search(q, limit=8, sync=False)
            lat.append((time.monotonic() - t0) * 1000)
        lat.sort()
        out["search_ms_median"] = round(lat[len(lat) // 2], 1)
        out["search_ms_max"] = round(lat[-1], 1)
        if vectors:
            rnd = random.Random(3)
            dim = 768
            ids = [r[0] for r in app.store.q("SELECT id FROM mem_chunks")]
            for k in range(0, len(ids), 2000):
                with app.store.tx() as conn:
                    for cid in ids[k:k + 2000]:
                        emb, sig = vmod.pack([rnd.gauss(0, 1) for _ in range(dim)])
                        conn.execute("UPDATE mem_chunks SET emb = ?, sig = ? WHERE id = ?", (emb, sig, cid))
            del ids
            gc.collect()
            emb = vmod.Embedder(app)
            lat = []
            for _ in range(10):
                q = [rnd.gauss(0, 1) for _ in range(dim)]
                t0 = time.monotonic()
                emb.nearest(q, 40)
                lat.append((time.monotonic() - t0) * 1000)
            lat.sort()
            out["vector_ms_median"] = round(lat[len(lat) // 2], 1)
        gc.collect()
        out["rss_end_mb"] = round(_rss_mb() or 0, 1)
        out["rss_peak_mb"] = _hwm_mb()
        app.store.x("PRAGMA wal_checkpoint(TRUNCATE)")
        out["db_mb"] = round(db.stat().st_size / 1e6, 1)
    finally:
        asyncio.run(app.close())
    return out


def _hwm_mb() -> float | None:
    try:
        for ln in Path("/proc/self/status").read_text().splitlines():
            if ln.startswith("VmHWM:"):
                return round(int(ln.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def run(messages: int = 20000, *, vectors: bool = False, days: int = 365) -> dict:
    import json
    import subprocess
    import sys

    from ..core.paths import PROGRAM_DIR

    tmp = Path(tempfile.mkdtemp(prefix="bender-membench-"))
    try:
        generate(tmp / "data", messages, days)
        cmd = [sys.executable, "-c", "import json,sys; from pathlib import Path; from bender.devtools.membench import child; "
               "print(json.dumps(child(Path(sys.argv[1]), sys.argv[2] == '1')))", str(tmp / "data"), "1" if vectors else "0"]
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROGRAM_DIR), timeout=3600)
        if res.returncode != 0:
            raise RuntimeError(res.stderr[-2000:])
        out = json.loads(res.stdout.strip().splitlines()[-1])
        out.update({"messages": messages, "days": days})
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(rest: list[str], term) -> int:
    n = int(next((r for r in rest if r.isdigit()), "20000"))
    as_json = "--json" in rest
    with term.step(f"Строю синтетический год: {n} сообщений, 365 дней дневника; меряю в отдельном процессе"):
        r = run(n, vectors="--vectors" in rest)
    if as_json:
        import json

        term.line(json.dumps(r, ensure_ascii=False))
        return 0
    term.line(f"  кусков в индексе: {r['chunks']} · первая индексация: {r['index_seconds']} с · повторная (без изменений): {r['resync_ms']} мс")
    term.line(f"  поиск словами: медиана {r['search_ms_median']} мс, худший {r['search_ms_max']} мс")
    if "vector_ms_median" in r:
        term.line(f"  поиск по смыслу (768 измерений): медиана {r['vector_ms_median']} мс")
    term.line(f"  база: {r['db_mb']} МБ (одни сообщения — {r['db_mb_messages_only']} МБ)")
    term.line(f"  память процесса: после индексации {r['rss_after_index_mb']} МБ, в конце {r['rss_end_mb']} МБ, пик {r['rss_peak_mb']} МБ")
    return 0
