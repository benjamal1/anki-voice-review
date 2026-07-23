"""The Voice Review window.

Deliberately one screen with one primary action. During a review the user is not looking at
it — they are talking — so the layout optimises for a glanceable state at arm's length: a
large status line, the card, what was heard, and a verdict. Settings live behind a button
rather than cluttering the review surface.

All worker callbacks arrive on a background thread, so every one of them is a Qt signal;
Qt queues cross-thread emissions onto the GUI thread automatically.
"""

from __future__ import annotations

from typing import Optional

from aqt import mw
from aqt.qt import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFont,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    Qt,
    QVBoxLayout,
    pyqtSignal,
)

from .avr.config import Config
from .bridge import AnkiBridge
from .worker import (
    PHASE_GRADING,
    PHASE_LISTENING,
    PHASE_LOADING,
    PHASE_SPEAKING,
    PHASE_STOPPED,
    PHASE_VERDICT,
    VoiceWorker,
)

PHASE_TEXT = {
    PHASE_LOADING: "Loading…",
    PHASE_SPEAKING: "Speaking",
    PHASE_LISTENING: "Listening — say your answer, then “{terminator}”",
    PHASE_GRADING: "Grading…",
    PHASE_VERDICT: "Grade it or wait",
    PHASE_STOPPED: "Stopped",
}
PHASE_COLOR = {
    PHASE_LOADING: "#9aa7b4",
    PHASE_SPEAKING: "#4aa3df",
    PHASE_LISTENING: "#4ade80",
    PHASE_GRADING: "#f4b942",
    PHASE_VERDICT: "#c9a0ff",
    PHASE_STOPPED: "#9aa7b4",
}


class SettingsDialog(QDialog):
    """Everything in config.json, with the two that matter most at the top."""

    def __init__(self, parent, config: dict) -> None:
        super().__init__(parent)
        self.setWindowTitle("Voice Review — Settings")
        self.config = dict(config)

        form = QFormLayout()

        self.terminator = QLineEdit(str(config.get("terminator", "done")))
        self.terminator.setToolTip("The word that ends your answer and triggers grading.")
        form.addRow("End-of-answer word", self.terminator)

        self.override = QDoubleSpinBox()
        self.override.setRange(0.0, 15.0)
        self.override.setSingleStep(0.5)
        self.override.setSuffix(" s")
        self.override.setValue(float(config.get("override_window_seconds", 2.5)))
        self.override.setToolTip("How long you get to countermand the grade by voice.")
        form.addRow("Override window", self.override)

        self.use_judge = QCheckBox("Use the local LLM for ambiguous answers")
        self.use_judge.setChecked(bool(config.get("use_llm_judge", True)))
        self.use_judge.setToolTip(
            "When off, grading is purely mechanical: faster, but a correct answer worded\n"
            "differently from the card will be marked wrong."
        )
        form.addRow("", self.use_judge)

        self.fuzzy_correct = QDoubleSpinBox()
        self.fuzzy_correct.setRange(0.0, 1.0)
        self.fuzzy_correct.setSingleStep(0.05)
        self.fuzzy_correct.setValue(float(config.get("fuzzy_correct", 0.75)))
        form.addRow("Correct at or above", self.fuzzy_correct)

        self.fuzzy_wrong = QDoubleSpinBox()
        self.fuzzy_wrong.setRange(0.0, 1.0)
        self.fuzzy_wrong.setSingleStep(0.05)
        self.fuzzy_wrong.setValue(float(config.get("fuzzy_wrong", 0.40)))
        form.addRow("Incorrect below", self.fuzzy_wrong)

        self.model_path = QLineEdit(str(config.get("whisper_model", "")))
        form.addRow("Whisper model", self.model_path)

        self.ollama_model = QLineEdit(str(config.get("ollama_model", "qwen2.5:3b")))
        form.addRow("Judge model", self.ollama_model)

        self.say_voice = QLineEdit(str(config.get("say_voice", "")))
        self.say_voice.setPlaceholderText("system default")
        form.addRow("Voice", self.say_voice)

        self.say_rate = QLineEdit(str(config.get("say_rate", 190)))
        form.addRow("Speech rate", self.say_rate)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def values(self) -> dict:
        updated = dict(self.config)
        updated.update(
            {
                "terminator": self.terminator.text().strip() or "done",
                "override_window_seconds": self.override.value(),
                "use_llm_judge": self.use_judge.isChecked(),
                "fuzzy_correct": self.fuzzy_correct.value(),
                "fuzzy_wrong": self.fuzzy_wrong.value(),
                "whisper_model": self.model_path.text().strip(),
                "ollama_model": self.ollama_model.text().strip(),
                "say_voice": self.say_voice.text().strip(),
                "say_rate": self.say_rate.text().strip() or "190",
            }
        )
        return updated


