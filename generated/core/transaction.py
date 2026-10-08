"""Reversible staging for multi-resource Blender mutations."""

from __future__ import annotations

from typing import Callable, List


class GeneratedTransaction:
    """Collect rollback callbacks until the whole change is committed."""

    def __init__(self, setup_id: str):
        self.setup_id = str(setup_id)
        self._rollback: List[Callable[[], None]] = []
        self.committed = False

    def on_rollback(self, callback: Callable[[], None]):
        if self.committed:
            raise RuntimeError("Cannot add rollback work after commit")
        if not callable(callback):
            raise TypeError("Rollback callback must be callable")
        self._rollback.append(callback)
        return callback

    def commit(self):
        self.committed = True
        self._rollback.clear()

    def rollback(self):
        errors = []
        while self._rollback:
            callback = self._rollback.pop()
            try:
                callback()
            except Exception as exc:  # Preserve the original builder failure.
                errors.append(exc)
        if errors:
            detail = "; ".join(str(item) for item in errors)
            raise RuntimeError("Generated rollback failed: %s" % detail) from errors[0]
        return ()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None or not self.committed:
            self.rollback()
        return False
