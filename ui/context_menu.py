from PySide6.QtGui import QIcon, QKeySequence
from PySide6.QtWidgets import QMenu


def style_context_menu(menu: QMenu) -> QMenu:
    for action in menu.actions():
        action.setIcon(QIcon())
        action.setShortcut(QKeySequence())
    menu.setStyleSheet(
        "QMenu::item { font-size: 17px; padding: 9px 24px; }")
    return menu
