import tempfile
import unittest
from pathlib import Path
from unittest import mock

from translators import usage


class TestUsageSession(unittest.TestCase):
    def test_start_session_clears_previous_counts_and_persisted_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "usage.json"
            with mock.patch.object(usage, "LEDGER_PATH", ledger_path):
                usage.reset()
                usage.note("model-a", {
                    "prompt_tokens": 70,
                    "completion_tokens": 30,
                    "total_tokens": 100,
                }, provider="test")
                usage.save(force=True)

                self.assertTrue(ledger_path.exists())
                self.assertEqual(usage.snapshot()["total"], 100)

                usage.start_session()

                self.assertEqual(usage.snapshot()["total"], 0)
                self.assertEqual(usage.snapshot()["requests"], 0)
                self.assertEqual(usage.snapshot()["models"], [])
                self.assertFalse(ledger_path.exists())
