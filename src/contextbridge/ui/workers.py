"""Background work. Discovery, conversion and summaries never run on the UI thread."""
from __future__ import annotations

import logging
import traceback
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)
_alive: set = set()


class _Signals(QObject):
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str, str)


class _Task(QRunnable):
    def __init__(self, fn: Callable, takes_progress: bool):
        super().__init__()
        self.fn = fn
        self.takes_progress = takes_progress
        self.signals = _Signals()
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = self.fn(self.signals.progress.emit) if self.takes_progress else self.fn()
        except Exception as err:  # Reported to the person; nothing else can handle it here.
            log.exception("background task failed")
            self.signals.failed.emit(str(err), traceback.format_exc())
        else:
            self.signals.done.emit(result)
        finally:
            _alive.discard(self)


def run_task(fn: Callable, *, on_done: Callable | None = None, on_failed: Callable | None = None,
             on_progress: Callable | None = None, owner: QObject | None = None) -> None:
    """Run `fn` on the pool. With on_progress, `fn` receives a progress callback.

    Signal connections deliver results on the UI thread. When `owner` is given, results
    are dropped if it has gone away (for example a closed dialog).
    """
    task = _Task(fn, on_progress is not None)
    _alive.add(task)

    def guarded(callback):
        if callback is None:
            return None
        if owner is None:
            return callback

        def inner(*args):
            try:
                owner.objectName()      # raises once the C++ object is deleted
            except RuntimeError:
                return
            callback(*args)
        return inner

    if on_progress:
        task.signals.progress.connect(guarded(on_progress))
    if on_done:
        task.signals.done.connect(guarded(on_done))
    if on_failed:
        task.signals.failed.connect(guarded(on_failed))
    QThreadPool.globalInstance().start(task)
