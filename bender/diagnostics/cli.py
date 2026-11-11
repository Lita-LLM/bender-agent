"""doctor/report без запуска фоновой жизни; отчёт сохраняется после предпросмотра."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import sys
import time
from ..core.errors import HumanError
from ..core.paths import default_data_dir
from ..core.termui import Term
from .doctor import collect,probe_models
from .report import build,save


def main(args):
    term=Term(animations=False);root=Path(args.data or default_data_dir())
    try:
        if args.command=='doctor':
            result=collect(root)
            if args.models:
                from ..core.app import App
                async def probes():
                    app=App(root)
                    try:return await probe_models(app)
                    finally:await app.close()
                result['rows']+=asyncio.run(probes())
                result['ok']=not any(row['level']=='error' for row in result['rows'])
            if args.json:print(json.dumps(result,ensure_ascii=False,indent=2))
            else:
                for row in result['rows']:
                    mark={'ok':'✅','warn':'⚠️','error':'❌'}[row['level']]
                    term.line(mark+' '+row['title']+(' — '+row['action'] if row['action'] else ''))
                    if row.get('details'):term.line('  '+json.dumps(row['details'],ensure_ascii=False))
            return 0 if result['ok'] else 1
        raw,plan=build(root,limit=args.limit)
        term.line('В архив войдёт:')
        for item in plan['files']:term.line('  '+item['name']+' — '+item['description'])
        term.line('Исключено: '+plan['excluded'])
        path=Path(args.output or ('bender-report-'+time.strftime('%Y%m%d-%H%M%S')+'.zip'))
        term.line('Папка назначения: '+str(path.resolve()))
        if not args.yes:
            if not sys.stdin.isatty():
                term.line('Архив ещё не сохранён. Просмотри состав и повтори с --yes для сохранения.')
                return 0
            if input('Сохранить этот состав? [да/нет] ').strip().lower() not in ('да','yes','y'):
                return 0
        saved=save(root,path,raw)
        term.line('Сохранён отчёт: '+str(saved))
        return 0
    except HumanError as exc:term.error(exc);return 1
