"""Preview one explicit checkout using its current Dev and native owner bindings."""

from dataclasses import dataclass
import json
import os
import re
import time

from checkout_git_config import config_proof, verify_git_config
from github_checkout_provider import GitHubRepositoryObservation, validate_branch_name
from issue_checkout_evidence import SOURCE_READ_TIMES
from native_checkout_project import NativeProjectBinding, selection_digest
from personal_checkout_inventory import Binding, Refused, lexical
from personal_scope import child_directory, read_regular
from physical_checkout_binding import OwnerPhysicalBindings, ROLES, pin_checkout
from repo_platform.discovery import DiscoveryEvidence
from skills_inventory_projection import linked_relationship


MAX_OBSERVATION_AGE = 86400
GUARDED_READ_SECONDS = 2
COMMIT_ID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})")
OWNER_HANDOFFS = {
    "actions-app": "repository-ci-and-reviewer-app",
    "native-github": "repository-ci-and-reviewer-app",
    "private-no-ci": "private-repository-owner",
    "unenrolled": "dev-delivery-enrollment-owner",
}


@dataclass(frozen=True)
class DeliveryClassification:
    repository_id: int
    repository_name: str
    disposition: str
    policy_revision: str


@dataclass(frozen=True)
class AdoptionPreview:
    project_id: str
    checkout: str
    git_directory: str
    common_directory: str
    repository_id: int
    repository_name: str
    project_title: str | None
    local_head: str
    hosted_default_branch: str
    hosted_default_head: str
    private: bool
    native_selection_digest: str
    native_observed_at: int
    hosting_identity_observed_at: int
    hosting_ref_observed_at: int
    dev_version: tuple[str, int, str]
    owner_evidence_digest: str
    delivery: DeliveryClassification | None
    owner_handoff: str | None
    discovery: DiscoveryEvidence | None
    warnings: tuple[str, ...]


def _fresh(observed_at, now):
    return type(observed_at) is int and 0 <= now - observed_at <= MAX_OBSERVATION_AGE


def _packed_ref(directory, ref):
    try:
        packed = read_regular(directory, "packed-refs")
    except FileNotFoundError as error:
        raise Refused("the local branch head could not be resolved") from error
    matches = []
    for line in packed.splitlines():
        if not line.startswith((b"#", b"^")):
            value, separator, name = line.partition(b" ")
            if separator and name == ref:
                matches.append(value)
    if len(matches) != 1:
        raise Refused("the local branch head could not be uniquely resolved")
    return matches[0]


def _branch_head(common, ref):
    try:
        name = ref.decode("utf-8")
        validate_branch_name(name.removeprefix("refs/heads/"))
    except (UnicodeError, Refused) as error:
        raise Refused("the local HEAD contains an invalid branch reference") from error
    *parents, leaf = name.split("/")
    try:
        parent = child_directory(common, tuple(parents))
        try:
            return read_regular(parent, leaf, max_bytes=4096).removesuffix(b"\n")
        finally:
            os.close(parent)
    except FileNotFoundError:
        return _packed_ref(common, ref)


def _local_head(binding, reader):
    # Use the admitted directory handles. Invoking Git here would load checkout
    # configuration and could follow paths outside the authorized Git roles.
    with reader.directory(binding.git_directory) as own:
        try:
            head = read_regular(own, "HEAD", max_bytes=4096).removesuffix(b"\n")
            if head.startswith(b"ref: refs/heads/"):
                with reader.directory(binding.common_directory) as common:
                    head = _branch_head(common, head.removeprefix(b"ref: "))
        except (OSError, ValueError) as error:
            raise Refused("the local HEAD could not be read through the admitted Git directories") from error
    if not COMMIT_ID.fullmatch(head):
        raise Refused("the selected checkout has no resolved local commit ID")
    return head.decode("ascii")


