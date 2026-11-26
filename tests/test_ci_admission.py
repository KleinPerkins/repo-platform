"""Test the closed CI receipts and the review seed's initial read-only authority."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verify_hosted", ROOT / "scripts/verify_hosted.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class CIAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.contract, self.rows, self.digest = verify.policy(ROOT)
        self.identity = {"run_id": "41", "run_attempt": "2", "revision": "0" * 40}
        self.needs = {}
        for name in verify.SHARDS:
            receipt = {
                "schema": "repo-platform-ci-shard/v1", "shard": name, "identity": self.identity,
                "result": "success", "contract_sha256": self.digest,
                "measurement": {"tests": self.rows[name]["tests"], "case_ids_sha256": "0" * 64,
                                "case_count": self.contract["case_floors"][name]},
            }
            self.needs[name] = {"result": "success", "outputs": {"receipt": verify.encode(receipt)}}

    def aggregate(self):
        return verify.aggregate(self.needs, self.identity, self.contract, self.rows, self.digest)

    def test_aggregate_accepts_both_current_nonempty_shards(self):
        receipt = self.aggregate()
        self.assertEqual(receipt["identity"], self.identity)
        self.assertEqual(receipt["contract_sha256"], self.digest)

    def test_aggregate_refuses_a_missing_shard(self):
        del self.needs["recovery"]
        with self.assertRaisesRegex(ValueError, "missing or extra"):
            self.aggregate()

    def test_aggregate_refuses_a_failed_job(self):
        self.needs["recovery"]["result"] = "failure"
        with self.assertRaisesRegex(ValueError, "failed"):
            self.aggregate()

    def test_aggregate_refuses_a_receipt_from_an_old_attempt(self):
        expected = deepcopy(self.identity)
        expected["run_attempt"] = "1"
        with self.assertRaisesRegex(ValueError, "stale or mismatched"):
            verify.aggregate(self.needs, expected, self.contract, self.rows, self.digest)

    def test_aggregate_refuses_an_empty_measurement(self):
        item = self.needs["recovery"]["outputs"]
        receipt = json.loads(item["receipt"])
        receipt["measurement"]["case_count"] = 0
        item["receipt"] = verify.encode(receipt)
        with self.assertRaisesRegex(ValueError, "empty or incomplete"):
            self.aggregate()

    def test_seed_only_exposes_read_only_app_qualification(self):
        policy = json.loads((ROOT / ".github/review-enrollments.json").read_bytes())
        workflow = json.loads((ROOT / ".github/workflows/reviewer-app.yml").read_bytes())
        self.assertEqual(policy["repository_id"], self.contract["repository_id"])
        self.assertEqual(policy["producers"], [])
        self.assertIs(policy["enabled"], False)
        self.assertEqual(workflow["on"], {"workflow_dispatch": {}})
        self.assertTrue(all(scope == "read" for scope in workflow["permissions"].values()))
        for step in workflow["jobs"]["admit"]["steps"]:
            self.assertNotIn("--write", step.get("run", ""))
            self.assertTrue(all(value == "read" for key, value in step.get("with", {}).items()
                                if key.startswith("permission-")))
