"""Qualify a selected checkout against the Skills-owned discovery snapshot."""

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import time

from lib.adapters import typed
from lib.contracts import Refusal, timestamp
from lib.deadline import deadline
from lib.engine import query
from lib.receipt_store import load_qualified_snapshot
from personal_checkout_inventory import Refused
from repo_platform.recovery import DiscoveryRepairRequired
from skills_inventory_projection import Projection, guarded_admission


@dataclass(frozen=True)
class DiscoveryObservation:
    entity_id: str
    field: str
    pointer: str
    source_kind: str
    observed_at: str
    state: str

    @classmethod
    def from_snapshot(cls, entity_id, item):
        return cls(entity_id, item["field"], item["pointer"], item["source_kind"],
                   item["observed_at"], item["state"])


@dataclass(frozen=True)
class DiscoveryEvidence:
    skills_revision: str
    snapshot_generation: int
    snapshot_digest: str
    snapshot_observed_at: str
    coverage: str
    repository_entity: str
    clone_entity: str
    worktree_entity: str
    observations: tuple[DiscoveryObservation, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class SkillsDiscovery:
    """The exact owner projection and cache supplied by the Dev session."""

    projection: Projection
    cache: os.PathLike | str
    skills_revision: str

    def refreshed_reference(self, inventory, version, physical, expires):
        """Read a fresh owner-bound receipt after an earlier Skills diagnosis."""
        remaining = min(2, expires - time.monotonic())
        if remaining <= 0 or self.projection.version != version:
            return None
        admission = guarded_admission(inventory, self.projection, self.skills_revision, physical=physical)
        try:
            with deadline(remaining):
                snapshot, receipt = load_qualified_snapshot(os.fspath(self.cache), admission)
                if _current(snapshot["observed_at"], datetime.now(timezone.utc)):
                    return receipt["snapshot_digest"]
        except Refusal:
            pass
        return None

    def qualify(self, *, inventory, version, rows, binding, physical, hosted, expires):
        """Read inside the caller's current Dev adoption guard."""
        if self.projection.version != version:
            raise DiscoveryRepairRequired("the Skills index has an older Dev binding; refresh the current projection")
        remaining = expires - time.monotonic()
        if remaining <= 0:
            raise Refused("the Skills index check exceeded the adoption read budget")
        admission = guarded_admission(inventory, self.projection, self.skills_revision, physical=physical)
        receipt = None
        try:
            with deadline(remaining):
                snapshot, receipt = load_qualified_snapshot(os.fspath(self.cache), admission)
                roots = admission.validate()["roots"]
                return _selected(snapshot, receipt, roots, self.projection.omitted, rows, binding, hosted)
        except Refusal as error:
            raise DiscoveryRepairRequired(
                f"the Skills index receipt or read is unavailable ({error.code}); check the owner refresh",
                snapshot_digest=receipt["snapshot_digest"] if receipt is not None else None,
            ) from error


def _current(stamp, now):
    return 0 <= (now - timestamp(stamp)).total_seconds() <= 86400


def _require_observation(entity, field, expected, pointer, now):
    observations = [item for item in entity["observations"]
                    if item["field"] == field and item["pointer"] == pointer
                    and item["value"] == expected and item["state"] == "current"]
    if len(observations) != 1 or not _current(observations[0]["observed_at"], now):
        raise Refused("the Skills index lacks a fresh selected identity or location observation")
    return DiscoveryObservation.from_snapshot(entity["id"], observations[0])


def _shares_location(row, binding):
    return not binding.locations().isdisjoint(
        row[key] for key in ("checkout", "git_directory", "common_directory")
    )


def _selected(snapshot, receipt, roots, omitted, inventory_rows, binding, hosted):
    now = datetime.now(timezone.utc)
    if not _current(snapshot["observed_at"], now):
        raise DiscoveryRepairRequired(
            "the Skills snapshot is stale or future-dated; refresh the current projection",
            snapshot_digest=receipt["snapshot_digest"],
        )

    provider = f"{binding.provider}:{binding.repository_id}"
    repository = typed("repository", "provider:" + provider)
    clone = typed("clone", binding.common_directory)
    worktree = typed("worktree", binding.git_directory + "\0" + binding.checkout)
    by_path = {root["path"]: root for root in roots}
    selected_root = by_path.get(binding.checkout)
    if (selected_root is None or selected_root["identity"]["provider"] != provider
            or selected_root["git"] != {"common_dir": binding.common_directory, "git_dir": binding.git_directory}):
        raise Refused("the Skills projection does not bind the selected checkout and Git directories")
    key = "inventory/" + selected_root["key"]

    related = {path for path, row in inventory_rows.items() if _shares_location(row, binding)}
    if any(path in related for path, _ in omitted):
        raise Refused("the Skills projection omits a checkout related to the selected identity")
    required = {key, key + "/git"}
    for path in related.intersection(by_path):
        peer = by_path[path]
        peer_key = "inventory/" + peer["key"]
        required.add(peer_key)
        if peer["git"] is not None:
            required.add(peer_key + "/git")
    diagnostics = [receipt["input_diagnostics"][name] for name in required.intersection(receipt["input_diagnostics"])]
    if any(item["state"] in {"conflict", "omitted"} for item in diagnostics):
        raise Refused("the Skills refresh reports an unresolved input for the selected identity")
    if diagnostics:
        raise DiscoveryRepairRequired(
            "the Skills collector could not qualify a required checkout input",
            snapshot_digest=receipt["snapshot_digest"],
        )
    current_keys = {item["key"] for item in snapshot["inputs"] if item["state"] == "current"}
    if not required <= current_keys:
        raise DiscoveryRepairRequired(
            "the Skills index lacks a current input for the selected identity",
            snapshot_digest=receipt["snapshot_digest"],
        )

    entities = {entity["id"]: entity for entity in snapshot["entities"]}
    selected_ids = (repository, clone, worktree)
    parents = (None, repository, clone)
    if any(eid not in entities or entities[eid]["parent"] != parent
           for eid, parent in zip(selected_ids, parents, strict=True)):
        raise Refused("the Skills index does not establish the selected checkout's parent chain")
    _check_conflicts(entities, selected_ids, related, inventory_rows, binding, provider)
    context = query(snapshot, "context", id=worktree)
    if context["status"] != "matches" or context["source_owner"] != repository:
        raise Refused("the Skills index has ambiguous ownership for the selected checkout")

    observed = (
        _require_observation(entities[repository], "identity", "provider:" + provider, key, now),
        _require_observation(entities[repository], "location", binding.checkout, key, now),
        _require_observation(entities[clone], "location", binding.common_directory, key + "/git/common/config", now),
        _require_observation(entities[worktree], "location", binding.checkout, key + "/git/worktree", now),
    )
    remote = ("remote:github.com/" + hosted.full_name).casefold()
    origins = [item for item in entities[repository]["observations"]
               if item["field"] == "identity" and item["pointer"] == key + "/git/common/config"]
    if (len(origins) != 1 or origins[0]["state"] != "current" or origins[0]["value"].casefold() != remote
            or not _current(origins[0]["observed_at"], now)):
        raise Refused("the Skills index lacks a fresh matching origin for the selected checkout")

    warnings = []
    if snapshot["coverage"] == "partial" or omitted:
        warnings.append("Skills discovery has incomplete coverage outside the required checkout evidence.")
    if "title" in entities[repository]["conflicts"]:
        warnings.append("Skills discovery reports differing display titles for this repository.")
    return DiscoveryEvidence(
        receipt["skills_revision"], receipt["snapshot_generation"], receipt["snapshot_digest"],
        snapshot["observed_at"], snapshot["coverage"], repository, clone, worktree,
        (*observed, DiscoveryObservation.from_snapshot(repository, origins[0])), tuple(warnings),
    )


def _check_conflicts(entities, selected_ids, related, inventory_rows, binding, provider):
    repository, clone, worktree = selected_ids
    claimants = {path: {"repository": repository, "clone": typed("clone", row["common_directory"]),
                        "worktree": typed("worktree", row["git_directory"] + "\0" + path)}
                 for path, row in inventory_rows.items() if path in related}
    git_roles = {}
    git_roles.setdefault(binding.git_directory, set()).add(worktree)
    git_roles.setdefault(binding.common_directory, set()).add(clone)
    selected_locations = {clone: binding.common_directory, worktree: binding.checkout}
    for entity in entities.values():
        entity_id, kind = entity["id"], entity["kind"]
        matched = entity_id in selected_ids
        for item in entity["observations"]:
            if item["state"] == "missing":
                continue
            if (item["field"] == "identity" and item["value"] == "provider:" + provider
                    and entity_id != repository):
                raise Refused("the Skills index contains a conflicting claim on the immutable repository ID")
            if (entity_id in selected_locations and item["field"] == "location"
                    and item["value"] != selected_locations[entity_id]):
                raise Refused("the Skills index contains conflicting locations for the selected Git entity")
            locations = ([item["value"]] if item["field"] == "location"
                         else item["value"] if item["field"] == "aliases" else [])
            for path in locations:
                if path in claimants and kind in claimants[path]:
                    if entity_id != claimants[path][kind]:
                        raise Refused("the Skills index contains a conflicting claim on an admitted checkout")
                    matched = True
                if path in git_roles and kind in {"clone", "worktree"} and entity_id not in git_roles[path]:
                    raise Refused("the Skills index contains a conflicting claim on an admitted Git location")
        if matched and {"identity", "parent"}.intersection(entity["conflicts"]):
            raise Refused("the Skills index contains an unresolved identity or parent conflict")
