"""Bounded adoption recovery that survives a command restart."""

from contextlib import ExitStack
import os
from time import monotonic, sleep, time

from github_checkout_provider import TransientProviderRead
from lib.contracts import Refusal, encode, json_load
from lib.io_safe import read_at
from lib.receipt_store import AdmittedPublisher
from personal_checkout_inventory import Refused
from repo_platform.adoption_results import replace_private_record


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


class TransientNativeRead(Refused):
    """The native owner did not complete the project or linked chat read."""


class DiagnosisRequired(Refused):
    """The case needs Codex diagnosis before another automatic attempt."""


def clock_ms():
    return int(time() * MILLISECONDS)


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
        self.name = f"recovery-{selection}.json"
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
                    except DiscoveryRepairRequired as error:
                        self.handle_discovery_failure(error, repair_skills)
                    except DiagnosisRequired:
                        raise
                    except Refused:
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
            except (Refusal, OSError) as error:
                raise Refused("the private adoption recovery record could not be maintained safely") from error

    def begin_attempt(self, version):
        """Reserve an attempt after Dev admits the selected path, before external reads."""
        if self.slot is None:
            path = os.path.join(self.directory, self.name)
            self.slot = self.resources.enter_context(AdmittedPublisher(path, CASE_LIMIT))
            self.record = self.load()
        evidence = {"dev_version": list(version), "projection_digest": self.projection.manifest_digest(),
                    "skills_revision": self.skills_revision,
                    "owner_operation": self.operation}
        if (self.record is not None and self.record["evidence"] == evidence
                and self.record["state"] == "diagnose" and self.record["kind"] == "skills-refresh"):
            fresh_digest = self.refreshed_snapshot(version, self.deadline)
            if fresh_digest is not None and fresh_digest != self.record.get("failed_snapshot"):
                self.record = None
        if self.record is None or self.record["evidence"] != evidence:
            self.record = {"schema": SCHEMA, "evidence": evidence, "started_ms": self.started_ms,
                           "ready_ms": self.started_ms, "attempts": 0, "state": "ready",
                           "reason": "The previous verification was interrupted.", "kind": "interrupted"}
        if self.record["state"] == "diagnose" or self.record["attempts"] >= MAX_ATTEMPTS:
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

    def require_diagnosis(self):
        self.record["state"] = "diagnose"
        self.save()
        owners = {"skills-refresh": "Skills refresh owner", "native-transport": "Codex Desktop project connection"}
        owner = owners.get(self.record["kind"], "Dev owner session")
        raise DiagnosisRequired(
            f"Codex diagnosis required after {self.record['attempts']} attempt(s). "
            f"{self.record['reason']} Check current evidence through the {owner}."
        )

    def load(self):
        try:
            os.stat(self.slot.name, dir_fd=self.slot.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        record = json_load(read_at(self.slot.fd, self.slot.name, CASE_LIMIT, True), CASE_LIMIT)
        if (not isinstance(record, dict) or record.get("schema") != SCHEMA
                or not isinstance(record.get("evidence"), dict)
                or any(type(record.get(field)) is not int for field in ("started_ms", "ready_ms"))
                or type(record.get("attempts")) is not int or not 0 <= record["attempts"] <= MAX_ATTEMPTS
                or record.get("state") not in ("ready", "attempting", "retry", "diagnose")
                or record.get("kind") not in ("interrupted", "hosting-transport", "native-transport", "skills-refresh")
                or not isinstance(record.get("reason"), str)):
            raise Refused("the private adoption recovery record could not be validated")
        return record

    def save(self):
        self.record["elapsed_ms"] = max(0, clock_ms() - self.record["started_ms"])
        replace_private_record(self.slot, encode(self.record))

    def finish(self):
        if self.slot is not None and self.record is not None:
            os.unlink(self.slot.name, dir_fd=self.slot.fd)
            os.fsync(self.slot.fd)
            self.record = None
