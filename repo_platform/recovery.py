"""Bounded adoption recovery that survives a command restart."""

from contextlib import ExitStack
import os
import re
from time import monotonic, sleep, time

import github_checkout_provider
from github_checkout_provider import TransientProviderRead
from lib.contracts import Refusal, encode, json_load
from lib.io_safe import directory, outside_repositories, private_dir, read_at
from lib.receipt_store import AdmittedPublisher
from personal_checkout_inventory import Refused
from repo_platform.adoption_results import SELECTION_DIGEST, replace_private_record


BUDGET_SECONDS = 90
DELAYS = (2, 5)
MAX_ATTEMPTS = len(DELAYS) + 1
SCHEMA = "repo-platform/adoption-recovery/v1"
CASE_LIMIT = 16384
MILLISECONDS = 1000


class DiscoveryRepairRequired(Refused):
    """The registered Skills owner must requalify the selected evidence."""

    def __init__(self, message, *, snapshot_digest=None):
        super().__init__(message)
        self.snapshot_digest = snapshot_digest


class DiscoveryConflict(Refused):
    """Preserve a material claim that the Skills source owner must reconcile."""

    def __init__(self, message, *, snapshot_digest=None, material_digest=None):
        super().__init__(message)
        self.snapshot_digest = snapshot_digest
        self.material_digest = material_digest


class TransientNativeRead(Refused):
    """The native owner did not complete the project or linked chat read."""


class DiagnosisRequired(Refused):
    """The case needs Codex diagnosis before another automatic attempt."""


class GitHubResetPending(Refused):
    """The selection is waiting for GitHub's reported reset time."""


def clock_ms():
    return int(time() * MILLISECONDS)


def recovery_name(selection):
    if not SELECTION_DIGEST.fullmatch(selection):
        raise Refused("invalid native selection digest for the private adoption recovery record")
    return f"recovery-{selection}.json"


def require_github_reset(record):
    if record is not None and record["state"] == "cooldown" and clock_ms() < record["ready_ms"]:
        raise GitHubResetPending(record["reason"])


def check_github_cooldown(path, selection):
    """Read this selection's known reset time without opening a publisher."""
    parent = os.fspath(path)
    try:
        outside_repositories(parent)
        with directory(parent) as fd:
            private_dir(fd)
            require_github_reset(load_record(fd, recovery_name(selection)))
    except GitHubResetPending:
        raise
    except (Refusal, OSError) as error:
        raise Refused("the private adoption recovery record could not be read safely") from error


