"""Double-click launcher for a checkout: opens ContextBridge without a console window.

Uses the repository's .venv when it exists, so PySide6 only has to be installed there.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv" / "Scripts" / "pythonw.exe"

if VENV.is_file() and Path(sys.executable).resolve() != VENV.resolve():
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    subprocess.Popen([str(VENV), "-m", "contextbridge"], env=env, cwd=str(ROOT))
    sys.exit(0)

sys.path.insert(0, str(ROOT / "src"))
from contextbridge.__main__ import main  # noqa: E402

sys.exit(main())
