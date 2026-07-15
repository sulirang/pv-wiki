from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "wikijs-sync-products" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pv_wiki.config import ConfigError, WikiSettings, allow_mirrors, state_path  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_defaults_do_not_require_secrets(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(str(state_path()).endswith(".hermes/data/pv-wiki/state.sqlite3"))
            self.assertFalse(allow_mirrors())

    def test_wiki_settings_require_https_origin(self) -> None:
        values = {"WIKIJS_URL": "https://wiki.example.com", "WIKIJS_TOKEN": "secret"}
        with mock.patch.dict(os.environ, values, clear=True):
            settings = WikiSettings.from_env()
            self.assertEqual("products", settings.path_prefix)
            self.assertTrue(settings.new_page_private)
            self.assertFalse(settings.new_page_published)

        values.update(
            {
                "WIKIJS_NEW_PAGE_PRIVATE": "false",
                "WIKIJS_NEW_PAGE_PUBLISHED": "true",
            }
        )
        with mock.patch.dict(os.environ, values, clear=True):
            settings = WikiSettings.from_env()
            self.assertFalse(settings.new_page_private)
            self.assertTrue(settings.new_page_published)

        values["WIKIJS_URL"] = "http://wiki.example.com/path"
        with mock.patch.dict(os.environ, values, clear=True):
            with self.assertRaises(ConfigError):
                WikiSettings.from_env()


if __name__ == "__main__":
    unittest.main()