class VoiceReviewDialog(QDialog):
    # Worker callbacks land on a background thread; signals hop them to the GUI thread.
    sig_phase = pyqtSignal(str, str)
    sig_card = pyqtSignal(str)
    sig_heard = pyqtSignal(str)
    sig_verdict = pyqtSignal(bool, float, str)
    sig_error = pyqtSignal(str)
    sig_finished = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent or mw)
        self.setWindowTitle("Voice Review")
        self.setMinimumWidth(520)
        self.worker: Optional[VoiceWorker] = None
        self.bridge = AnkiBridge()

        self._build()
        self._connect()
        self._refresh_ready_state()

    # --- layout ---

    def _build(self) -> None:
        layout = QVBoxLayout()
        layout.setSpacing(14)

        self.status = QLabel("Ready")
        status_font = QFont()
        status_font.setPointSize(20)
        status_font.setBold(True)
        self.status.setFont(status_font)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status)

        self.hint = QLabel("")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setStyleSheet("color: palette(mid);")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.card_label = QLabel("")
        self.card_label.setWordWrap(True)
        self.card_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_font = QFont()
        card_font.setPointSize(14)
        self.card_label.setFont(card_font)
        self.card_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout.addWidget(self.card_label)

        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setMaximumHeight(150)
        self.transcript.setPlaceholderText("What the microphone hears will appear here.")
        layout.addWidget(self.transcript)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("Start")
        self.start_button.setDefault(True)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.settings_button = QPushButton("Settings…")
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        buttons.addWidget(self.settings_button)
        layout.addLayout(buttons)

        self.commands = QLabel(
            "Say your answer, then the end word. "
            "Commands: <b>again</b> · <b>hard</b> · <b>good</b> · <b>easy</b> · "
            "<b>repeat</b> · <b>skip</b> · <b>quit</b>"
        )
        self.commands.setWordWrap(True)
        self.commands.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(self.commands)

        self.setLayout(layout)

    def _connect(self) -> None:
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.settings_button.clicked.connect(self.open_settings)

        self.sig_phase.connect(self._on_phase)
        self.sig_card.connect(self._on_card)
        self.sig_heard.connect(self._on_heard)
        self.sig_verdict.connect(self._on_verdict)
        self.sig_error.connect(self._on_error)
        self.sig_finished.connect(self._on_finished)

    # --- config ---

    def _config(self) -> dict:
        return mw.addonManager.getConfig(__name__.split(".")[0]) or {}

    def _save_config(self, data: dict) -> None:
        mw.addonManager.writeConfig(__name__.split(".")[0], data)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self, self._config())
        if dialog.exec():
            values = dialog.values()
            problems = Config.from_mapping(values).validate()
            if problems:
                self._set_hint("; ".join(problems), error=True)
                return
            self._save_config(values)
            self._set_hint("Settings saved.")
            self._refresh_ready_state()

    # --- state ---

    def _refresh_ready_state(self) -> None:
        if self.bridge.is_reviewing():
            self._set_hint("")
            self.start_button.setEnabled(True)
        else:
            self._set_hint(
                "No card is showing. Open a deck and click Study Now, then press Start.",
                error=True,
            )
            self.start_button.setEnabled(True)  # start anyway; the worker reports the details

    def _set_hint(self, text: str, error: bool = False) -> None:
        self.hint.setText(text)
        self.hint.setStyleSheet(f"color: {'#ff6b6b' if error else 'palette(mid)'};")

    def _set_status(self, text: str, color: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    # --- worker plumbing ---

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        cfg = Config.from_mapping(self._config())
        problems = cfg.validate()
        if problems:
            self._set_hint("; ".join(problems), error=True)
            return

        self.terminator_word = cfg.terminator
        self.transcript.clear()
        self._set_hint("")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        self.worker = VoiceWorker(
            cfg=cfg,
            bridge=self.bridge,
            on_phase=lambda phase, detail: self.sig_phase.emit(phase, detail),
            on_card=lambda text: self.sig_card.emit(text),
            on_heard=lambda text: self.sig_heard.emit(text),
            on_verdict=lambda ok, score, src: self.sig_verdict.emit(ok, score, src),
            on_error=lambda text: self.sig_error.emit(text),
            on_finished=lambda text: self.sig_finished.emit(text),
        )
        self.worker.start()

    def stop(self) -> None:
        if self.worker:
            self.worker.request_stop()
        self.stop_button.setEnabled(False)

    def closeEvent(self, event) -> None:
        # A window closed with the loop still running would leave whisper-stream holding the
        # microphone with nothing to turn it off.
        self.stop()
        super().closeEvent(event)

    # --- signal handlers (GUI thread) ---

    def _on_phase(self, phase: str, detail: str) -> None:
        template = PHASE_TEXT.get(phase, phase)
        text = template.format(terminator=getattr(self, "terminator_word", "done"))
        self._set_status(text, PHASE_COLOR.get(phase, "palette(text)"))

    def _on_card(self, question: str) -> None:
        self.card_label.setText(question)
        self.transcript.clear()

    def _on_heard(self, text: str) -> None:
        self.transcript.appendPlainText(text)

    def _on_verdict(self, correct: bool, score: float, source: str) -> None:
        label = "Correct" if correct else "Incorrect"
        self._set_status(label, "#4ade80" if correct else "#ff6b6b")
        self._set_hint(f"score {score:.2f} · decided by {source}")

    def _on_error(self, text: str) -> None:
        self._set_status("Problem", "#ff6b6b")
        self._set_hint(text, error=True)

    def _on_finished(self, summary: str) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if summary:
            self._set_hint(summary)
