from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "n8n"


class ComposeTests(unittest.TestCase):
    def test_worker_is_internal_and_n8n_uses_a_separate_database(self) -> None:
        compose = yaml.safe_load(
            (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
        )
        services = compose["services"]
        self.assertEqual({"n8n", "n8n-db", "pv-wiki-worker"}, set(services))
        self.assertNotIn("ports", services["pv-wiki-worker"])
        self.assertEqual(
            ["127.0.0.1:${N8N_PORT:-5678}:5678"],
            services["n8n"]["ports"],
        )
        self.assertEqual("postgresdb", services["n8n"]["environment"]["DB_TYPE"])
        self.assertEqual(
            "n8n-db",
            services["n8n"]["environment"]["DB_POSTGRESDB_HOST"],
        )
        self.assertEqual(
            "true",
            services["n8n"]["environment"]["N8N_BLOCK_ENV_ACCESS_IN_NODE"],
        )
        excluded = services["n8n"]["environment"]["NODES_EXCLUDE"]
        self.assertIn("executeCommand", excluded)
        self.assertNotIn("/var/run/docker.sock", json.dumps(compose))
        self.assertEqual(
            ["database"],
            services["n8n-db"]["networks"],
        )
        self.assertEqual(
            {"automation", "database"},
            set(services["n8n"]["networks"]),
        )
        self.assertEqual(["automation"], services["pv-wiki-worker"]["networks"])
        self.assertTrue(compose["networks"]["database"]["internal"])
        worker_mounts = services["pv-wiki-worker"]["volumes"]
        self.assertTrue(
            any(
                isinstance(item, dict)
                and item.get("target") == "/run/pv-wiki/catalogue-ca.pem"
                and item.get("read_only") is True
                for item in worker_mounts
            )
        )

        bootstrap = yaml.safe_load(
            (DEPLOY / "compose.bootstrap.yaml").read_text(encoding="utf-8")
        )
        bootstrap_env = bootstrap["services"]["n8n"]["environment"]
        self.assertEqual("localhost", bootstrap_env["N8N_HOST"])
        self.assertEqual("http", bootstrap_env["N8N_PROTOCOL"])
        self.assertEqual("false", bootstrap_env["N8N_SECURE_COOKIE"])

    def test_secret_files_are_examples_only_and_docker_runs_non_root(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("worker.env", gitignore)
        worker_example = (DEPLOY / "worker.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("AI_BASE_URL=", worker_example)
        self.assertIn("AI_API_KEY=", worker_example)
        self.assertIn("AI_MODEL=", worker_example)
        self.assertNotIn("tvly-dev-", worker_example)
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER pvwiki", dockerfile)
        self.assertIn("ca-certificates", dockerfile)
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for pattern in (".env.*", "*.pem", "*.key", ".pgpass"):
            self.assertIn(pattern, dockerignore)

        existing = yaml.safe_load(
            (DEPLOY / "compose.existing-n8n.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {"pv-wiki-worker"},
            set(existing["services"]),
        )
        self.assertTrue(existing["networks"]["existing_n8n"]["external"])
        self.assertNotIn("ports", existing["services"]["pv-wiki-worker"])

        systemd = yaml.safe_load(
            (DEPLOY / "compose.systemd-n8n.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            ["127.0.0.1:8080:8080"],
            systemd["services"]["pv-wiki-worker"]["ports"],
        )


class WorkflowTemplateTests(unittest.TestCase):
    def test_templates_are_inactive_secret_free_and_use_only_safe_nodes(self) -> None:
        allowed_types = {
            "n8n-nodes-base.manualTrigger",
            "n8n-nodes-base.scheduleTrigger",
            "n8n-nodes-base.httpRequest",
            "n8n-nodes-base.if",
        }
        paths = sorted((DEPLOY / "workflows").glob("*.json"))
        self.assertEqual(2, len(paths))
        for path in paths:
            with self.subTest(path=path.name):
                workflow = json.loads(path.read_text(encoding="utf-8"))
                self.assertIs(workflow["active"], False)
                for node in workflow["nodes"]:
                    self.assertIn(node["type"], allowed_types)
                    self.assertNotIn("credentials", node)
                    if node["type"] == "n8n-nodes-base.httpRequest":
                        self.assertEqual(
                            "httpHeaderAuth",
                            node["parameters"]["genericAuthType"],
                        )
                        self.assertTrue(
                            node["parameters"]["url"].startswith(
                                "http://pv-wiki-worker:8080/"
                            )
                        )
                serialized = json.dumps(workflow)
                self.assertNotIn("Bearer ", serialized)
                self.assertNotIn("api_key", serialized.casefold())
                self.assertNotIn("executeCommand", serialized)

        homepage = json.loads(
            (DEPLOY / "workflows" / "pv-wiki-homepage-refresh.json").read_text(
                encoding="utf-8"
            )
        )
        schedule = next(
            node
            for node in homepage["nodes"]
            if node["type"] == "n8n-nodes-base.scheduleTrigger"
        )
        interval = schedule["parameters"]["rule"]["interval"][0]
        self.assertEqual("days", interval["field"])
        self.assertEqual(1, interval["daysInterval"])
        self.assertEqual(2, interval["triggerAtHour"])
        self.assertEqual(35, interval["triggerAtMinute"])

        idempotent_nodes = [
            node
            for path in paths
            for node in json.loads(path.read_text(encoding="utf-8"))["nodes"]
            if node["name"] in {
                "Sync Catalogue",
                "Refresh Catalogue",
                "Refresh Homepage",
            }
        ]
        self.assertEqual(3, len(idempotent_nodes))
        self.assertTrue(all(node["retryOnFail"] for node in idempotent_nodes))
        product = json.loads(
            (DEPLOY / "workflows" / "pv-wiki-product-cycle.json").read_text(
                encoding="utf-8"
            )
        )
        monthly = next(
            node
            for node in product["nodes"]
            if node["name"] == "Monthly Credits Refresh"
        )
        monthly_interval = monthly["parameters"]["rule"]["interval"][0]
        self.assertEqual("cronExpression", monthly_interval["field"])
        self.assertEqual("5 8 1 * *", monthly_interval["expression"])
        hourly = next(
            node
            for node in product["nodes"]
            if node["name"] == "Hourly Due Recovery"
        )
        hourly_interval = hourly["parameters"]["rule"]["interval"][0]
        self.assertEqual("hours", hourly_interval["field"])
        self.assertEqual(1, hourly_interval["hoursInterval"])
        self.assertEqual(47, hourly_interval["triggerAtMinute"])
        self.assertEqual(
            "Run One Product",
            product["connections"]["Hourly Due Recovery"]["main"][0][0][
                "node"
            ],
        )
        daily_catalogue = next(
            node
            for node in product["nodes"]
            if node["name"] == "Daily Catalogue Recovery"
        )
        daily_interval = daily_catalogue["parameters"]["rule"]["interval"][0]
        self.assertEqual("days", daily_interval["field"])
        self.assertEqual(1, daily_interval["daysInterval"])
        self.assertEqual(3, daily_interval["triggerAtHour"])
        self.assertEqual(17, daily_interval["triggerAtMinute"])
        self.assertEqual(
            "Refresh Catalogue",
            product["connections"]["Daily Catalogue Recovery"]["main"][0][0][
                "node"
            ],
        )
        self.assertEqual(
            "Run One Product",
            product["connections"]["Refresh Catalogue"]["main"][0][0]["node"],
        )

        continue_node = next(
            node
            for node in product["nodes"]
            if node["name"] == "Continue While Processed"
        )
        condition = continue_node["parameters"]["conditions"]["conditions"][0]
        self.assertEqual("={{ $json.processed }}", condition["leftValue"])
        self.assertEqual("true", condition["operator"]["operation"])
        loop_outputs = product["connections"]["Continue While Processed"]["main"]
        self.assertEqual("Run One Product", loop_outputs[0][0]["node"])
        self.assertEqual([], loop_outputs[1])

        run_one = next(
            node for node in product["nodes"] if node["name"] == "Run One Product"
        )
        sync_catalogue = next(
            node for node in product["nodes"] if node["name"] == "Sync Catalogue"
        )
        refresh_catalogue = next(
            node
            for node in product["nodes"]
            if node["name"] == "Refresh Catalogue"
        )
        for node in (sync_catalogue, refresh_catalogue):
            self.assertEqual("POST", node["parameters"]["method"])
            self.assertNotIn("sendBody", node["parameters"])
            self.assertNotIn("bodyParameters", node["parameters"])
            self.assertTrue(node["retryOnFail"])
            self.assertEqual(3, node["maxTries"])
            self.assertEqual(60_000, node["waitBetweenTries"])
            self.assertGreaterEqual(
                node["parameters"]["options"]["timeout"],
                600_000,
            )
        self.assertTrue(run_one["retryOnFail"])
        self.assertEqual(3, run_one["maxTries"])
        self.assertEqual(60000, run_one["waitBetweenTries"])
        self.assertGreaterEqual(
            run_one["parameters"]["options"]["timeout"],
            2_400_000,
        )


if __name__ == "__main__":
    unittest.main()
