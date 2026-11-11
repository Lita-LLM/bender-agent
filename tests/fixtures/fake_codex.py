"""Codex для проверки недоступного движка: ни одна генерация не должна вызываться."""
import sys

if "--help" in sys.argv:
    print("exec --json --config --disable --resume --ignore-user-config --ignore-rules")
elif "--version" in sys.argv:
    print("codex-cli 0.163.0")
else:
    raise RuntimeError("Codex не должен запускать модель без проверенной границы инструментов")
