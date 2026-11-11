"""Где лежит программа и данные."""

from __future__ import annotations

import os
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    env = os.environ.get("BENDER_HOME")
    if env:
        return Path(env).expanduser()
    return PROGRAM_DIR / "data"


class DataDir:
    """Раскладка папки данных."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.db = self.root / "bender.db"
        self.secrets = self.root / "secrets.json"
        self.self_dir = self.root / "self"
        self.diary = self.root / "diary"
        self.skills = self.root / "skills"
        self.work = self.root / "work"
        self.workshop = self.root / "workshop"
        self.checkpoints = self.root / "checkpoints"
        self.local = self.root / "local"
        self.imports = self.root / "imports"

    def ensure(self) -> None:
        for d in (self.root, self.self_dir, self.diary, self.skills, self.work, self.checkpoints, self.local):
            d.mkdir(parents=True, exist_ok=True)


class AgentDataDir(DataDir):
    """Общие база/сейф, отдельные все личные пути; main не переносится."""
    def __init__(self, shared, agent_id):
        from ..agents.store import valid_id
        from .pathutil import within, canonical
        from .errors import HumanError
        super().__init__(shared.root)
        base = shared.root / 'agents'
        private = base / valid_id(agent_id)
        if base.is_symlink() or private.is_symlink() or not within(canonical(base), canonical(shared.root)) or not within(canonical(private), canonical(base)):
            raise HumanError('agent_path', 'Папка агента выходит из данных стаи', 'Убери подставленную ссылку и повтори.')
        self.agent_root = private
        for attr, name in (('self_dir','self'),('diary','diary'),('skills','skills'),('work','work'),('workshop','workshop'),('checkpoints','checkpoints'),('local','local'),('imports','imports')):
            setattr(self, attr, private/name)
