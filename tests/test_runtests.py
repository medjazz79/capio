import unittest

from tools import runtests


class TestTestModuleDiscovery(unittest.TestCase):
    def test_existing_test_modules_are_all_discovered(self):
        expected = {
            "tests.test_document_import",
            "tests.test_google_translate",
            "tests.test_usage",
        }
        self.assertTrue(expected.issubset(set(runtests.MODULES)))
