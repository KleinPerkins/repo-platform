"""Run every required source shard and bind its result to the actual Actions attempt."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "KleinPerkins/repo-platform"
REPOSITORY_ID = 1472074383
SHARDS = ("adoption", "recovery")
CI_PATH = ".github/workflows/ci.yml"
MARKER = "DEV_CI_AGGREGATE_V1 "
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
ENV_KEYS = {
    "repository": "GITHUB_REPOSITORY", "repository_id": "GITHUB_REPOSITORY_ID",
    "event": "GITHUB_EVENT_NAME", "revision": "GITHUB_SHA", "workflow_sha": "GITHUB_WORKFLOW_SHA",
    "run_id": "GITHUB_RUN_ID", "run_attempt": "GITHUB_RUN_ATTEMPT", "ref": "GITHUB_REF",
    "head_sha": "CI_HEAD_SHA", "base_sha": "CI_BASE_SHA", "workflow_ref": "GITHUB_WORKFLOW_REF",
    "workflow": "GITHUB_WORKFLOW",
}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def decode(raw):
    require(len(raw) <= 1_000_000, "CI input exceeds size bound")

    def unique(pairs):
        obj = {}
        for key, value in pairs:
            require(key not in obj, "duplicate CI input key")
            obj[key] = value
        return obj

    return json.loads(raw, object_pairs_hook=unique)


def closed(value, fields):
    require(isinstance(value, dict) and value.keys() == set(fields.split()), "unexpected CI record fields")


def git(root, *args):
    return subprocess.check_output(
        ["git", "--no-optional-locks", "--no-replace-objects", "-C", str(root), *args],
        text=True, timeout=10,
    ).strip()


def policy(root=ROOT):
    contract_bytes = (root / ".github/ci-contract.json").read_bytes()
    shard_bytes = (root / ".github/test-shards.json").read_bytes()
    contract, manifest = decode(contract_bytes), decode(shard_bytes)
    closed(contract, "schema repository_id repository_names workflow workflow_name required_check actions_app_id "
           "reviewer_app_id dev_revision skills_revision case_floors")
    require(
        (contract["schema"], contract["repository_id"], contract["repository_names"], contract["workflow"],
         contract["workflow_name"], contract["required_check"], contract["actions_app_id"],
         contract["reviewer_app_id"]) == (
            "repo-platform-ci-contract/v1", REPOSITORY_ID, [REPOSITORY], CI_PATH, "CI", "ci-required", 15368, 4217367
        ),
        "unexpected CI authority",
    )
    require(all(isinstance(contract[k], str) and SHA.fullmatch(contract[k])
                for k in ("dev_revision", "skills_revision")), "missing exact dependency pins")
    require(isinstance(contract["case_floors"], dict) and contract["case_floors"].keys() == set(SHARDS)
            and all(type(n) is int and n > 0 for n in contract["case_floors"].values()), "invalid case floors")
    rows = manifest["shards"]
    require([row["id"] for row in rows] == list(SHARDS), "shard topology changed")
    for row in rows:
        require(row["command"] == ["python3", "-I", "-B", "scripts/run-source-shard.py", row["id"]],
                "unexpected shard entry point")
    return contract, {row["id"]: row for row in rows}, sha256(contract_bytes + b"\0" + shard_bytes).hexdigest()


def identity(environment, root=ROOT):
    value = {key: environment.get(name, "") for key, name in ENV_KEYS.items()}
    require((value["repository"], value["repository_id"], value["workflow"]) ==
            (REPOSITORY, str(REPOSITORY_ID), "CI"), "wrong repository, ID or workflow name")
    require(value["event"] in {"pull_request", "push", "workflow_dispatch"}, "unsupported CI event")
    require(all(re.fullmatch(r"[1-9][0-9]*", value[k]) for k in ("run_id", "run_attempt")), "invalid run or attempt")
    require(SHA.fullmatch(value["revision"]) and value["workflow_sha"] == value["revision"] and
            value["workflow_ref"] == f"{REPOSITORY}/{CI_PATH}@{value['ref']}", "wrong tested workflow")
    require(git(root, "rev-parse", "HEAD") == value["revision"], "checked-out revision differs")
    if value["event"] == "pull_request":
        require(re.fullmatch(r"refs/pull/[1-9][0-9]*/merge", value["ref"])
                and SHA.fullmatch(value["head_sha"]) and SHA.fullmatch(value["base_sha"]), "invalid PR event tuple")
        require(git(root, "show", "-s", "--format=%P", "HEAD").split() ==
                [value["base_sha"], value["head_sha"]], "PR merge parents differ")
    else:
        require(value["ref"] == "refs/heads/main" and value["head_sha"] == value["base_sha"] == "",
                "wrong main event tuple")
    return value


def verified_dev(contract, root=ROOT):
    dev = Path(os.environ["REPO_PLATFORM_DEV_ROOT"]).resolve(strict=True)
    require(git(dev, "rev-parse", "HEAD") == contract["dev_revision"], "Dev source revision differs")
    require(not git(dev, "status", "--porcelain=v1", "--untracked-files=all"), "Dev source checkout changed")
    pin = decode((dev / "_ops/portability/personal-index-runtime.json").read_bytes())
    require(pin["revision"] == contract["skills_revision"], "Dev's Skills pin differs")
    subprocess.run([sys.executable, "-I", "-B", str(dev / "_ops/bin/test-sharding.py"), "verify", "--repo",
                    str(root), "--require", "--activation"], check=True, timeout=30)
    return dev


def validate_measurement(value, row, floor):
    closed(value, "tests case_count case_ids_sha256")
    require(value["tests"] == row["tests"], "shard test files differ")
    require(type(value["case_count"]) is int and value["case_count"] >= floor, "empty or incomplete shard")
    require(isinstance(value["case_ids_sha256"], str) and DIGEST.fullmatch(value["case_ids_sha256"]),
            "invalid executed case digest")


def aggregate(needs, expected, contract, rows, digest):
    require(isinstance(needs, dict) and needs.keys() == set(SHARDS), "missing or extra shard")
    for name in SHARDS:
        result = needs[name]
        closed(result, "result outputs")
        require(result["result"] == "success", "shard failed, skipped or was cancelled")
        closed(result["outputs"], "receipt")
        receipt = decode(result["outputs"]["receipt"])
        closed(receipt, "schema shard identity result measurement contract_sha256")
        require((receipt["schema"], receipt["shard"], receipt["identity"], receipt["result"],
                 receipt["contract_sha256"]) ==
                ("repo-platform-ci-shard/v1", name, expected, "success", digest), "stale or mismatched shard receipt")
        validate_measurement(receipt["measurement"], rows[name], contract["case_floors"][name])
    return {"schema": "dev-ci-aggregate/v1", "identity": expected, "result": "success", "contract_sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "shard", "aggregate"))
    parser.add_argument("--shard", choices=SHARDS)
    args = parser.parse_args()
    require((args.mode == "shard") == (args.shard is not None), "select a shard only in shard mode")
    contract, rows, digest = policy()
    expected = identity(os.environ)
    dev = verified_dev(contract)
    if args.mode == "prepare":
        sys.path.insert(0, str(dev / "_ops/portability"))
        from personal_core import read_url
        from personal_index_package import stage, verify

        runtime = Path(os.environ["REPO_PLATFORM_SKILLS_ROOT"])
        runtime.mkdir(mode=0o700)
        stage(runtime, read_url)
        verify(runtime)
    elif args.mode == "aggregate":
        receipt = aggregate(decode(os.environ["CI_NEEDS"]), expected, contract, rows, digest)
        print(MARKER + encode(receipt))
    else:
        row = rows[args.shard]
        with TemporaryDirectory(prefix="repo-platform-ci-") as temp:
            report = Path(temp) / "measurement.json"
            subprocess.run([sys.executable, *row["command"][1:], "--report", str(report)], cwd=ROOT,
                           check=True, timeout=row["timeout_seconds"])
            measurement = decode(report.read_bytes())
        validate_measurement(measurement, row, contract["case_floors"][args.shard])
        receipt = {"schema": "repo-platform-ci-shard/v1", "shard": args.shard, "identity": expected,
                   "result": "success", "measurement": measurement, "contract_sha256": digest}
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write("receipt=" + encode(receipt) + "\n")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Repo-platform CI refused. {error}") from None
