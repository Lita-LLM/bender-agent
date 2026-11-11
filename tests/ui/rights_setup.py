"""Та же модель без токенов, что в проверке стаи."""
import runpy
from pathlib import Path
runpy.run_path(str(Path(__file__).with_name('agents_setup.py')))
