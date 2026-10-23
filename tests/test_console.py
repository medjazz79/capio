import unittest
from unittest import mock


class TestStandaloneConsole(unittest.TestCase):
    def _console_apis(self):
        kernel32 = mock.MagicMock()
        user32 = mock.MagicMock()
        kernel32.GetConsoleWindow.return_value = 123
        return kernel32, user32

    def test_minimizes_console_attached_only_to_this_process(self):
        import config.console

        kernel32, user32 = self._console_apis()
        kernel32.GetConsoleProcessList.return_value = 1
        with (
            mock.patch("config.console.os.name", "nt"),
            mock.patch("ctypes.WinDLL", side_effect=[kernel32, user32]),
        ):
            config.console.minimize_standalone_console()

        user32.ShowWindow.assert_called_once_with(123, 7)

    def test_keeps_existing_terminal_visible(self):
        import config.console

        kernel32, user32 = self._console_apis()
        kernel32.GetConsoleProcessList.return_value = 2
        with (
            mock.patch("config.console.os.name", "nt"),
            mock.patch("ctypes.WinDLL", side_effect=[kernel32, user32]),
        ):
            config.console.minimize_standalone_console()

        user32.ShowWindow.assert_not_called()
