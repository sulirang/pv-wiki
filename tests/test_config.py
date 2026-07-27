from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "wikijs-sync-products" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pv_wiki.config import (  # noqa: E402
    ConfigError,
    GlobalResearchBudgetSettings,
    ResearchSettings,
    TrustedSourceNotConfigured,
    WikiSettings,
    allow_mirrors,
    missing_environment,
    public_brand_alias,
    public_brand_alias_map,
    state_path,
    state_target,
    supplier_public_category,
    supplier_registry_entry,
    supplier_search_excluded_domains,
    trusted_source_domain_map,
    trusted_source_domains,
    trusted_source_domains_for_product,
)


class ConfigTests(unittest.TestCase):
    def test_bundled_supplier_registry_uses_operator_brand_table(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual("Fox ESS", public_brand_alias("FOX"))
            self.assertEqual("JA Solar", public_brand_alias("ja"))
            self.assertEqual("", public_brand_alias("HPA"))
            self.assertEqual(
                "heat pump accessories",
                supplier_public_category("HPA"),
            )
            self.assertEqual(
                frozenset(
                    {
                        "outes.com",
                        "outesgroup.com",
                        "outes-tech.com",
                        "outes.eu",
                        "outes.it",
                    }
                ),
                trusted_source_domains_for_product("OTS", "OUTES"),
            )
            self.assertEqual(
                "manufacturer",
                supplier_registry_entry("TRN")["kind"],
            )
            self.assertEqual(
                frozenset({"jasolar.com"}),
                trusted_source_domains_for_product("JA", "JA Solar"),
            )
            self.assertEqual(
                frozenset(
                    {
                        "jinkosolar.com",
                        "jinkosolarcdn.shwebspace.com",
                    }
                ),
                trusted_source_domains_for_product("JK", "JinkoSolar"),
            )
            self.assertIn(
                "scribd.com",
                supplier_search_excluded_domains(),
            )

    def test_defaults_do_not_require_secrets(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HOME": tempfile.gettempdir(),
                "USERPROFILE": tempfile.gettempdir(),
            },
            clear=True,
        ):
            self.assertTrue(
                state_path().as_posix().endswith(
                    ".local/state/pv-wiki/state.sqlite3"
                )
            )
            self.assertFalse(allow_mirrors())

    def test_state_target_prefers_valid_postgresql_url(self) -> None:
        database_url = (
            "postgresql://pv_wiki_app:p%40ss@state-db:5432/pv_wiki_state"
        )
        with mock.patch.dict(
            os.environ,
            {
                "PV_WIKI_STATE_DATABASE_URL": database_url,
                "PV_WIKI_STATE_PATH": "/ignored/state.sqlite3",
            },
            clear=True,
        ):
            self.assertEqual(database_url, state_target())

    def test_state_target_rejects_incomplete_or_non_postgresql_urls(
        self,
    ) -> None:
        invalid_urls = (
            "sqlite:////tmp/state.sqlite3",
            "postgresql://state-db/pv_wiki_state",
            "postgresql://user:password@state-db/",
            "postgresql://user:password@:5432/pv_wiki_state",
            "postgresql://user:password@state-db:99999/pv_wiki_state",
        )
        for database_url in invalid_urls:
            with self.subTest(database_url=database_url), mock.patch.dict(
                os.environ,
                {"PV_WIKI_STATE_DATABASE_URL": database_url},
                clear=True,
            ):
                with self.assertRaises(ConfigError):
                    state_target()

    def test_wiki_settings_require_https_origin(self) -> None:
        values = {"WIKIJS_URL": "https://wiki.example.com", "WIKIJS_TOKEN": "secret"}
        with mock.patch.dict(os.environ, values, clear=True):
            settings = WikiSettings.from_env()
            self.assertEqual("products", settings.path_prefix)
            self.assertEqual("home", settings.home_path)
            self.assertEqual("PV Wiki", settings.home_title)
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

    def test_placeholders_and_invalid_trusted_domains_fail_closed(self) -> None:
        values = {
            "WIKIJS_URL": "https://wiki.example.com",
            "WIKIJS_TOKEN": "replace-with-a-restricted-token",
            "PGPASSWORD": "replace-me",
            "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON":
                '{"Acme":["https://maker.example/path"]}',
        }
        with mock.patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(ConfigError, "placeholder"):
                WikiSettings.from_env()
            self.assertEqual(
                ["PGPASSWORD"],
                missing_environment(("PGPASSWORD",)),
            )
            with self.assertRaisesRegex(ConfigError, "hostnames"):
                trusted_source_domains("Acme")

        with mock.patch.dict(
            os.environ,
            {
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON":
                    '{"Acme":["maker.example","docs.maker.example"]}'
            },
            clear=True,
        ):
            self.assertEqual(
                frozenset({"maker.example", "docs.maker.example"}),
                trusted_source_domains("Acme"),
            )

        for public_suffix in (
            "co.uk",
            "github.io",
            "attacker.github.io",
            "s3.amazonaws.com",
            "tenant.appspot.com",
            "site.wordpress.com",
            "bucket.storage.googleapis.com",
            "tenant.blob.core.windows.net",
            "sites.google.com",
            "docs.google.com",
            "acme.medium.com",
            "1.1.1.1",
        ):
            with self.subTest(public_suffix=public_suffix), mock.patch.dict(
                os.environ,
                {
                    "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON": json.dumps(
                        {"Acme": [public_suffix]}
                    )
                },
                clear=True,
            ):
                with self.assertRaisesRegex(ConfigError, "public or shared"):
                    trusted_source_domains("Acme")

        values.update(
            {
                "WIKIJS_URL": "https://wiki.example.com",
                "WIKIJS_TOKEN": "secret",
                "WIKIJS_HOME_PATH": "../home",
            }
        )
        with mock.patch.dict(os.environ, values, clear=True):
            with self.assertRaises(ConfigError):
                WikiSettings.from_env()

    def test_research_settings_are_bounded_and_operator_tunable(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = ResearchSettings.from_env()
        self.assertEqual(3, settings.max_rounds)
        self.assertEqual(7, settings.max_queries)
        self.assertEqual(20, settings.max_credits)
        self.assertEqual(600.0, settings.max_seconds)

        configured = {
            "PV_WIKI_RESEARCH_MAX_ROUNDS": "2",
            "PV_WIKI_RESEARCH_MAX_QUERIES": "5",
            "PV_WIKI_RESEARCH_MAX_CREDITS": "12",
            "PV_WIKI_RESEARCH_MAX_SECONDS": "300",
        }
        with mock.patch.dict(os.environ, configured, clear=True):
            settings = ResearchSettings.from_env()
        self.assertEqual((2, 5, 12, 300.0), (
            settings.max_rounds,
            settings.max_queries,
            settings.max_credits,
            settings.max_seconds,
        ))

        for name, value in (
            ("PV_WIKI_RESEARCH_MAX_ROUNDS", "4"),
            ("PV_WIKI_RESEARCH_MAX_QUERIES", "8"),
            ("PV_WIKI_RESEARCH_MAX_CREDITS", "2"),
            ("PV_WIKI_RESEARCH_MAX_SECONDS", "30"),
        ):
            with self.subTest(name=name), mock.patch.dict(
                os.environ,
                {name: value},
                clear=True,
            ):
                with self.assertRaises(ConfigError):
                    ResearchSettings.from_env()

    def test_global_research_budget_is_opt_in_and_bounded(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = GlobalResearchBudgetSettings.from_env()
        self.assertEqual(0, settings.daily_credit_limit)
        self.assertEqual(0, settings.monthly_credit_limit)

        with mock.patch.dict(
            os.environ,
            {
                "PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT": "500",
                "PV_WIKI_GLOBAL_MONTHLY_CREDIT_LIMIT": "10000",
            },
            clear=True,
        ):
            settings = GlobalResearchBudgetSettings.from_env()
        self.assertEqual(500, settings.daily_credit_limit)
        self.assertEqual(10_000, settings.monthly_credit_limit)

        for name, value in (
            ("PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT", "-1"),
            ("PV_WIKI_GLOBAL_MONTHLY_CREDIT_LIMIT", "10000001"),
            ("PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT", "not-an-integer"),
        ):
            with self.subTest(name=name), mock.patch.dict(
                os.environ,
                {name: value},
                clear=True,
            ):
                with self.assertRaises(ConfigError):
                    GlobalResearchBudgetSettings.from_env()

    def test_trusted_override_is_bound_to_catalogue_brand(self) -> None:
        environment = {
            "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON": (
                '{"Acme":["maker.example"],"CAT-7":["catalogue.example"]}'
            )
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                frozenset(),
                trusted_source_domains_for_product("", "  acME "),
            )
            self.assertEqual(
                frozenset({"catalogue.example"}),
                trusted_source_domains_for_product("CAT-7", "Acme"),
            )
            self.assertEqual(
                frozenset(),
                trusted_source_domains_for_product("UNKNOWN", "Acme"),
            )
            self.assertEqual(
                frozenset(),
                trusted_source_domains_for_product("", "Unknown Manufacturer"),
            )
            self.assertEqual(
                frozenset({"catalogue.example"}),
                trusted_source_domains_for_product("CAT-7", ""),
            )
            self.assertEqual(
                frozenset({"maker.example"}),
                trusted_source_domains_for_product("Acme", "Contoso"),
            )

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual({}, trusted_source_domain_map())
            self.assertEqual(
                frozenset(),
                trusted_source_domains_for_product("Acme", "Acme"),
            )

        self.assertFalse(issubclass(TrustedSourceNotConfigured, ConfigError))

    def test_public_brand_aliases_are_explicit_and_strict(self) -> None:
        environment = {
            "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON": (
                '{"FOX":"Fox ESS","HUAWEI":" Huawei "}'
            )
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                {"fox": "Fox ESS", "huawei": "Huawei"},
                public_brand_alias_map(),
            )
            self.assertEqual("Fox ESS", public_brand_alias(" fox "))
            self.assertEqual("", public_brand_alias("UNKNOWN"))
            self.assertEqual("", public_brand_alias(None))

        with mock.patch.dict(
            os.environ,
            {
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON":
                    '{"FOX":"Fox ESS"}',
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON":
                    '{"Fox ESS":["foxess.example"]}',
            },
            clear=True,
        ):
            self.assertEqual(
                frozenset({"foxess.example"}),
                trusted_source_domains_for_product("FOX", "Malicious Override"),
            )

        with mock.patch.dict(
            os.environ,
            {
                "PV_WIKI_PUBLIC_BRAND_ALIASES_JSON":
                    '{"FOX":"Fox ESS"}',
                "PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON": (
                    '{"FOX":["catalogue.example"],'
                    '"Fox ESS":["manufacturer.example"]}'
                ),
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "conflict"):
                trusted_source_domains_for_product("FOX", "Fox ESS")

        invalid_values = (
            '["FOX","Fox ESS"]',
            '{"FOX":""}',
            '{"FOX":"https://foxess.example"}',
            '{"FOX":"12345"}',
            '{"FOX":"Fox\\nESS"}',
            '{"FOX":"Fox ESS","fox":"Different"}',
            '{"FOX":"Fox ESS","FOX":"Different"}',
        )
        for configured in invalid_values:
            with self.subTest(configured=configured), mock.patch.dict(
                os.environ,
                {"PV_WIKI_PUBLIC_BRAND_ALIASES_JSON": configured},
                clear=True,
            ):
                with self.assertRaises(ConfigError):
                    public_brand_alias_map()

    def test_malformed_trusted_source_map_is_not_a_product_lookup_miss(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON": "{broken"},
            clear=True,
        ):
            with self.assertRaises(ConfigError):
                trusted_source_domains_for_product("", "Acme")


if __name__ == "__main__":
    unittest.main()
