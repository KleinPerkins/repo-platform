from dataclasses import replace
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from github_checkout_provider import GitHubRepositoryObservation
from issue_checkout_evidence import issue_checkout_evidence_reissue, observe_checkout_authorities
from native_checkout_project import NativeProjectBinding
from personal_checkout_inventory import Binding, Inventory, Refused
from physical_checkout_binding import OwnerPhysicalBinding, OwnerPhysicalBindings, ROLES
from repo_platform import adoption


HOSTED_HEAD = "ace0" * 10


def git(root, *args):
    return subprocess.check_output(
        ["/usr/bin/git", "-C", str(root), *args],
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null"}, stderr=subprocess.PIPE, text=True,
    ).strip()


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="repo-platform-adoption-")
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
        self.denied = str(self.root / "denied")
        (self.root / "authority").mkdir(mode=0o700)
        self.inventory = Inventory(self.root / "authority")
        version = self.inventory.initialize("fixture-host", "fixture-policy", [self.denied],
                                            [self.primary, self.linked])
        proofs = []
        for binding in (self.primary, self.linked):
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

    def select(self, path, *, read_native=None, read_hosting=None, thread=None):
        return adoption.preview(
            self.inventory, path, "fixture-project", "proof", physical=self.physical,
            read_native=read_native or self.native, read_hosting=read_hosting or self.hosting,
            thread_id=thread, deadline=time.monotonic() + 3, expected_title="Sample Product",
        )

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
        native.assert_called_once()
        hosting.assert_called_once()

    def test_invoked_provider_cannot_substitute_another_repository(self):
        def different_provider(repository_id, *, deadline):
            return replace(self.hosting(repository_id, deadline=deadline), repository_id=74)

        with patch("repo_platform.adoption.pin_checkout") as probe:
            with self.assertRaisesRegex(Refused, "hosting identity"):
                self.select(self.primary.checkout, read_hosting=different_provider)
            probe.assert_not_called()

    def test_lifecycle_reservation_after_native_read_stops_target_probe(self):
        def reserve_retirement(project_id, checkout, *, deadline):
            result = self.native(project_id, checkout, deadline=deadline)
            self.inventory.reserve(self.version, "retirement", {checkout: None}, deadline=deadline)
            return result

        with patch("repo_platform.adoption.pin_checkout") as probe:
            with self.assertRaisesRegex(Refused, "Dev inventory changed"):
                self.select(self.primary.checkout, read_native=reserve_retirement)
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
