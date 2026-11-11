"""Настройки агента поверх общих: отсутствие ключа означает «как у всех»."""
from ..core.errors import HumanError
from ..core.settings import Settings, NeedsOwner

LOCAL = {'reasoning','api_rights','userbot','attachments','agent_rights','agent','roles','guard','guard_rules','tools','heart','sleep','memory','appearance','voice','budget','team','team_roles','skills','skill_trust','search','inspectors','manner'}


class AgentSettings(Settings):
    def __init__(self, common, store, on_change):
        self.common = common
        super().__init__(store, on_change=on_change)

    def group(self, group):
        if group in ('agent_rights','userbot'):return super().group(group)  # своё у каждого агента, без наследования от главного
        if group == 'telegram':
            return {**self.common.group(group), **self._stored(group)}
        if group not in LOCAL: return self.common.group(group)
        return {**self.common.group(group), **self._stored(group)}

    def set(self, group, changes, *, by='owner'):
        if group == 'owner' and by != 'owner': raise HumanError('owner_only', 'Карточку «О хозяине» пишет только хозяин', 'Это его слова о себе: агент их не меняет и не предлагает правку. Попроси хозяина написать самому.')
        if group not in LOCAL and not (group == 'telegram' and set(changes) <= {'live'}):
            if by != 'owner': raise NeedsOwner(group, changes, ['Общими подключениями и доверием управляет хозяин'])
            return self.common.set(group, changes, by=by)
        return super().set(group, changes, by=by)

    def replace(self, group, value):
        if group not in LOCAL: return self.common.replace(group, value)
        return super().replace(group, value)

    def inherit(self, group, keys):
        with self.transaction():
            stored = dict(self._stored(group))
            for key in keys: stored.pop(key, None)
            self._write(group, stored)
            self._notify(group,{})
