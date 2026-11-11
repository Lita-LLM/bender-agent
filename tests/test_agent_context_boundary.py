"""Прикладной код получает контекст, не создаёт корневое приложение/главного агента."""
import ast
from pathlib import Path
from tests.helpers import TempDirCase


class ContextBoundaryTest(TempDirCase):
    def test_workers_have_no_implicit_root_app_access(self):
        root=Path(__file__).parents[1]/'bender'
        for directory in ('mind','tools','heart','sleep','memory','voice','skills','inspectors','team','mcp','workshop','modules'):
            for path in (root/directory).rglob('*.py'):
                if path.name in ('registry.py','exam_worker.py'):continue  # ToolContext.app — прежнее публичное имя поля, внутри AgentContext
                tree=ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node,ast.ImportFrom):
                        self.assertFalse(any(x.name=='App' for x in node.names),str(path))
                    if isinstance(node,ast.Attribute) and isinstance(node.value,ast.Name) and node.value.id=='app':
                        self.assertNotIn(node.attr,('dirs','main_session','selfdir','store'),str(path))
