"""Источник переезда не открывает закрытые папки через конфиг или ссылку."""
from pathlib import Path
from ..core.errors import HumanError
from ..core.pathutil import canonical,within,same
from ..guard.control import sealed


def check(app, path, root=None):
    ctx=getattr(app,'_import_source_guard',app)
    path=Path(path)
    if path.is_symlink() or root is not None and not within(path,root):
        raise HumanError('import_source','Ссылка или выход из папки в источнике переезда','Переноси обычные файлы внутри выбранной папки.')
    resolved=canonical(path)
    if same(resolved,ctx.host.dirs.root) or sealed(resolved,'fs.read',ctx.host.dirs.root) or ctx.guard.policy.protected(resolved) or ctx.host.agents.private_path(ctx,resolved,'fs.read'):
        raise HumanError('import_source','Источник ссылается на защищённые данные Бендера','Для переноса другого агента используй его экспорт в пульте.')
    return resolved
