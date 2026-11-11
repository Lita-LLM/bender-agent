"""Проснувшийся агент на фальшивой модели, настоящий Сторож включает run.sh."""
import runpy
from pathlib import Path
runpy.run_path(str(Path(__file__).with_name('agents_setup.py')))
