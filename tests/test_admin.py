from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "wikijs-sync-products"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from pv_wiki import admin  # noqa: E402


class _Store:
    def __enter__(self) -> "_Store":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def append_control_event(self, product_id: str, **values: object) -> str:
        self.control = (product_id, values)
        return "p0:c1"

    def publication_status(self, product_id: str) -> dict[str, object]:
        return {
            "product_id": product_id,
            "mode": "pending",
            "suppressed": False,
            "fence": "p0:c0",
        }


class AdminTests(unittest.TestCase):
    def test_rerender_defaults_to_preview_and_apply_is_explicit(self) -> None:
        calls: list[bool] = []

        def rerender(_store: object, product_id: str, *, apply: bool) -> dict:
            calls.append(apply)
            return {"ok": True, "product_id": product_id, "applied": apply}

        for arguments, expected in (
            (["rerender", "--product-id", "P-42"], False),
            (["rerender", "--product-id", "P-42", "--apply"], True),
        ):
            with self.subTest(arguments=arguments), mock.patch.object(
                admin, "HermesCompletionStore", return_value=_Store()
            ), mock.patch.object(admin, "rerender_completion", side_effect=rerender), mock.patch(
                "sys.stdout", new_callable=io.StringIO
            ) as output:
                self.assertEqual(0, admin.main(arguments))
                self.assertEqual(expected, json.loads(output.getvalue())["applied"])
        self.assertEqual([False, True], calls)

    def test_control_and_status_use_completion_side_tables(self) -> None:
        store = _Store()
        with mock.patch.object(
            admin, "HermesCompletionStore", return_value=store
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(
                0,
                admin.main(
                    [
                        "suppress",
                        "--product-id",
                        "P-42",
                        "--reason",
                        "operator hold",
                        "--expected-fence",
                        "p0:c0",
                    ]
                ),
            )
            self.assertEqual("suppress", store.control[1]["action"])
            self.assertEqual("p0:c1", json.loads(output.getvalue())["fence"])

        with mock.patch.object(
            admin, "HermesCompletionStore", return_value=_Store()
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(
                0,
                admin.main(["status", "--product-id", "P-42"]),
            )
            self.assertEqual("pending", json.loads(output.getvalue())["mode"])


if __name__ == "__main__":
    unittest.main()