def preview(inventory, checkout, project_id, owner_operation, *, physical,
            read_native, read_hosting, deadline, thread_id=None, expected_title=None,
            expected_repository_id=None, read_delivery=None, discovery=None, results=None,
            begin_attempt=None):
    """Qualify one checkout and optionally record the result without changing the checkout."""
    checkout = lexical(checkout)
    selected = selection_digest(project_id, checkout, thread_id)
    raw, version = inventory.snapshot()
    inventory_rows = json.loads(raw)["bindings"]
    row = inventory_rows.get(checkout)
    # Route even an absent row through Dev so it can distinguish a denied path
    # from an unregistered path before any native, provider, or target read.
    locations = (("checkout", checkout),)
    if row is not None:
        binding = Binding(**row)
        locations = tuple((role, getattr(binding, role)) for role in ROLES)
    binding = inventory.read_bound(
        version, checkout, "adopt", locations, lambda admitted, _: admitted,
        deadline=min(deadline, time.monotonic() + GUARDED_READ_SECONDS),
        include_linked_metadata=True,
    )
    if binding.provider != "github":
        raise Refused("the selected checkout has no supported hosting provider")
    if expected_repository_id is not None and (
        type(expected_repository_id) is not int or binding.repository_id != expected_repository_id
    ):
        raise Refused("the requested repository ID differs from Dev's selected checkout binding")
    if bool(binding.linked_metadata()) != (thread_id is not None):
        raise Refused("the native selection must identify the admitted primary or linked checkout")
    if not binding.linked_metadata() and binding.git_directory != checkout + "/.git":
        raise Refused("the primary checkout does not have an admitted in-place Git directory")
    if begin_attempt is not None:
        deadline = min(deadline, begin_attempt(version))
    expected_result = results.reference(selected) if results is not None else None

    scope = inventory.current_owner_evidence(
        owner_operation, deadline=min(deadline, time.monotonic() + GUARDED_READ_SECONDS)
    )
    if scope.current != version or binding not in scope.bindings or scope.record is None:
        raise Refused("the selected checkout lacks current committed Dev owner evidence")
    record = json.loads(scope.record)
    rows = [row for row in record["observations"] if row["checkout"] == checkout]
    if len(rows) != 1:
        raise Refused("the committed owner evidence does not identify one selected checkout")
    owner, = rows
    if (record["dev_policy_revision"] != version[0] or owner.get("project_id") != project_id
            or owner.get("native_selection_digest") != selected
            or owner.get("repository_id") != binding.repository_id):
        raise Refused("the committed owner evidence differs from the selected checkout identity")
    proofs = OwnerPhysicalBindings(tuple(physical.find(item) for item in scope.bindings))
    if proofs.digest() != record["physical_binding_digest"]:
        raise Refused("the supplied physical proof differs from committed Dev evidence")
    proof = proofs.find(binding)
    config = config_proof(owner)

    def observe_native():
        observation = read_native(project_id, checkout, deadline=deadline)
        if not isinstance(observation, NativeProjectBinding) or observation.selection_digest != selected:
            raise Refused("the native project no longer identifies the selected checkout")
        if not _fresh(observation.observed_at, int(time.time())):
            raise Refused("a fresh native project observation is required")
        return observation

    initial_native = observe_native()
    hosted = read_hosting(binding.repository_id, deadline=deadline)
    if (not isinstance(hosted, GitHubRepositoryObservation)
            or hosted.repository_id != binding.repository_id
            or not isinstance(hosted.full_name, str)
            or hosted.full_name.casefold() != owner["github_name"].casefold()):
        raise Refused("the hosting identity differs from Dev's selected checkout binding")

    delivery = None
    if read_delivery is not None:
        delivery = read_delivery(hosted, deadline=deadline)
        if (not isinstance(delivery, DeliveryClassification)
                or delivery.repository_id != hosted.repository_id or delivery.repository_name != hosted.full_name
                or not delivery.policy_revision
                or delivery.disposition not in OWNER_HANDOFFS):
            raise Refused("Dev delivery policy does not permit a handoff for the observed repository")
        if (delivery.disposition in {"actions-app", "native-github", "private-no-ci"}
                and hosted.private != (delivery.disposition == "private-no-ci")):
            raise Refused("the observed repository visibility conflicts with Dev's delivery policy")

    native = observe_native()
    if native.observed_at < initial_native.observed_at:
        raise Refused("the native project recheck returned an earlier observation")

    def inspect(admitted, expires):
        now = int(time.time())
        times = (initial_native.observed_at, native.observed_at,
                 hosted.identity_observed_at, hosted.ref_observed_at,
                 *(owner[name] for name in SOURCE_READ_TIMES))
        if not all(_fresh(observed, now) for observed in times):
            raise Refused("fresh owner, native, and hosting observations are required")

        indexed = None
        if discovery is not None:
            indexed = discovery.qualify(
                inventory=inventory, version=version, rows=inventory_rows, binding=admitted,
                physical=physical, hosted=hosted, expires=expires,
            )
        with pin_checkout(admitted, proof, locations + admitted.linked_metadata(), expires) as reader:
            linked_relationship(admitted, reader)
            verify_git_config(admitted, reader, config)
            head = _local_head(admitted, reader)
            if head != _local_head(admitted, reader):
                raise Refused("the local HEAD changed during the preview")
            verify_git_config(admitted, reader, config)
            linked_relationship(admitted, reader)
        warnings = list(indexed.warnings) if indexed is not None else []
        if native.title != initial_native.title:
            warnings.append("Codex project display title changed during the preview.")
        if expected_title is not None and native.title is not None and native.title != expected_title:
            warnings.append("Codex project display title differs from the selected title.")
        result = AdoptionPreview(
            project_id, checkout, binding.git_directory, binding.common_directory,
            hosted.repository_id, hosted.full_name, native.title, head, hosted.default_branch,
            hosted.default_head, hosted.private, native.selection_digest, native.observed_at,
            hosted.identity_observed_at, hosted.ref_observed_at, version, scope.digest, delivery,
            OWNER_HANDOFFS[delivery.disposition] if delivery is not None else None, indexed,
            tuple(warnings),
        )
        if delivery is not None and read_delivery(hosted, deadline=expires) != delivery:
            raise Refused("Dev delivery policy changed during checkout adoption; rerun with current evidence")
        if results is not None:
            results.publish(result, expected_result, expires=expires)
        return result

    return inventory.read_bound(
        version, checkout, "adopt", locations, inspect,
        deadline=min(deadline, time.monotonic() + GUARDED_READ_SECONDS),
        include_linked_metadata=True,
    )
