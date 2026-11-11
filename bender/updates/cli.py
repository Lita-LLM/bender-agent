"""Обновление из терминала тем же кодом, что кнопка пульта."""
import asyncio
import sys
from ..core.errors import HumanError


def main(args):
    from ..core.app import App
    from ..core.paths import default_data_dir
    async def run():
        app = App(args.data or default_data_dir())
        try:
            if getattr(args, 'realign', False):
                result = await app.updater.realign(skip_backup=bool(getattr(args, 'no_backup', False)))
                print(result['message'])
                if result['realigned']: print(await app.updater.restart())
                return 0
            plan = await app.updater.check()
            if not plan['available']:
                print('Установлена последняя версия.' + (' Есть свои коммиты поверх неё.' if plan.get('ahead') else ''))
                return 0
            print('Новые коммиты:\n' + '\n'.join(plan['commits']))
            if args.check: return 0
            if not args.yes:
                if not sys.stdin.isatty():
                    print('Просмотри список и повтори bender update --yes для установки.')
                    return 0
                if input('Обновить? (да/нет) ').strip().lower() not in ('да', 'yes', 'y'): return 0
            result = await app.updater.install(plan, skip_backup=bool(getattr(args, 'no_backup', False)))
            print(result['message'])
            if result.get('backup', {}).get('path'): print('Бекап: ' + result['backup']['path'])
            elif result.get('backup', {}).get('skipped'): print('Без бекапа (по твоему слову).')
            if result['updated']: print(await app.updater.restart())
            return 0
        finally: await app.close()
    try: return asyncio.run(run())
    except HumanError as exc:
        print(exc.title + '. ' + exc.hint, file=sys.stderr)
        return 1
