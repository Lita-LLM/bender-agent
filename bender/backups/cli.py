"""Команды хозяина для бекапов; восстановление сначала показывает изменения."""
import asyncio
import json
from ..core.app import App
from ..core.paths import default_data_dir
from ..core.errors import HumanError


def main(args):
    async def run():
        app = App(args.data or default_data_dir())
        try:
            manager = app.backups
            if args.action == 'now':
                result = await manager.now()
            elif args.action == 'list':
                result = manager.status()
            elif args.action == 'verify':
                result = await manager.verify(full=args.full)
            elif args.action == 'repair':
                result = await manager.repair()
            else:
                if not args.target or not args.snapshot:
                    raise HumanError('backup_restore', 'Нужны снимок и --target', 'Сначала backup restore НОМЕР --target НОВАЯ_ПАПКА; для применения добавь --apply.')
                result = await manager.restore(args.snapshot, args.target) if args.apply else manager.preview(args.snapshot, args.target)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        finally:
            await app.close()
    try:
        return asyncio.run(run())
    except HumanError as exc:
        print(str(exc))
        return 1
