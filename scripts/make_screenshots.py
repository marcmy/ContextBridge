"""Regenerate docs/screenshots from synthetic sessions. Real titles never reach the repo."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from helpers import CLAUDE, Sandbox  # noqa: E402
from contextbridge.handoff import HandoffOptions  # noqa: E402
from PySide6.QtCore import QCoreApplication, Qt, QThreadPool  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from contextbridge.models import Provider  # noqa: E402
from contextbridge.ui.handoff_dialog import HandoffDialog  # noqa: E402
from contextbridge.ui.main_window import MainWindow  # noqa: E402
from contextbridge.ui.theme import apply_theme  # noqa: E402

OUT = ROOT / "docs" / "screenshots"


def pump(until, timeout=60):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if until():
            QTest.qWait(150)    # let layouts settle before a capture
            return
        QTest.qWait(20)
    raise TimeoutError


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    box = Sandbox()
    app = box.app()
    qt = QApplication(sys.argv[:1])
    apply_theme(qt)

    origin = box.codex_rollout()
    for i, text in enumerate(("Add an offline render queue", "Stream frames to the encoder",
                              "Fix the stutter on 60 fps sources", "Write the benchmark harness",
                              "Tune the worker pool")):
        box.codex_turn(origin, text, f"Implemented step {i}. " + "Details. " * 300, tool=True)
    app.refresh()
    claude_id = app.execute(app.plan(app.session("codex", origin), CLAUDE,
                                     HandoffOptions(context_budget=None))).dest_id
    box.claude_turn(claude_id, "Profile the encoder path", "Profiled; two hot spots remain.")
    box.claude_turn(claude_id, "Remove the first hot spot", "Removed, with a regression test.")
    app.refresh()
    app.execute(app.plan(app.session("claude", claude_id), Provider.CODEX,
                         HandoffOptions(context_budget=None)))
    separate = box.codex_rollout()
    box.codex_turn(separate, "Add a settings dialog", "Added.")
    native = box.claude_session(("Write the installer script", "Written."),
                                ("Sign the installer", "Signed."))
    both = box.codex_rollout()
    box.codex_turn(both, "Migrate the storage layer", "Migrated.")
    app.refresh()
    other = app.execute(app.plan(app.session("codex", both), CLAUDE,
                                 HandoffOptions(context_budget=None))).dest_id
    box.codex_turn(both, "Handle the old schema too", "Handled.")
    box.claude_turn(other, "Add a rollback path", "Added.")
    _ = native

    win = MainWindow(app)
    win.show()
    pump(lambda: not win._refreshing and win.snapshot.sessions)
    win.tree.select(("claude", claude_id))
    pump(lambda: win.details.session is not None)
    win.details.tabs.setCurrentIndex(1)
    QTest.qWait(200)
    win.grab().save(str(OUT / "sessions.png"))

    dialog = HandoffDialog(app, app.session("codex", origin), Provider.CLAUDE, win)
    box.codex_turn(origin, "Add frame pacing", "Added frame pacing.", tool=True)
    app.refresh()
    dialog.close()
    dialog = HandoffDialog(app, app.session("codex", both), Provider.CLAUDE, win)
    dialog.show()
    pump(lambda: dialog.plan is not None and not dialog._busy)
    dialog.grab().save(str(OUT / "review-diverged.png"))
    dialog.close()
    dialog = HandoffDialog(app, app.session("codex", origin), Provider.CLAUDE, win)
    dialog.show()
    pump(lambda: dialog.plan is not None and not dialog._busy)
    QTest.mouseClick(dialog.next_button, Qt.LeftButton)
    pump(lambda: dialog.pages.currentIndex() == 1)
    dialog.grab().save(str(OUT / "context.png"))
    dialog.close()
    QThreadPool.globalInstance().waitForDone(5000)
    app.store.close()
    win.close()
    box.close()


if __name__ == "__main__":
    main()
