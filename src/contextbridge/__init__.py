"""ContextBridge — one workflow, any AI agent.

A desktop application and backend for moving ongoing development conversations
between OpenAI Codex and Claude Code without losing context or duplicating history.
"""
from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.1.0"
APP_NAME = "ContextBridge"
TAGLINE = "One workflow. Any AI agent."

# The converter modules predate this package and stay top-level in src/ so that
# `python src/codex_import.py` keeps working.
_SRC = str(Path(__file__).resolve().parent.parent)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
