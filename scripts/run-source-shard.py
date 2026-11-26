"""Run one repo-platform test shard with the owner-selected Dev and Skills inputs."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import unittest


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def startTest(self, test):
        self.cases.append(test.id())
        super().startTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shard")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".github/test-shards.json").read_text())
    claimed = [shard for shard in manifest["shards"] if shard["id"] == args.shard]
    if len(claimed) != 1:
        parser.error("select exactly one configured shard")
    try:
        dev = Path(os.environ["REPO_PLATFORM_DEV_ROOT"]).resolve(strict=True) / "_ops/portability"
        skills = Path(os.environ["REPO_PLATFORM_SKILLS_ROOT"]).resolve(strict=True)
    except (KeyError, OSError):
        parser.error("provide prepared Dev and Skills sources in REPO_PLATFORM_DEV_ROOT and REPO_PLATFORM_SKILLS_ROOT")

    sys.path[:0] = [str(root), str(root / "tests"), str(dev)]
    from personal_index_package import index_digests, parser_digests, verify
    from skills_index_runtime import bind_index_runtime

    verify(skills)
    bind_index_runtime(skills, index_digests(), parser_digests=parser_digests())
    os.environ["PYTHONPATH"] = os.pathsep.join(map(str, (root, dev, skills, skills / "vendor")))
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    tests = unittest.TestSuite()
    for member in claimed[0]["tests"]:
        path = root / member
        tests.addTests(unittest.defaultTestLoader.discover(str(path.parent), pattern=path.name))
    if not tests.countTestCases():
        parser.error("the declared shard discovered no tests")
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(tests)
    if not result.wasSuccessful() or result.skipped or not result.cases or len(result.cases) != len(set(result.cases)):
        return 1
    if args.report:
        digest = sha256(json.dumps(sorted(result.cases), separators=(",", ":")).encode()).hexdigest()
        args.report.write_text(json.dumps({"tests": claimed[0]["tests"], "case_count": len(result.cases),
                                           "case_ids_sha256": digest}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
