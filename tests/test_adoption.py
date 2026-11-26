from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import DEFAULT, Mock, patch

from github_checkout_provider import GitHubRepositoryObservation, TransientProviderRead
from issue_checkout_evidence import issue_checkout_evidence_reissue, observe_checkout_authorities
from lib.contracts import Refusal as SkillsRefusal, encode
from lib.engine import git_records, refresh_admitted
from native_checkout_project import NativeProjectBinding
from personal_checkout_inventory import Binding, Inventory, Refused
from physical_checkout_binding import OwnerPhysicalBinding, OwnerPhysicalBindings, ROLES
from repo_platform import adoption, command, host
from repo_platform.adoption_results import AdoptionResults
from repo_platform.desktop import DesktopReader
from repo_platform.discovery import SkillsDiscovery
from repo_platform.recovery import TransientNativeRead
from repo_platform.skills_repair import SkillsRepair
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

    def index_settings(self, *, bad_origin=False, optional_missing=False, conflicting_alias=False):
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
        return encode(config)

    def index(self, *, stale_offset=0, bad_origin=False, optional_missing=False,
              conflicting_alias=False, publish=True):
        storage = self.root / "authority"
        trusted_settings = self.index_settings(bad_origin=bad_origin, optional_missing=optional_missing,
                                               conflicting_alias=conflicting_alias)
        projection = project_inventory(self.inventory, self.version, trusted_settings, deadline=time.monotonic() + 2)
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

    def test_same_name_fork_and_shallow_clone_keep_their_own_checkout_bindings(self):
        upstream = Path(self.primary.checkout)
        peer = Path(self.peer.checkout)
        for checkout in (upstream, peer):
            git(checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "--allow-empty", "-m", "Commit " + checkout.parent.name)
        shallow, fork = (self.root / kind / upstream.name for kind in ("shallow", "fork"))
        for path, options, remote in (
            (shallow, ("--depth=1",), "example/sample.product"),
            (fork, (), "fork-owner/sample.product"),
        ):
            path.parent.mkdir()
            git(self.root, "clone", "--no-local", *options, str(upstream), str(path))
            git(path, "remote", "set-url", "origin", "https://github.com/" + remote + ".git")
        self.assertEqual(git(upstream, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual(git(fork, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual(git(shallow, "rev-parse", "--is-shallow-repository"), "true")
        self.assertEqual(git(shallow, "rev-list", "--count", "HEAD"), "1")
        shared_head = git(upstream, "rev-parse", "HEAD")
        self.assertEqual(git(fork, "rev-parse", "HEAD"), shared_head)
        self.assertEqual(git(shallow, "rev-parse", "HEAD"), shared_head)
        self.assertNotEqual(git(peer, "rev-parse", "HEAD"), shared_head)

        names = {73: "example/sample.product", 74: "other/sample.product", 75: "fork-owner/sample.product"}
        titles = {str(path): upstream.name for path in (upstream, peer, shallow, fork)}
        titles[str(shallow)] = "Sample product shallow checkout"
        bindings = {}
        proofs = []
        roots = []
        for key, path, repository_id in (("upstream", upstream, 73), ("peer", peer, 74),
                                          ("shallow", shallow, 73), ("fork", fork, 75)):
            gitdir = str(path / ".git")
            binding = Binding(str(path), gitdir, gitdir, "github", repository_id, key, key, ("adopt", "enumerate"))
            bindings[str(path)] = binding
            locations = []
            for role in ROLES:
                value = getattr(binding, role)
                stat = Path(value).lstat()
                locations.append((role, value, stat.st_dev, stat.st_ino))
            proofs.append(OwnerPhysicalBinding("github", repository_id, key, key, tuple(locations)))
            roots.append({"key": key, "path": str(path), "scope": "permitted", "inventory": "native",
                          "title": titles[str(path)], "aliases": [], "storage_fields": ["title"], "sources": [],
                          "identity": {"provider": None, "remote": "https://github.com/" + names[repository_id]},
                          "git": {"common_dir": gitdir, "git_dir": gitdir}})

        storage = self.root / "shared-history-authority"
        storage.mkdir(mode=0o700)
        inventory = Inventory(storage)
        generation = inventory.initialize("fixture-host", "fixture-policy", [self.denied], bindings.values())
        physical = OwnerPhysicalBindings(tuple(proofs))
        selections = {path: "fixture-" + binding.worktree_id for path, binding in bindings.items()}
        operation = "qualify-shared-history"

        def read_native(project_id, checkout, *, deadline, thread_id=None):
            self.assertEqual(selections[checkout], project_id)
            self.assertIsNone(thread_id)
            return NativeProjectBinding(project_id, checkout, titles[checkout], None, int(time.time()), thread_id)

        def read_hosting(repository_id, *, deadline):
            return replace(self.hosting(repository_id, deadline=deadline), full_name=names[repository_id])

        deadline = time.monotonic() + 10
        observations = observe_checkout_authorities(
            selections, repository_ids={path: binding.repository_id for path, binding in bindings.items()},
            read_native=read_native, read_hosting=read_hosting, deadline=deadline,
        )
        version = issue_checkout_evidence_reissue(
            inventory, generation, operation, selections, observations, physical=physical, deadline=deadline,
        )
        settings = json.loads(self.index_settings())
        settings["roots"] = roots
        settings["bounds"]["roots"] = len(roots)
        projection = project_inventory(inventory, version, encode(settings), deadline=deadline)
        stored = store_projection(storage, projection)
        cache = storage / "index.json"
        refresh_admitted(str(stored.config), str(cache),
                         guarded_admission(inventory, projection, SKILLS_REVISION, physical=physical))
        owner = command.OwnerSession(
            inventory, operation, physical, read_native, read_hosting,
            lambda hosted, **_: adoption.DeliveryClassification(
                hosted.repository_id, hosted.full_name, "private-no-ci", "fixture-policy",
            ),
            SkillsDiscovery(projection, cache, SKILLS_REVISION), AdoptionResults(storage),
        )

        accepted = {}
        for path in (upstream, peer, shallow, fork):
            args = ["adopt", str(path), "--project-id", selections[str(path)]]
            output, errors = StringIO(), StringIO()
            self.assertEqual(command.run(args, owner=owner, output=output, errors=errors), 0, errors.getvalue())
            result = json.loads(output.getvalue())
            self.assertEqual(result["repository_id"], bindings[str(path)].repository_id)
            self.assertEqual(result["project_title"], titles[str(path)])
            self.assertEqual(result["local_head"], git(path, "rev-parse", "HEAD"))
            self.assertEqual(result["hosted_default_head"], HOSTED_HEAD)
            title_warnings = [warning for warning in result["warnings"] if "differing display titles" in warning]
            self.assertEqual(len(title_warnings), int(path in (upstream, shallow)))
            accepted[path] = result["discovery"]
        self.assertEqual(accepted[upstream]["repository_entity"], accepted[shallow]["repository_entity"])
        self.assertEqual(len({entry["repository_entity"] for entry in accepted.values()}), 3)
        for entity in ("clone_entity", "worktree_entity"):
            self.assertEqual(len({entry[entity] for entry in accepted.values()}), 4)

        previous = {path.name: path.read_bytes() for path in storage.glob("adoption-*.json")}
        self.assertEqual(len(previous), 4)
        substitute = Mock(side_effect=lambda _, **kwargs: read_hosting(73, **kwargs))
        errors = StringIO()
        args = ["adopt", str(fork), "--project-id", selections[str(fork)], "--repository-id", "75"]
        with patch("repo_platform.adoption.pin_checkout") as probe:
            self.assertEqual(command.run(args, owner=replace(owner, read_hosting=substitute),
                                         output=StringIO(), errors=errors), 1)
            probe.assert_not_called()
        substitute.assert_called_once()
        self.assertIn("hosting identity", errors.getvalue())
        self.assertEqual({path.name: path.read_bytes() for path in storage.glob("adoption-*.json")}, previous)

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

    def command_owner(self):
        def acquire_native(project_id, checkout, *, thread_id, deadline):
            native = self.native(project_id, checkout, deadline=deadline)
            self.assertEqual(native.thread_id, thread_id)
            return native

        def classify(hosted, *, deadline):
            return adoption.DeliveryClassification(
                hosted.repository_id, hosted.full_name, "private-no-ci", "fixture-policy-revision"
            )

        storage = self.root / "accepted"
        storage.mkdir(mode=0o700, exist_ok=True)
        return command.OwnerSession(self.inventory, "proof", self.physical, acquire_native,
                                    self.hosting, classify, self.index(), AdoptionResults(storage))

    def test_read_only_preview_uses_live_ownership_without_recording_an_adoption(self):
        checkout = self.linked.checkout
        args = [checkout, "--project-id", "fixture-project", "--thread-id", "fixture-thread",
                "--repository-id", "73", "--expected-title", "A different display title"]
        owner = self.command_owner()
        self.assertEqual(command.run(["adopt", *args], owner=owner, output=StringIO(), errors=StringIO()), 0)
        prior_results = {file.name: file.read_bytes() for file in owner.results.path.iterdir()}
        prior_snapshot = owner.discovery.cache.read_bytes()
        prior_inventory = self.inventory.snapshot()
        gitfile = Path(checkout) / ".git"
        prior_gitfile = gitfile.read_bytes()
        native = Mock(wraps=owner.read_native)
        hosting = Mock(wraps=owner.read_hosting)
        delivery = Mock(wraps=owner.read_delivery)
        owner = replace(owner, read_native=native, read_hosting=hosting, read_delivery=delivery)
        output, errors = StringIO(), StringIO()
        with patch("repo_platform.command.RecoveryCase", side_effect=AssertionError("write recovery")), patch.object(
            AdoptionResults, "publish", side_effect=AssertionError("write adoption")
        ):
            self.assertEqual(command.run(["preview", *args], owner=owner, output=output, errors=errors),
                             0, errors.getvalue())
        observed = json.loads(output.getvalue())
        self.assertEqual(observed["repository_id"], 73)
        self.assertEqual(observed["checkout"], checkout)
        self.assertEqual(observed["local_head"], self.local_head)
        self.assertEqual(observed["hosted_default_head"], HOSTED_HEAD)
        self.assertEqual(observed["owner_handoff"], "private-repository-owner")
        self.assertIn("Codex project display title differs from the selected title.", observed["warnings"])
        self.assertEqual((native.call_count, hosting.call_count, delivery.call_count), (2, 1, 2))
        self.assertEqual(self.inventory.snapshot(), prior_inventory)
        self.assertEqual(owner.discovery.cache.read_bytes(), prior_snapshot)
        self.assertEqual({file.name: file.read_bytes() for file in owner.results.path.iterdir()}, prior_results)
        self.assertEqual(gitfile.read_bytes(), prior_gitfile)

    def test_explicit_command_prints_private_owner_handoff_and_preserves_prior_result(self):
        owner = self.command_owner()
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
        stored, = owner.results.path.glob("adoption-*.json")
        accepted = stored.read_bytes()
        self.assertEqual(json.loads(accepted)["result"], result)
        with patch("repo_platform.adoption.pin_checkout") as probe:
            self.assertEqual(command.run([*args, "74"], owner=owner, output=output, errors=errors), 1)
            probe.assert_not_called()
        self.assertEqual(output.getvalue(), prior)
        self.assertIn("requested repository ID differs", errors.getvalue())

        with patch("repo_platform.adoption.pin_checkout") as probe:
            forbidden = replace(owner, read_delivery=lambda hosted, **_: replace(
                owner.read_delivery(hosted, deadline=0), disposition="retired"
            ))
            self.assertEqual(command.run([*args, "73"], owner=forbidden, output=output, errors=errors), 1)
            probe.assert_not_called()
        self.assertEqual(output.getvalue(), prior)
        self.assertEqual(stored.read_bytes(), accepted)

    def test_racing_command_cannot_overwrite_a_result_published_since_it_started(self):
        owner = self.command_owner()
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        output, errors = StringIO(), StringIO()
        winner = []

        def publish_first(repository_id, *, deadline):
            winner.append(adoption.preview(
                owner.inventory, self.primary.checkout, "fixture-project", owner.operation,
                physical=owner.physical, read_native=self.native, read_hosting=owner.read_hosting,
                read_delivery=owner.read_delivery, deadline=deadline, discovery=owner.discovery,
                results=owner.results, expected_title="Earlier publication",
            ))
            return self.hosting(repository_id, deadline=deadline)

        slower = replace(owner, read_hosting=publish_first)
        self.assertEqual(command.run(args, owner=slower, output=output, errors=errors), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("another command published", errors.getvalue())
        stored, = owner.results.path.glob("adoption-*.json")
        self.assertEqual(json.loads(stored.read_bytes())["result"]["warnings"], list(winner[0].warnings))

    def test_changed_delivery_policy_preserves_the_previous_adoption(self):
        owner = self.command_owner()
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        result, errors = StringIO(), StringIO()
        self.assertEqual(command.run(args, owner=owner, output=result, errors=errors), 0)
        accepted, = owner.results.path.glob("adoption-*.json")
        previous = accepted.read_bytes()
        stale = owner.read_delivery(self.hosting(73, deadline=0), deadline=0)
        changed = replace(stale, policy_revision="new-policy")
        reader = Mock(side_effect=[stale, changed])
        output, errors = StringIO(), StringIO()
        status = command.run(args, owner=replace(owner, read_delivery=reader), output=output, errors=errors)
        self.assertEqual(status, 1)
        self.assertEqual(reader.call_count, 2)
        self.assertIn("delivery policy changed", errors.getvalue())
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(accepted.read_bytes(), previous)

    def test_public_delivery_routes_refuse_a_private_hosted_repository(self):
        owner = self.command_owner()
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        for disposition in ("actions-app", "native-github"):
            with self.subTest(disposition=disposition):
                observed = owner.read_delivery(self.hosting(73, deadline=0), deadline=0)
                reader = Mock(return_value=replace(observed, disposition=disposition))
                output, errors = StringIO(), StringIO()
                with patch.object(adoption, "pin_checkout", wraps=adoption.pin_checkout) as probe:
                    status = command.run(args, owner=replace(owner, read_delivery=reader), output=output, errors=errors)
                    probe.assert_not_called()
                self.assertEqual(status, 1)
                self.assertIn("visibility conflicts", errors.getvalue())
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(list(owner.results.path.glob("adoption-*.json")), [])

    def test_retirement_during_recheck_preserves_the_accepted_disk_result(self):
        owner = self.command_owner()
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        output, errors = StringIO(), StringIO()
        self.assertEqual(command.run(args, owner=owner, output=output, errors=errors), 0)
        stored, = owner.results.path.glob("adoption-*.json")
        accepted = stored.read_bytes()

        def retire(repository_id, *, deadline):
            self.inventory.reserve(self.version, "retirement", {self.primary.checkout: None}, deadline=deadline)
            return self.hosting(repository_id, deadline=deadline)

        output = StringIO()
        with patch("repo_platform.adoption.pin_checkout") as probe:
            self.assertEqual(command.run(args, owner=replace(owner, read_hosting=retire),
                                         output=output, errors=errors), 1)
            self.assertEqual(command.run(args, owner=owner, output=output, errors=errors), 1)
            probe.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Dev inventory changed", errors.getvalue())
        self.assertIn("lifecycle change", errors.getvalue())
        self.assertEqual(stored.read_bytes(), accepted)

    def test_interrupted_publication_releases_lock_and_preserves_prior_result(self):
        owner = self.command_owner()
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        errors = StringIO()
        self.assertEqual(command.run(args, owner=owner, output=StringIO(), errors=errors), 0)
        stored, = owner.results.path.glob("adoption-*.json")
        accepted = stored.read_bytes()
        child = subprocess.run(
            [sys.executable, "-c", """
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
from repo_platform.adoption_results import AdoptionResults

@dataclass(frozen=True)
class Result:
    native_selection_digest: str
    interrupted: bool = True

store = AdoptionResults(Path(sys.argv[1]))
selection = sys.argv[2]
with patch("repo_platform.adoption_results.os.replace", side_effect=lambda *a, **kw: os._exit(79)):
    store.publish(Result(selection), store.reference(selection), expires=time.monotonic() + 2)
""", str(owner.results.path), json.loads(accepted)["result"]["native_selection_digest"]],
            check=False, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(child.returncode, 79, child.stderr)
        self.assertEqual(stored.read_bytes(), accepted)
        self.assertEqual(len(list(owner.results.path.glob(".*.pending"))), 1)
        restarted = replace(owner, results=AdoptionResults(owner.results.path),
                            inventory=Inventory(owner.inventory.directory))
        output = StringIO()
        self.assertEqual(command.run(args, owner=restarted, output=output, errors=errors), 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(list(owner.results.path.glob(".*.pending")), [])
        self.assertEqual(json.loads(stored.read_bytes())["result"], json.loads(output.getvalue()))

    def test_transient_hosting_retry_rechecks_native_selection_and_clears_recovery_case(self):
        owner = self.command_owner()
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        host = Mock(side_effect=[TransientProviderRead("fixture transport timeout"),
                                 self.hosting(73, deadline=time.monotonic() + 2)])
        native = Mock(side_effect=owner.read_native)
        owner = replace(owner, read_hosting=host, read_native=native)
        output, errors = StringIO(), StringIO()
        clock = [100.0]
        with patch("repo_platform.recovery.time", side_effect=lambda: clock[0]), patch(
            "repo_platform.recovery.sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)
        ) as wait:
            self.assertEqual(command.run(args, owner=owner, output=output, errors=errors), 0)
        wait.assert_called_once_with(2)
        self.assertEqual(native.call_count, 3)
        self.assertEqual(host.call_count, 2)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["repository_id"], 73)
        self.assertEqual(list(owner.results.path.glob("recovery-*.json")), [])

    def test_exhausted_case_uses_two_delays_and_refuses_restart_without_fresh_evidence(self):
        owner = self.command_owner()
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        host = Mock(side_effect=TransientProviderRead("fixture transport timeout"))
        owner = replace(owner, read_hosting=host)
        clock = [100.0]
        errors = StringIO()
        with patch("repo_platform.recovery.time", side_effect=lambda: clock[0]), patch(
            "repo_platform.recovery.sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)
        ) as wait:
            self.assertEqual(command.run(args, owner=owner, output=StringIO(), errors=errors), 1)
            reopened = replace(owner, inventory=Inventory(owner.inventory.directory),
                               results=AdoptionResults(owner.results.path))
            self.assertEqual(command.run(args, owner=reopened, output=StringIO(), errors=errors), 1)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [2, 5])
        self.assertEqual(host.call_count, 3)
        self.assertIn("Codex diagnosis required after 3 attempt(s)", errors.getvalue())
        case, = owner.results.path.glob("recovery-*.json")
        case_record = json.loads(case.read_bytes())
        self.assertEqual(case_record["attempts"], 3)
        self.assertEqual(case_record["elapsed_ms"], 7000)
        self.assertEqual(case_record["state"], "diagnose")
        advanced = replace(owner, read_hosting=self.hosting, discovery=self.index(optional_missing=True))
        advanced_errors = StringIO()
        self.assertEqual(command.run(args, owner=advanced, output=StringIO(), errors=advanced_errors),
                         0, advanced_errors.getvalue())
        self.assertFalse(case.exists())

    def test_read_only_preview_retries_transient_reads_without_a_recovery_record(self):
        owner = self.command_owner()
        observation = self.native("fixture-project", self.primary.checkout, deadline=time.monotonic() + 2)
        native = Mock(side_effect=[TransientNativeRead("native fixture unavailable"), observation, observation])
        args = ["preview", self.primary.checkout, "--project-id", "fixture-project"]
        output, errors = StringIO(), StringIO()
        with patch("repo_platform.recovery.sleep") as wait:
            self.assertEqual(command.run(args, owner=replace(owner, read_native=native),
                                         output=output, errors=errors), 0, errors.getvalue())
        wait.assert_called_once_with(2)
        self.assertEqual(native.call_count, 3)
        self.assertTrue(output.getvalue())
        self.assertEqual(list(owner.results.path.iterdir()), [])

    def test_read_only_preview_leaves_stale_discovery_for_the_adoption_owner(self):
        index = self.index(stale_offset=86401)
        owner = replace(self.command_owner(), discovery=index,
                        skills_repair=SkillsRepair(self.index_settings(), self.root / "authority"))
        args = ["preview", self.primary.checkout, "--project-id", "fixture-project"]
        before = index.cache.read_bytes()
        output, errors = StringIO(), StringIO()
        with patch.object(SkillsRepair, "refresh") as refresh, patch("repo_platform.adoption.pin_checkout") as probe:
            self.assertEqual(command.run(args, owner=owner, output=output, errors=errors), 1)
            refresh.assert_not_called()
            probe.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Run adopt to request a refresh through the Skills owner", errors.getvalue())
        self.assertEqual(list(owner.results.path.iterdir()), [])
        self.assertEqual(index.cache.read_bytes(), before)

    def test_stale_skills_snapshot_goes_straight_to_owner_diagnosis(self):
        owner = replace(self.command_owner(), discovery=self.index(stale_offset=86401))
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        errors = StringIO()
        with patch("repo_platform.recovery.sleep") as wait, patch("repo_platform.adoption.pin_checkout") as probe:
            self.assertEqual(command.run(args, owner=owner, output=StringIO(), errors=errors), 1)
            wait.assert_not_called()
            probe.assert_not_called()
        case, = owner.results.path.glob("recovery-*.json")
        record = json.loads(case.read_bytes())
        self.assertEqual((record["attempts"], record["kind"]), (1, "skills-refresh"))
        self.assertIn("Skills refresh owner", errors.getvalue())
        hosted = Mock(side_effect=owner.read_hosting)
        owner = replace(owner, read_hosting=hosted)
        self.assertEqual(command.run(args, owner=owner, output=StringIO(), errors=StringIO()), 1)
        hosted.assert_not_called()
        stored = store_projection(self.root / "authority", owner.discovery.projection)
        admission = guarded_admission(self.inventory, owner.discovery.projection, SKILLS_REVISION,
                                     physical=self.physical)
        refreshed = refresh_admitted(str(stored.config), str(owner.discovery.cache), admission)
        self.assertNotEqual(refreshed["result"]["snapshot_digest"], record["failed_snapshot"])
        self.assertEqual(command.run(args, owner=owner, output=StringIO(), errors=StringIO()), 0)
        hosted.assert_called_once()
        self.assertFalse(case.exists())

    def test_elapsed_budget_stops_before_a_retry(self):
        owner = self.command_owner()
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        clock = [100.0]

        def slow_transport(repository_id, *, deadline):
            clock[0] += 89
            raise TransientProviderRead("fixture transport timeout")

        host = Mock(side_effect=slow_transport)
        errors = StringIO()
        with patch("repo_platform.recovery.time", side_effect=lambda: clock[0]), patch(
            "repo_platform.recovery.sleep"
        ) as wait:
            self.assertEqual(command.run(args, owner=replace(owner, read_hosting=host),
                                         output=StringIO(), errors=errors), 1)
        wait.assert_not_called()
        host.assert_called_once()
        self.assertIn("Codex diagnosis required after 1 attempt(s)", errors.getvalue())

    def test_owner_refresh_repairs_stale_skills_and_reruns_full_adoption(self):
        index = self.index(stale_offset=86401)
        repair = SkillsRepair(self.index_settings(), self.root / "authority")
        owner = replace(self.command_owner(), discovery=index, skills_repair=repair)
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        host = Mock(side_effect=owner.read_hosting)
        native = Mock(side_effect=owner.read_native)
        owner = replace(owner, read_hosting=host, read_native=native)
        old = index.cache.read_bytes()
        output, errors = StringIO(), StringIO()
        self.assertEqual(command.run(args, owner=owner, output=output, errors=errors), 0, errors.getvalue())
        self.assertEqual(host.call_count, 2)
        self.assertEqual(native.call_count, 4)
        self.assertEqual(json.loads(output.getvalue())["discovery"]["snapshot_generation"], 2)
        self.assertNotEqual(index.cache.read_bytes(), old)
        self.assertEqual(list(owner.results.path.glob("recovery-*.json")), [])

    def test_owner_refresh_recovers_an_old_required_observation_in_a_fresh_snapshot(self):
        required = (
            ("location", "inventory/primary/git/common/config"),
            ("location", "inventory/primary/git/worktree"),
            ("identity", "inventory/primary/git/common/config"),
        )
        for number, (field, pointer) in enumerate(required):
            with self.subTest(field=field, pointer=pointer):
                unconfigured = self.root / f"without-refresh-{number}"
                configured = self.root / f"with-refresh-{number}"
                for slot in (unconfigured, configured):
                    slot.mkdir(mode=0o700)
                selection = replace(self.command_owner(), results=AdoptionResults(unconfigured))
                args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
                previous = StringIO()
                self.assertEqual(command.run(args, owner=selection, output=previous, errors=StringIO()), 0)
                accepted, = selection.results.path.glob("adoption-*.json")
                original_result = accepted.read_bytes()

                def record_old_observation(*args, **kwargs):
                    digest, entities, reused = git_records(*args, **kwargs)
                    old = (datetime.now(timezone.utc) - timedelta(seconds=86401)).isoformat()
                    for entity in entities:
                        if field == "location" and entity["kind"] not in {"clone", "worktree"}:
                            continue
                        for item in entity["observations"]:
                            if (item["field"], item["pointer"]) == (field, pointer):
                                item["observed_at"] = old
                    return digest, entities, reused

                with patch("lib.engine.git_records", side_effect=record_old_observation):
                    index = self.index()
                snapshot = json.loads(index.cache.read_bytes())
                generation = snapshot["generation"]
                now = datetime.now(timezone.utc)
                self.assertLess((now - datetime.fromisoformat(snapshot["observed_at"])).total_seconds(), 60)
                expired = [item for entity in snapshot["entities"] for item in entity["observations"]
                           if (item["field"], item["pointer"]) == (field, pointer)
                           and (now - datetime.fromisoformat(item["observed_at"])).total_seconds() > 86400]
                self.assertEqual(len(expired), 1)

                with patch("repo_platform.adoption.pin_checkout") as probe:
                    output, errors = StringIO(), StringIO()
                    self.assertEqual(command.run(args, owner=replace(selection, discovery=index),
                                                 output=output, errors=errors), 1)
                    probe.assert_not_called()
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(accepted.read_bytes(), original_result)
                self.assertIn("Check current evidence through the Skills refresh owner", errors.getvalue())
                recovery, = selection.results.path.glob("recovery-*.json")
                case = json.loads(recovery.read_bytes())
                self.assertEqual(case["kind"], "skills-refresh")
                self.assertTrue(case["failed_snapshot"])

                owner = replace(selection, discovery=index, results=AdoptionResults(configured),
                                skills_repair=SkillsRepair(self.index_settings(), self.root / "authority"))
                host_reader = Mock(wraps=owner.read_hosting)
                native_reader = Mock(wraps=owner.read_native)
                owner = replace(owner, read_hosting=host_reader, read_native=native_reader)
                output, errors = StringIO(), StringIO()
                with patch("repo_platform.skills_repair.refresh_admitted", wraps=refresh_admitted) as refresh:
                    self.assertEqual(command.run(args, owner=owner, output=output, errors=errors), 0,
                                     errors.getvalue())
                    refresh.assert_called_once()
                self.assertEqual(host_reader.call_count, 2)
                self.assertEqual(native_reader.call_count, 4)
                result = json.loads(output.getvalue())["discovery"]
                self.assertEqual(result["snapshot_generation"], generation + 1)
                self.assertNotEqual(result["snapshot_digest"], case["failed_snapshot"])
                self.assertFalse(list(owner.results.path.glob("recovery-*.json")))

    def test_owner_refresh_recovers_a_failed_required_collector(self):
        with patch("lib.engine.git_records", side_effect=SkillsRefusal("source_failed", 3)):
            index = self.index()
        old = index.cache.read_bytes()
        owner = replace(self.command_owner(), discovery=index,
                        skills_repair=SkillsRepair(self.index_settings(), self.root / "authority"))
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        output, errors = StringIO(), StringIO()
        self.assertEqual(command.run(args, owner=owner, output=output, errors=errors), 0, errors.getvalue())
        observed = json.loads(output.getvalue())["discovery"]
        self.assertEqual(observed["coverage"], "complete")
        self.assertEqual(observed["snapshot_generation"], 2)
        self.assertNotEqual(index.cache.read_bytes(), old)

    def test_refused_repair_preserves_prior_result_and_is_not_repeated_on_restart(self):
        owner = replace(self.command_owner(), skills_repair=SkillsRepair(self.index_settings(),
                                                                        self.root / "authority"))
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        self.assertEqual(command.run(args, owner=owner, output=StringIO(), errors=StringIO()), 0)
        stored, = owner.results.path.glob("adoption-*.json")
        accepted = stored.read_bytes()
        corrupt = b"partial snapshot fixture\n"
        with owner.discovery.cache.open("wb") as file:
            file.write(corrupt)
        errors, output = StringIO(), StringIO()
        with patch("repo_platform.skills_repair.refresh_admitted", wraps=refresh_admitted) as refresh:
            self.assertEqual(command.run(args, owner=owner, output=output, errors=errors), 1)
            reopened = replace(owner, inventory=Inventory(self.inventory.directory),
                               results=AdoptionResults(owner.results.path))
            self.assertEqual(command.run(args, owner=reopened, output=output, errors=errors), 1)
        refresh.assert_called_once()
        self.assertEqual(output.getvalue(), "")
        self.assertIn("the owner-supported Skills refresh refused (unavailable)", errors.getvalue())
        self.assertEqual(stored.read_bytes(), accepted)
        self.assertEqual(owner.discovery.cache.read_bytes(), corrupt)
        case, = owner.results.path.glob("recovery-*.json")
        self.assertTrue(json.loads(case.read_bytes())["repair_attempted"])

    def test_retirement_before_skills_repair_stops_all_additional_target_access(self):
        repair = SkillsRepair(self.index_settings(), self.root / "authority")
        owner = replace(self.command_owner(), discovery=self.index(stale_offset=86401), skills_repair=repair)
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        real_refresh = repair.refresh

        def reserve_before_refresh(*args, **kwargs):
            self.inventory.reserve(self.version, "retirement", {self.primary.checkout: None},
                                   deadline=time.monotonic() + 2)
            return real_refresh(*args, **kwargs)

        output, errors = StringIO(), StringIO()
        with patch.object(SkillsRepair, "refresh", side_effect=reserve_before_refresh) as requested, patch(
            "repo_platform.skills_repair.refresh_admitted"
        ) as refresh, patch("repo_platform.adoption.pin_checkout") as probe:
            self.assertEqual(command.run(args, owner=owner, output=output, errors=errors), 1)
            requested.assert_called_once()
            refresh.assert_not_called()
            probe.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertIn("lifecycle change", errors.getvalue())

    def test_retirement_while_retry_waits_stops_next_hosting_read(self):
        owner = self.command_owner()
        args = ["adopt", self.primary.checkout, "--project-id", "fixture-project"]
        clock = [100.0]
        host = Mock(side_effect=TransientProviderRead("fixture transport timeout"))

        def reserve_during_backoff(delay):
            clock[0] += delay
            self.inventory.reserve(self.version, "retirement", {self.primary.checkout: None},
                                   deadline=time.monotonic() + 2)

        errors = StringIO()
        with patch("repo_platform.recovery.time", side_effect=lambda: clock[0]), patch(
            "repo_platform.recovery.sleep", side_effect=reserve_during_backoff
        ), patch("repo_platform.adoption.pin_checkout") as probe:
            self.assertEqual(command.run(args, owner=replace(owner, read_hosting=host),
                                         output=StringIO(), errors=errors), 1)
            probe.assert_not_called()
        host.assert_called_once()
        self.assertIn("lifecycle change", errors.getvalue())

    def test_linked_desktop_connection_exhaustion_persists_and_preserves_accepted_result(self):
        owner = self.command_owner()
        args = ["adopt", self.linked.checkout, "--project-id", "fixture-project", "--thread-id", "fixture-thread"]
        self.assertEqual(command.run(args, owner=owner, output=StringIO()), 0)
        accepted, = owner.results.path.glob("adoption-*.json")
        previous = accepted.read_bytes()
        local_project = {"projectId": "fixture-project", "projectKind": "local", "hostId": "local",
                         "isGitRepository": True, "label": "Sample Product", "path": self.primary.checkout}
        catalog = Mock(return_value={"schemaVersion": 2, "projects": [local_project]})
        linked = Mock(side_effect=ConnectionResetError("fixture disconnected"))
        hosting = Mock(side_effect=owner.read_hosting)
        owner = replace(owner, read_native=DesktopReader(catalog, linked), read_hosting=hosting)
        output, errors = StringIO(), StringIO()
        clock = [100.0]
        with patch("repo_platform.recovery.time", side_effect=lambda: clock[0]), patch(
            "repo_platform.recovery.sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)
        ) as wait, patch("repo_platform.adoption.pin_checkout") as probe:
            self.assertEqual(command.run(args, owner=owner, output=output, errors=errors), 1)
            reopened = replace(owner, inventory=Inventory(owner.inventory.directory),
                               results=AdoptionResults(owner.results.path))
            self.assertEqual(command.run(args, owner=reopened, output=output, errors=errors), 1)
            probe.assert_not_called()
        self.assertEqual([call.args[0] for call in wait.call_args_list], [2, 5])
        self.assertEqual(catalog.call_count, 3)
        self.assertEqual(linked.call_count, 3)
        hosting.assert_not_called()
        self.assertIn("Codex diagnosis required after 3 attempt(s)", errors.getvalue())
        self.assertIn("Codex Desktop project connection", errors.getvalue())
        case, = owner.results.path.glob("recovery-*.json")
        record = json.loads(case.read_bytes())
        self.assertEqual((record["state"], record["kind"], record["elapsed_ms"]),
                         ("diagnose", "native-transport", 7000))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(accepted.read_bytes(), previous)

    def test_installed_owner_reader_uses_durable_profile_and_live_desktop_identity(self):
        root = self.root / "installed-owner"
        root.mkdir(mode=0o700)
        for name in ("inventory", "discovery", "results"):
            (root / name).mkdir(mode=0o700)
        shutil.copy2(self.inventory.database, root / "inventory" / self.inventory.database.name)
        settings = self.index_settings()
        stored = self.write_private(root / "discovery" / "settings.json", settings)
        expected = self.index(stale_offset=86401)
        projection = store_projection(stored.parent, expected.projection)
        for src in expected.cache.parent.glob(expected.cache.name + "*"):
            name = src.name.replace(expected.cache.name, "snapshot.json", 1)
            shutil.copy2(src, stored.parent / name)
        descriptor = {"schema": host.OWNER_SESSION_SCHEMA, "operation": "proof",
                      "skills_revision": SKILLS_REVISION, "projection_digest": projection.manifest_digest,
                      "physical_bindings": [asdict(binding) for binding in self.physical.entries]}
        descriptor_path = self.write_private(root / "owner-session.json", encode(descriptor))

        local_project = {"projectId": "fixture-project", "projectKind": "local", "hostId": "local",
                         "isGitRepository": True, "label": "Sample Product", "path": self.primary.checkout}
        list_projects = Mock(return_value={"schemaVersion": 2, "projects": [local_project]},
                             side_effect=[TimeoutError("fixture catalog timeout"), *([DEFAULT] * 4)])
        thread = {"id": "fixture-thread", "kind": "codex", "hostId": "local", "cwd": self.linked.checkout}
        linked = Mock(return_value=(thread | {"projectId": "fixture-project"}, thread))
        owner = self.command_owner()
        reader = DesktopReader(list_projects, linked)
        args = ["adopt", self.linked.checkout, "--project-id", "fixture-project", "--thread-id", "fixture-thread"]
        output, errors = StringIO(), StringIO()
        clock = [100.0]
        with patch("repo_platform.recovery.time", side_effect=lambda: clock[0]), patch(
            "repo_platform.recovery.sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)
        ) as wait:
            self.assertEqual(host.run_installed(args, owner_state=root, read_native=reader, read_hosting=self.hosting,
                                                read_delivery=owner.read_delivery, output=output, errors=errors),
                             0, errors.getvalue())
        wait.assert_called_once_with(2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["checkout"], self.linked.checkout)
        self.assertEqual(result["discovery"]["snapshot_generation"], 2)
        self.assertEqual(list_projects.call_count, 5)
        self.assertEqual(linked.call_count, 4)
        accepted, = (root / "results").glob("adoption-*.json")
        previous = accepted.read_bytes()

        list_projects.reset_mock(side_effect=True)
        self.write_private(stored, self.index_settings(optional_missing=True))
        with patch("repo_platform.adoption.pin_checkout") as probe:
            refusal, error = StringIO(), StringIO()
            self.assertEqual(host.run_installed(args, owner_state=root, read_native=reader, read_hosting=self.hosting,
                                                read_delivery=owner.read_delivery, output=refusal, errors=error), 1)
            probe.assert_not_called()
        self.assertEqual(refusal.getvalue(), "")
        self.assertIn("installed Dev owner state is unavailable", error.getvalue())
        list_projects.assert_not_called()
        self.assertEqual(accepted.read_bytes(), previous)

        digest = sha256(settings).hexdigest()
        versioned_settings = self.write_private(stored.parent / f"settings-{digest}.json", settings)
        versioned_snapshot = f"snapshot-{projection.manifest_digest}.json"
        for source in stored.parent.glob("snapshot.json*"):
            shutil.copy2(source, stored.parent / source.name.replace("snapshot.json", versioned_snapshot, 1))
        versioned = descriptor | {"schema": host.VERSIONED_OWNER_SESSION_SCHEMA, "settings_digest": digest}
        self.write_private(descriptor_path, encode(versioned))
        output, errors = StringIO(), StringIO()
        self.assertEqual(host.run_installed(args, owner_state=root, read_native=reader, read_hosting=self.hosting,
                                            read_delivery=owner.read_delivery, output=output, errors=errors),
                         0, errors.getvalue())
        self.assertEqual(json.loads(output.getvalue())["discovery"]["snapshot_generation"], 2)
        previous = accepted.read_bytes()
        list_projects.reset_mock()
        self.write_private(versioned_settings, self.index_settings(optional_missing=True))
        output, errors = StringIO(), StringIO()
        self.assertEqual(host.run_installed(args, owner_state=root, read_native=reader, read_hosting=self.hosting,
                                            read_delivery=owner.read_delivery, output=output, errors=errors), 1)
        self.assertIn("Skills settings differ", errors.getvalue())
        self.assertEqual(output.getvalue(), "")
        list_projects.assert_not_called()
        self.assertEqual(accepted.read_bytes(), previous)

    @staticmethod
    def write_private(path, content):
        path.write_bytes(content)
        path.chmod(0o600)
        return path

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
