from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from github_checkout_provider import GitHubRepositoryObservation
from issue_checkout_evidence import issue_checkout_evidence_reissue, observe_checkout_authorities
from lib.contracts import encode
from lib.engine import refresh_admitted
from native_checkout_project import NativeProjectBinding
from personal_checkout_inventory import Binding, Inventory, Refused
from physical_checkout_binding import OwnerPhysicalBinding, OwnerPhysicalBindings, ROLES
from repo_platform import adoption, command
from repo_platform.discovery import SkillsDiscovery
from skills_inventory_projection import guarded_admission, project_inventory, store_projection


HOSTED_HEAD = "ace0" * 10
SKILLS_REVISION = "a6f797c28fa1f6ef602b78e9c5d8ac871f781755"


def git(root, *args):
    return subprocess.check_output(
        ["/usr/bin/git", "-C", str(root), *args],
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null"}, stderr=subprocess.PIPE, text=True,
    ).strip()


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="repo-platform-adoption-", dir=Path("/tmp").resolve())
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        primary = self.root / "sample.product"
        primary.mkdir()
        git(primary, "init", "-b", "fixture", "--template=")
        git(primary, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "--allow-empty", "-m", "Fixture")
        git(primary, "remote", "add", "origin", "https://github.com/example/sample.product.git")
        self.local_head = git(primary, "rev-parse", "HEAD")
        linked = self.root / "linked.fixture"
        git(primary, "worktree", "add", "--detach", str(linked))
        common = primary / ".git"
        self.primary = Binding(str(primary), str(common), str(common), "github", 73, "clone",
                               "primary", ("adopt", "enumerate"))
        self.linked = Binding(str(linked), str(common / "worktrees" / linked.name), str(common),
                              "github", 73, "clone", "linked", ("adopt", "enumerate"))
        peer = self.root / "other" / primary.name
        peer.mkdir(parents=True)
        git(peer, "init", "-b", "fixture", "--template=")
        git(peer, "remote", "add", "origin", "https://github.com/other/sample.product.git")
        self.peer = Binding(str(peer), str(peer / ".git"), str(peer / ".git"), "github", 74,
                            "peer-clone", "peer", ("adopt", "enumerate"))
        self.denied = str(self.root / "denied")
        (self.root / "authority").mkdir(mode=0o700)
        self.inventory = Inventory(self.root / "authority")
        version = self.inventory.initialize("fixture-host", "fixture-policy", [self.denied],
                                            [self.primary, self.linked, self.peer])
        proofs = []
        for binding in (self.primary, self.linked, self.peer):
            locations = []
            for role in ROLES:
                path = getattr(binding, role)
                info = Path(path).stat()
                locations.append((role, path, info.st_dev, info.st_ino))
            proofs.append(OwnerPhysicalBinding(binding.provider, binding.repository_id, binding.clone_id,
                                               binding.worktree_id, tuple(locations)))
        self.physical = OwnerPhysicalBindings(tuple(proofs))
        selections = {binding.checkout: "fixture-project" for binding in (self.primary, self.linked)}
        observations = observe_checkout_authorities(
            selections, repository_ids=dict.fromkeys(selections, 73), read_native=self.native,
            read_hosting=self.hosting, deadline=time.monotonic() + 3,
        )
        self.version = issue_checkout_evidence_reissue(
            self.inventory, version, "proof", selections, observations, physical=self.physical,
            deadline=time.monotonic() + 3,
        )

    def native(self, project_id, checkout, *, deadline):
        thread = "fixture-thread" if checkout == self.linked.checkout else None
        return NativeProjectBinding(project_id, checkout, "Fixture in Codex", None, int(time.time()), thread)

    def hosting(self, repository_id, *, deadline):
        now = int(time.time())
        return GitHubRepositoryObservation(repository_id, "example/sample.product", "main",
                                           HOSTED_HEAD, True, False, now, now)

    def select(self, path, *, read_native=None, read_hosting=None, thread=None, discovery=None):
        return adoption.preview(
            self.inventory, path, "fixture-project", "proof", physical=self.physical,
            read_native=read_native or self.native, read_hosting=read_hosting or self.hosting,
            thread_id=thread, deadline=time.monotonic() + 3, expected_title="Sample Product",
            discovery=discovery,
        )

    def index(self, *, stale_offset=0, bad_origin=False, optional_missing=False,
              conflicting_alias=False, publish=True):
        storage = self.root / "authority"
        config = {"schema": "ecosystem-index/config/v1", "exclude_paths": [], "exclude_identities": [],
                  "bounds": {"roots": 3, "entities": 32, "input_bytes": 65536,
                             "snapshot_bytes": 1048576, "refresh_seconds": 10}, "roots": []}
        for binding in (self.primary, self.linked, self.peer):
            origin = "different.product" if bad_origin and binding == self.linked else "sample.product"
            namespace = "other" if binding == self.peer else "example"
            root = {"key": binding.worktree_id, "path": binding.checkout, "scope": "permitted", "inventory": "native",
                    "title": "Sample Product", "aliases": [], "storage_fields": ["title"], "sources": [],
                    "identity": {"provider": None, "remote": f"https://github.com/{namespace}/{origin}"},
                    "git": {"git_dir": binding.git_directory, "common_dir": binding.common_directory}}
            if optional_missing and binding == self.peer:
                root["storage_fields"].append("description")
                root["sources"] = [{"adapter": "json", "path": "optional.json", "fields": ["description"],
                                    "kind": "workspace", "revision": None}]
            if conflicting_alias and binding == self.peer:
                root["storage_fields"].append("aliases")
                root["aliases"] = [self.primary.checkout]
            config["roots"].append(root)
        projection = project_inventory(self.inventory, self.version, encode(config), deadline=time.monotonic() + 2)
        cache = storage / ("cache-" + projection.config_digest() + f"-{stale_offset}.json")
        if publish:
            stored = store_projection(storage, projection)
            admission = guarded_admission(self.inventory, projection, SKILLS_REVISION, physical=self.physical)
            when = (datetime.now(timezone.utc) - timedelta(seconds=stale_offset)).isoformat()
            with patch("lib.engine.now", return_value=when):
                refresh_admitted(str(stored.config), str(cache), admission)
        return SkillsDiscovery(projection, cache, SKILLS_REVISION)

    def test_primary_and_linked_keep_the_local_and_hosted_heads_separate(self):
        # Pack the primary branch to exercise fallback; a detached linked HEAD
        # still comes directly from the linked checkout's own Git directory.
        git(self.primary.checkout, "pack-refs", "--all")
        dirty = Path(self.primary.checkout) / "local.txt"
        dirty.write_text("local work", encoding="utf-8")
        before = self.inventory.snapshot()
        primary = self.select(self.primary.checkout)
        linked = self.select(self.linked.checkout, thread="fixture-thread")
        for result in (primary, linked):
            self.assertEqual(result.local_head, self.local_head)
            self.assertEqual(result.hosted_default_head, HOSTED_HEAD)
            self.assertEqual(result.hosted_default_branch, "main")
            self.assertEqual(result.repository_id, 73)
            self.assertTrue(result.private)
            self.assertEqual(len(result.warnings), 1)
        self.assertNotEqual(primary.git_directory, linked.git_directory)
        self.assertEqual(primary.common_directory, linked.common_directory)
        self.assertEqual(before, self.inventory.snapshot())
        self.assertEqual(dirty.read_text(encoding="utf-8"), "local work")

    def test_unknown_excluded_and_noncanonical_paths_get_zero_target_probes(self):
        native = Mock(side_effect=self.native)
        hosting = Mock(side_effect=self.hosting)
        for path in (self.denied, self.denied + "/child", str(self.root / "unregistered"),
                     self.primary.checkout + "/../sample.product", "sample.product"):
            with self.subTest(path=path), patch("repo_platform.adoption.pin_checkout") as probe:
                with self.assertRaises(Refused):
                    self.select(path, read_native=native, read_hosting=hosting)
                probe.assert_not_called()
        native.assert_not_called()
        hosting.assert_not_called()
        with patch.object(adoption, "pin_checkout", wraps=adoption.pin_checkout) as probe:
            self.select(self.primary.checkout, read_native=native, read_hosting=hosting)
            probe.assert_called_once()
        self.assertEqual(native.call_count, 2)
        hosting.assert_called_once()

    def test_invoked_provider_cannot_substitute_another_repository(self):
        def different_provider(repository_id, *, deadline):
            return replace(self.hosting(repository_id, deadline=deadline), repository_id=74)

        with patch("repo_platform.adoption.pin_checkout") as probe:
            with self.assertRaisesRegex(Refused, "hosting identity"):
                self.select(self.primary.checkout, read_hosting=different_provider)
            probe.assert_not_called()

    def test_lifecycle_reservation_after_native_read_stops_target_probe(self):
        def reserve_retirement(repository_id, *, deadline):
            self.inventory.reserve(self.version, "retirement", {self.primary.checkout: None}, deadline=deadline)
            return self.hosting(repository_id, deadline=deadline)

        with patch("repo_platform.adoption.pin_checkout") as probe:
            with self.assertRaisesRegex(Refused, "Dev inventory changed"):
                self.select(self.primary.checkout, read_hosting=reserve_retirement)
            with self.assertRaisesRegex(Refused, "lifecycle change"):
                self.select(self.primary.checkout)
            probe.assert_not_called()

    def test_changed_push_endpoint_is_refused_even_when_fetch_still_matches(self):
        git(self.primary.checkout, "remote", "set-url", "--push", "origin",
            "https://github.com/example/different.product.git")
        with self.assertRaisesRegex(Refused, "configuration changed"):
            self.select(self.primary.checkout)

    def test_freshness_accepts_the_24_hour_boundary_and_refuses_other_ages(self):
        now = int(time.time())
        for age in (0, 86400, 86401, -1):
            def dated_host(repository_id, *, deadline):
                return replace(self.hosting(repository_id, deadline=deadline), identity_observed_at=now - age)

            with self.subTest(age=age), patch("repo_platform.adoption.time.time", return_value=now):
                if 0 <= age <= 86400:
                    self.assertEqual(self.select(self.primary.checkout, read_hosting=dated_host).repository_id, 73)
                else:
                    with self.assertRaisesRegex(Refused, "fresh"):
                        self.select(self.primary.checkout, read_hosting=dated_host)

    def test_native_recheck_rejects_reassignment_before_access(self):
        initial = self.native("fixture-project", self.primary.checkout, deadline=time.monotonic() + 3)
        moved = replace(initial, checkout=str(self.root / "moved"))
        samples = Mock(side_effect=[initial, moved])
        with patch("repo_platform.adoption.pin_checkout") as probe:
            with self.assertRaisesRegex(Refused, "native project no longer"):
                self.select(self.primary.checkout, read_native=samples)
            probe.assert_not_called()
        self.assertEqual(samples.call_count, 2)

    def test_native_recheck_accepts_a_change_to_the_display_title(self):
        initial = self.native("fixture-project", self.primary.checkout, deadline=time.monotonic() + 3)
        renamed = replace(initial, title="Sample Product")
        result = self.select(self.primary.checkout, read_native=Mock(side_effect=[initial, renamed]))
        self.assertEqual(result.project_title, "Sample Product")
        self.assertEqual(result.native_selection_digest, initial.selection_digest)
        self.assertEqual(result.warnings, ("Codex project display title changed during the preview.",))

    def test_explicit_command_prints_private_owner_handoff_and_preserves_prior_result(self):
        def acquire_native(project_id, checkout, *, thread_id, deadline):
            native = self.native(project_id, checkout, deadline=deadline)
            self.assertEqual(native.thread_id, thread_id)
            return native

        def classify(hosted, *, deadline):
            return adoption.DeliveryClassification(
                hosted.repository_id, hosted.full_name, "private-no-ci", "fixture-policy-revision"
            )

        owner = command.OwnerSession(self.inventory, "proof", self.physical,
                                     acquire_native, self.hosting, classify, self.index())
        args = ["adopt", self.linked.checkout, "--project-id", "fixture-project",
                "--thread-id", "fixture-thread", "--repository-id"]
        output, errors = StringIO(), StringIO()
        self.assertEqual(command.run([*args, "73"], owner=owner, output=output, errors=errors), 0)
        prior = output.getvalue()
        result = json.loads(prior)
        self.assertEqual(result["owner_handoff"], "private-repository-owner")
        self.assertEqual(result["delivery"]["disposition"], "private-no-ci")
        self.assertEqual(result["checkout"], self.linked.checkout)
        self.assertEqual(result["discovery"]["skills_revision"], SKILLS_REVISION)
        self.assertEqual(len(result["discovery"]["observations"]), 5)
        self.assertTrue(all(row["state"] == "current" for row in result["discovery"]["observations"]))
        self.assertEqual(errors.getvalue(), "")
        with patch("repo_platform.adoption.pin_checkout") as probe:
            self.assertEqual(command.run([*args, "74"], owner=owner, output=output, errors=errors), 1)
            probe.assert_not_called()
        self.assertEqual(output.getvalue(), prior)
        self.assertIn("requested repository ID differs", errors.getvalue())

        with patch("repo_platform.adoption.pin_checkout") as probe:
            forbidden = replace(owner, read_delivery=lambda hosted, **_: replace(
                classify(hosted, deadline=0), disposition="retired"
            ))
            self.assertEqual(command.run([*args, "73"], owner=forbidden, output=output, errors=errors), 1)
            probe.assert_not_called()
        self.assertEqual(output.getvalue(), prior)

    def test_index_joins_one_repository_and_clone_with_two_worktrees(self):
        index = self.index(optional_missing=True)
        primary = self.select(self.primary.checkout, discovery=index).discovery
        linked = self.select(self.linked.checkout, thread="fixture-thread", discovery=index).discovery
        self.assertEqual(primary.repository_entity, linked.repository_entity)
        self.assertEqual(primary.clone_entity, linked.clone_entity)
        self.assertNotEqual(primary.worktree_entity, linked.worktree_entity)
        self.assertEqual(primary.snapshot_digest, linked.snapshot_digest)
        self.assertEqual(primary.coverage, "partial")
        self.assertEqual(len(primary.warnings), 1)

    def test_index_refuses_missing_stale_future_and_related_conflict_before_target_access(self):
        for options, reason in (
            ({"publish": False}, "index receipt"),
            ({"stale_offset": 86401}, "stale or future"),
            ({"stale_offset": -300}, "stale or future"),
            ({"bad_origin": True}, "unresolved input"),
            ({"conflicting_alias": True}, "conflicting claim"),
        ):
            index = self.index(**options)
            with self.subTest(options=options), patch("repo_platform.adoption.pin_checkout") as probe:
                with self.assertRaisesRegex(Refused, reason):
                    self.select(self.primary.checkout, discovery=index)
                probe.assert_not_called()

    def test_index_refuses_a_prior_inventory_generation_before_target_access(self):
        index = self.index()
        policy, generation, digest = index.projection.version
        prior = replace(index.projection, version=(policy, generation - 1, digest))
        with patch("repo_platform.adoption.pin_checkout") as probe:
            with self.assertRaisesRegex(Refused, "older Dev binding"):
                self.select(self.primary.checkout, discovery=replace(index, projection=prior))
            probe.assert_not_called()