def load_record(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    record = json_load(read_at(fd, name, CASE_LIMIT, True), CASE_LIMIT)
    if (not isinstance(record, dict) or record.get("schema") != SCHEMA
            or not isinstance(record.get("evidence"), dict)
            or any(type(record.get(field)) is not int for field in ("started_ms", "ready_ms"))
            or type(record.get("attempts")) is not int or not 0 <= record["attempts"] <= MAX_ATTEMPTS
            or record.get("state") not in ("ready", "attempting", "retry", "diagnose", "cooldown")
            or record.get("kind") not in ("interrupted", "hosting-transport", "native-transport",
                                          "skills-refresh", "skills-conflict", "github-rate-limit")
            or not isinstance(record.get("reason"), str)):
        raise Refused("the private adoption recovery record could not be validated")
    if record["kind"] == "skills-conflict" and any(
            not isinstance(record.get(key), str) or re.fullmatch(r"[a-f0-9]{64}", record[key]) is None
            for key in ("material_digest", "failed_snapshot")):
        raise Refused("the private adoption recovery record could not be validated")
    if record["state"] == "cooldown" and (
            record["kind"] != "github-rate-limit" or record["ready_ms"] <= record["started_ms"]):
        raise Refused("the private adoption recovery record has an invalid GitHub reset")
    return record


def read_only_preview(verify):
    """Retry transient owner reads within the normal budget, without a journal."""
    expires = monotonic() + BUDGET_SECONDS
    for delay in (0, *DELAYS):
        if delay:
            if expires - monotonic() <= delay:
                break
            sleep(delay)
        try:
            return verify(expires)
        except (TransientProviderRead, TransientNativeRead) as error:
            last_error = error
    raise DiagnosisRequired(
        f"Codex diagnosis required. {last_error} Check the native project and hosting connections."
    ) from last_error


class RecoveryCase:
    def __init__(self, directory, selection, projection, skills_revision, operation, refreshed_snapshot):
        self.directory = os.fspath(directory)
        self.name = recovery_name(selection)
        self.projection = projection
        self.skills_revision = skills_revision
        self.operation = operation
        self.refreshed_snapshot = refreshed_snapshot
        self.started_ms = clock_ms()
        self.deadline = monotonic() + BUDGET_SECONDS
        self.resources = ExitStack()
        self.slot = None
        self.record = None

    def run(self, verify, repair_skills=None):
        """Run the full preview for every eligible attempt."""
        with self.resources:
            try:
                while True:
                    try:
                        result = verify(self.deadline, self.begin_attempt)
                    except (TransientProviderRead, TransientNativeRead) as error:
                        kind = "native-transport" if isinstance(error, TransientNativeRead) else "hosting-transport"
                        self.record_transport_failure(error, kind=kind)
                    except DiscoveryConflict as error:
                        if self.record is None or error.material_digest is None or error.snapshot_digest is None:
                            raise DiagnosisRequired(str(error)) from error
                        self.record.update(kind="skills-conflict", reason=str(error),
                                           failed_snapshot=error.snapshot_digest, material_digest=error.material_digest)
                        self.require_diagnosis()
                    except DiscoveryRepairRequired as error:
                        self.handle_discovery_failure(error, repair_skills)
                    except (DiagnosisRequired, GitHubResetPending):
                        raise
                    except Refused as error:
                        # The hosted CI compatibility pin predates this owner signal.
                        if isinstance(error, getattr(github_checkout_provider, "GitHubRateLimited", ())):
                            self.record_github_limit(error)
                        self.finish()
                        raise
                    else:
                        try:
                            self.finish()
                        except OSError as error:
                            raise DiagnosisRequired(
                                "The adoption result was published. Codex needs to reconcile its retry record."
                            ) from error
                        return result
            except Refused:
                # Keep the verified refusal or diagnosis actionable for the caller.
                raise
            except (Refusal, OSError) as error:
                raise Refused("the private adoption recovery record could not be maintained safely") from error

    def begin_attempt(self, version):
        """Reserve an attempt after Dev admits the selected path, before external reads."""
        if self.slot is None:
            path = os.path.join(self.directory, self.name)
            self.slot = self.resources.enter_context(AdmittedPublisher(path, CASE_LIMIT))
            self.record = load_record(self.slot.fd, self.slot.name)
        require_github_reset(self.record)
        if self.record is not None and self.record["state"] == "cooldown":
            # An explicit reset starts a new bounded verification window.
            self.record = None
        evidence = {"dev_version": list(version), "projection_digest": self.projection.manifest_digest(),
                    "skills_revision": self.skills_revision,
                    "owner_operation": self.operation}
        if self.record is not None and self.record["evidence"] == evidence and self.record["state"] == "diagnose":
            kind = self.record["kind"]
            if kind in {"skills-refresh", "skills-conflict"}:
                material = kind == "skills-conflict"
                previous = self.record.get("material_digest" if material else "failed_snapshot")
                current = self.refreshed_snapshot(version, self.deadline, material=material)
                if current is not None and current != previous:
                    self.record = None
        if self.record is None or self.record["evidence"] != evidence:
            self.record = {"schema": SCHEMA, "evidence": evidence, "started_ms": self.started_ms,
                           "ready_ms": self.started_ms, "attempts": 0, "state": "ready",
                           "reason": "The previous verification was interrupted.", "kind": "interrupted"}
        if self.record["state"] == "diagnose":
            self.require_diagnosis(persist=False)
        if self.record["attempts"] >= MAX_ATTEMPTS:
            self.require_diagnosis()

        now = clock_ms()
        end_ms = self.record["started_ms"] + BUDGET_SECONDS * MILLISECONDS
        remaining = min(self.deadline - monotonic(), (end_ms - now) / MILLISECONDS)
        delay = max(0, self.record["ready_ms"] - now) / MILLISECONDS
        if now < self.record["started_ms"] or delay >= remaining:
            self.require_diagnosis()
        if delay:
            sleep(delay)
        remaining = min(self.deadline - monotonic(), (end_ms - clock_ms()) / MILLISECONDS)
        if remaining <= 0:
            self.require_diagnosis()

        attempt_deadline = monotonic() + remaining
        attempt = self.record["attempts"] + 1
        delay_after_interruption = DELAYS[attempt - 1] if attempt < MAX_ATTEMPTS else 0
        self.record.update(attempts=attempt, state="attempting",
                           ready_ms=clock_ms() + delay_after_interruption * MILLISECONDS)
        self.save()
        return attempt_deadline

    def record_github_limit(self, error):
        if self.record is None:
            raise DiagnosisRequired(str(error)) from error
        self.record.update(kind="github-rate-limit", reason=str(error))
        retry_at = error.retry_at
        if type(retry_at) is not int or retry_at * MILLISECONDS <= clock_ms():
            self.require_diagnosis()
        self.record.update(state="cooldown", ready_ms=retry_at * MILLISECONDS)
        self.save()
        raise GitHubResetPending(str(error)) from error

    def record_transport_failure(self, error, *, kind):
        if self.record is None:
            raise DiagnosisRequired(str(error))
        self.record.update(kind=kind, reason=str(error), state="retry")
        attempt = self.record["attempts"]
        if attempt >= MAX_ATTEMPTS:
            self.require_diagnosis()
        self.record["ready_ms"] = clock_ms() + DELAYS[attempt - 1] * MILLISECONDS
        self.save()

    def handle_discovery_failure(self, error, repair_skills):
        if self.record is None:
            raise DiagnosisRequired(str(error))
        self.record.update(kind="skills-refresh", reason=str(error), state="diagnose",
                           failed_snapshot=error.snapshot_digest)
        if repair_skills is None or self.record.get("repair_attempted"):
            self.require_diagnosis()

        # Record the repair before invoking the owner. A restart can verify
        # completed publication but cannot repeat an interrupted repair blindly.
        self.record["repair_attempted"] = True
        self.save()
        version = tuple(self.record["evidence"]["dev_version"])
        try:
            refreshed = repair_skills(version=version, failed_snapshot=error.snapshot_digest, expires=self.deadline)
            if not refreshed or refreshed == error.snapshot_digest:
                raise Refused("the Skills refresh returned no changed, qualified receipt")
        except Refused as repair_error:
            self.record["reason"] = f"{error} Owner refresh result. {repair_error}"
            self.require_diagnosis()
        self.record.update(state="retry", ready_ms=clock_ms(), refreshed_snapshot=refreshed)
        self.save()

    def require_diagnosis(self, *, persist=True):
        if persist:
            self.record["state"] = "diagnose"
            self.save()
        owners = {"skills-refresh": "Skills refresh owner", "skills-conflict": "Skills source owner",
                  "native-transport": "Codex Desktop project connection",
                  "github-rate-limit": "Dev public GitHub reader"}
        owner = owners.get(self.record["kind"], "Dev owner session")
        raise DiagnosisRequired(
            f"Codex diagnosis required after {self.record['attempts']} attempt(s). "
            f"{self.record['reason']} Check current evidence through the {owner}."
        )

    def save(self):
        self.record["elapsed_ms"] = max(0, clock_ms() - self.record["started_ms"])
        replace_private_record(self.slot, encode(self.record))

    def finish(self):
        if self.slot is not None and self.record is not None:
            os.unlink(self.slot.name, dir_fd=self.slot.fd)
            os.fsync(self.slot.fd)
            self.record = None
