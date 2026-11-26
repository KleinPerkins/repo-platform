"""Keep the GitHub delivery seed bound to the existing governed project."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return json.loads((ROOT / name).read_bytes())


class FoundationIdentityTests(unittest.TestCase):
    def test_ci_and_doc_warden_share_the_github_repository(self):
        ci = read(".github/ci-contract.json")
        governance = read(".planning/doc-warden.json")
        self.assertEqual(ci["repository_id"], governance["github_repository_id"])
        self.assertEqual(ci["repository_names"], ["KleinPerkins/repo-platform"])

    def test_canonical_project_uses_the_governed_repository_key(self):
        doc = (ROOT / ".planning/PROJECT.md").read_text()
        key = read(".planning/doc-warden.json")["repository_key"]
        self.assertRegex(doc, rf"Canonical project: `PROJECT-{key}-[0-9a-f-]+`")

    def test_born_v2_governance_remains_enabled(self):
        governance = read(".planning/doc-warden.json")
        self.assertEqual(governance["schema"], "doc-warden/config/v2")
        self.assertEqual(governance["schema_version"], 2)
        self.assertIs(governance["enabled"], True)

    def test_source_requires_the_born_sharding_gate(self):
        requirement = read(".github/test-sharding-required.json")
        self.assertEqual(requirement, {"schema": "dev-test-sharding-requirement/v1", "required": True})
        self.assertEqual(read(".github/test-shards.json")["aggregate"], "ci-required")
