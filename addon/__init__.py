"""Anki add-on entry point: adds Tools → Voice Review.

Imports are kept lazy so that a broken dependency shows up as a dialog when you open the
window, not as an add-on that refuses to load and takes its menu item with it.
"""

from __future__ import annotations

from aqt import gui_hooks, mw
from aqt.qt import QAction, QKeySequence, qconnect
from aqt.utils import showWarning

_dialog = None


def open_voice_review() -> None:
    global _dialog
    try:
        from .ui import VoiceReviewDialog
    except Exception as exc:  # noqa: BLE001 - report rather than silently dead-end the menu
        showWarning(f"Voice Review could not start:\n\n{exc}")
        return

    if _dialog is None:
        _dialog = VoiceReviewDialog(mw)
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()
    _dialog._refresh_ready_state()


def _migrate_config() -> None:
    """Bring a config saved against an older version of this add-on up to date.

    Without this, settings you never chose — the previous version's defaults — keep overriding
    the current ones, and a renamed setting leaves the feature it controls apparently broken.
    """
    try:
        from .avr.config import migrate_config

        package = __name__.split(".")[0]
        stored = mw.addonManager.getConfig(package) or {}
        migrated, changes = migrate_config(stored)
        if changes:
            mw.addonManager.writeConfig(package, migrated)
            print(f"[Voice Review] migrated settings: {'; '.join(changes)}")
    except Exception as exc:  # noqa: BLE001 - never block the add-on from loading
        print(f"[Voice Review] settings migration skipped: {exc}")


def _install_menu() -> None:
    _migrate_config()

    action = QAction("Voice Review", mw)
    action.setShortcut(QKeySequence("Ctrl+Shift+V"))
    qconnect(action.triggered, open_voice_review)
    mw.form.menuTools.addAction(action)


if mw is not None:
    gui_hooks.main_window_did_init.append(_install_menu)
