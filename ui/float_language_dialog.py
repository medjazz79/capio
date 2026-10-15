"""Language selection for the standalone floating translator."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QPushButton, QVBoxLayout,
)

from translators.prompt import LANGUAGE_CHOICES

FLOAT_SOURCE_CHOICES = (("Auto", "auto"), *LANGUAGE_CHOICES)


class FloatLanguageDialog(QDialog):
    """Choose the floating translator's source and target languages."""

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Floating Translator Languages")
        self.setMinimumWidth(300)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.source_language = QComboBox()
        self.target_language = QComboBox()
        for label, code in FLOAT_SOURCE_CHOICES:
            self.source_language.addItem(label, code)
        for label, code in LANGUAGE_CHOICES:
            self.target_language.addItem(label, code)
        self._select_code(
            self.source_language, settings.get("float_source_language", "auto"))
        self._select_code(
            self.target_language, settings.get("float_target_language", "ar"))
        form.addRow("Translate from:", self.source_language)
        form.addRow("Translate to:", self.target_language)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    @staticmethod
    def _select_code(combo: QComboBox, code: str) -> None:
        index = combo.findData(code)
        if index >= 0:
            combo.setCurrentIndex(index)

    def values(self) -> tuple[str, str]:
        return (
            str(self.source_language.currentData()),
            str(self.target_language.currentData()),
        )
