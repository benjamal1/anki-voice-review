"""Drive the real settings dialog, with real Qt widgets, headless.

These exist because "manual mode does nothing" turned out to be manual mode never being
switched on — the stored config said `grading_mode: auto` after the user had chosen Manual.
That is a widget-binding question, and it was previously untestable only because I assumed the
dialog needed Anki. It does not: `aqt.qt` is a thin re-export of PyQt6, so stubbing `aqt` and
`aqt.utils` is enough to instantiate the genuine dialog and click its genuine buttons.
"""

from __future__ import annotations

import os
import pathlib
import sys
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication, QDialogButtonBox  # noqa: E402


def _install_aqt_stub() -> None:
    """Make `aqt.qt` the real PyQt6, and `aqt.mw` a stand-in for the add-on manager."""
    if "aqt" in sys.modules:
        return

    from PyQt6 import QtCore, QtGui, QtWidgets

    aqt = types.ModuleType("aqt")
    qt = types.ModuleType("aqt.qt")
    for module in (QtWidgets, QtCore, QtGui):
        for name in dir(module):
            if not name.startswith("_"):
                setattr(qt, name, getattr(module, name))
    qt.qconnect = lambda signal, fn: signal.connect(fn)

    class _AddonManager:
        def __init__(self) -> None:
            self.saved: dict = {}

        def getConfig(self, package):  # noqa: N802 - Anki's spelling
            return dict(self.saved)

        def writeConfig(self, package, data):  # noqa: N802
            self.saved = dict(data)

    class _MainWindow:
        def __init__(self) -> None:
            self.addonManager = _AddonManager()
            self.state = "review"
            self.reviewer = None

        @property
        def taskman(self):
            raise AssertionError("the dialog must not touch Anki's task manager")

    aqt.mw = _MainWindow()
    aqt.qt = qt

    hooks = types.ModuleType("aqt.gui_hooks")

    class _Hook(list):
        def append(self, fn):
            super().append(fn)

        def remove(self, fn):
            if fn in self:
                super().remove(fn)

    # The add-on registers against several hooks at import time; hand out a fresh recorder for
    # any of them rather than listing names that will drift.
    hooks.__getattr__ = lambda name: hooks.__dict__.setdefault(name, _Hook())
    hooks.reviewer_did_show_question = _Hook()
    hooks.main_window_did_init = _Hook()
    aqt.gui_hooks = hooks

    utils = types.ModuleType("aqt.utils")
    utils.showWarning = lambda *a, **k: None

    sys.modules["aqt"] = aqt
    sys.modules["aqt.qt"] = qt
    sys.modules["aqt.utils"] = utils
    sys.modules["aqt.gui_hooks"] = hooks


_install_aqt_stub()


def _import_built_addon():
    """Build the .ankiaddon and import it exactly as Anki would.

    Testing the built artifact rather than the source tree means the vendoring step is covered
    too: a module left out of the package would fail here rather than on the user's machine.
    """
    import tempfile
    import zipfile

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from build_addon import build

    archive = build()
    target = pathlib.Path(tempfile.mkdtemp()) / "anki_voice_review"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target)
    sys.path.insert(0, str(target.parent))
    import anki_voice_review.ui as ui_module
    from anki_voice_review.avr.config import Config as _Config

    return ui_module.SettingsDialog, _Config


SettingsDialog, Config = _import_built_addon()


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


STALE = {
    # Captured verbatim from the user's real installation.
    "terminator": "done",
    "override_window_seconds": 2.5,
    "headphones": False,
    "grading_mode": "auto",
    "flag_on_skip": 1,
    "fuzzy_correct": 0.75,
    "fuzzy_wrong": 0.4,
    "say_rate": "250",
}


class TestGradingModeSelector:
    """The setting whose silent failure looked exactly like a broken feature."""

    def test_choosing_manual_is_what_gets_saved(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        index = dialog.grading_mode.findData("manual")
        assert index >= 0, "there is no Manual option to choose"
        dialog.grading_mode.setCurrentIndex(index)
        assert dialog.values()["grading_mode"] == "manual"

    def test_the_saved_value_actually_turns_manual_mode_on(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        dialog.grading_mode.setCurrentIndex(dialog.grading_mode.findData("manual"))
        assert Config.from_mapping(dialog.values()).manual

    def test_it_opens_showing_the_mode_that_is_stored(self, qapp):
        dialog = SettingsDialog(None, {**STALE, "grading_mode": "manual"})
        assert dialog.grading_mode.currentData() == "manual"

    def test_automatic_round_trips_too(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        dialog.grading_mode.setCurrentIndex(dialog.grading_mode.findData("auto"))
        assert not Config.from_mapping(dialog.values()).manual


class TestEverySettingRoundTrips:
    """Each control must save the value it is showing. A control that silently fails to persist
    is indistinguishable from the feature behind it being broken."""

    def test_headphones_checkbox(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        dialog.headphones.setChecked(True)
        assert Config.from_mapping(dialog.values()).headphones

    def test_flag_on_skip(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        dialog.flag_on_skip.setCurrentIndex(dialog.flag_on_skip.findData(4))
        assert Config.from_mapping(dialog.values()).flag_on_skip == 4

    def test_flag_can_be_turned_off(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        dialog.flag_on_skip.setCurrentIndex(dialog.flag_on_skip.findData(0))
        assert Config.from_mapping(dialog.values()).flag_on_skip == 0

    def test_terminator(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        dialog.terminator.setText("finished")
        assert Config.from_mapping(dialog.values()).terminator == "finished"

    def test_pause_after_grading(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        dialog.override.setValue(0.0)
        assert Config.from_mapping(dialog.values()).override_window_s == 0.0

    def test_say_rate(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        dialog.say_rate.setText("230")
        assert Config.from_mapping(dialog.values()).say_rate == "230"

    def test_nothing_touched_means_nothing_changes(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        saved = dialog.values()
        for key, value in STALE.items():
            assert saved[key] == value, f"{key} changed without being touched"


class TestRestoreDefaults:
    def test_the_button_exists(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults) is not None

    def test_clicking_it_restores_the_current_defaults(self, qapp):
        dialog = SettingsDialog(None, dict(STALE))
        button = dialog.findChild(QDialogButtonBox).button(
            QDialogButtonBox.StandardButton.RestoreDefaults
        )
        button.click()

        cfg = Config.from_mapping(dialog.values())
        defaults = Config()
        assert cfg.override_window_s == defaults.override_window_s
        assert cfg.flag_on_skip == 0
        assert cfg.terminator == defaults.terminator

    def test_it_clears_a_stale_pause(self, qapp):
        # The specific value that stopped immediate advance from ever taking effect.
        dialog = SettingsDialog(None, dict(STALE))
        dialog.findChild(QDialogButtonBox).button(
            QDialogButtonBox.StandardButton.RestoreDefaults
        ).click()
        assert dialog.values()["override_window_seconds"] == 0.0


