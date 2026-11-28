"""Qualify a selected checkout against the Skills-owned discovery snapshot."""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import os
import time

from lib.adapters import typed
from lib.contracts import Refusal, encode, timestamp
from lib.deadline import deadline
from lib.engine import query
from lib.receipt_store import load_qualified_snapshot
from personal_checkout_inventory import Refused
from repo_platform.recovery import DiscoveryConflict, DiscoveryRepairRequired
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
    related_observations: tuple[DiscoveryObservation, ...] = ()


@dataclass(frozen=True)
class SkillsDiscovery:
    """The exact owner projection and cache supplied by the Dev session."""

    projection: Projection
    cache: os.PathLike | str
    skills_revision: str

    def refreshed_reference(self, inventory, version, physical, expires, *, material=False):
        """Read current owner evidence without accessing a checkout or refreshing."""
        remaining = min(2, expires - time.monotonic())
        if remaining <= 0 or self.projection.version != version:
            return None
        admission = guarded_admission(inventory, self.projection, self.skills_revision, physical=physical)
        try:
            with deadline(remaining):
                snapshot, receipt = load_qualified_snapshot(os.fspath(self.cache), admission)
                if _current(snapshot["observed_at"], datetime.now(timezone.utc)):
                    return _material_reference(snapshot, receipt) if material else receipt["snapshot_digest"]
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
        except DiscoveryConflict as error:
            raise DiscoveryConflict(str(error), snapshot_digest=receipt["snapshot_digest"],
                                    material_digest=_material_reference(snapshot, receipt)) from error
        except Refusal as error:
            raise DiscoveryRepairRequired(
                f"the Skills index receipt or read is unavailable ({error.code}); check the owner refresh",
                snapshot_digest=receipt["snapshot_digest"] if receipt is not None else None,
            ) from error


def _material_reference(snapshot, receipt):
    """Bind ownership claims and collector outcomes, independent of refresh timestamps."""
    claims = []
    for entity in snapshot["entities"]:
        observations = [tuple(item[key] for key in ("field", "value", "state", "source_kind", "pointer"))
                        for item in entity["observations"]
                        if item["field"] in {"identity", "location", "aliases", "edges"} and item["state"] != "missing"]
        conflicts = sorted({"identity", "location", "parent"}.intersection(entity["conflicts"]))
        if observations or conflicts:
            claims.append((entity["id"], entity["parent"], conflicts, sorted(observations, key=encode)))
    diagnostics = {key: entry for key, entry in receipt["input_diagnostics"].items()
                   if key.count("/") == 1 or (key.endswith("/git") and key.count("/") == 2)}
    return sha256(encode((sorted(claims, key=lambda claim: claim[0]), diagnostics))).hexdigest()


def _current(stamp, now):
    return 0 <= (now - timestamp(stamp)).total_seconds() <= 86400


def _require_current_observation(entity_id, item, now, snapshot_digest):
    if item["state"] != "current" or not _current(item["observed_at"], now):
        raise DiscoveryRepairRequired(
            f"a required Skills {item['field']} observation is out of date; refresh the current projection",
            snapshot_digest=snapshot_digest,
        )
    return DiscoveryObservation.from_snapshot(entity_id, item)


def _require_observation(entity, field, expected, pointer, now, snapshot_digest):
    matches = [item for item in entity["observations"]
               if item["field"] == field and item["pointer"] == pointer and item["value"] == expected]
    observations = [item for item in matches if item["state"] == "current"] or matches
    if len(observations) != 1:
        raise DiscoveryConflict("the Skills index lacks a unique selected identity or location observation")
    return _require_current_observation(entity["id"], observations[0], now, snapshot_digest)


def _shares_location(row, binding):
    return not binding.locations().isdisjoint(
        row[key] for key in ("checkout", "git_directory", "common_directory")
    )


def _root_observations(entities, members, root, provider, remote, now, snapshot_digest):
    repository, clone, worktree = members
    key = "inventory/" + root["key"]
    common = key + "/git/common/config"
    observed = (
        _require_observation(entities[repository], "identity", "provider:" + provider, key, now, snapshot_digest),
        _require_observation(entities[repository], "location", root["path"], key, now, snapshot_digest),
        _require_observation(entities[clone], "location", root["git"]["common_dir"], common, now, snapshot_digest),
        _require_observation(entities[worktree], "location", root["path"],
                             key + "/git/worktree", now, snapshot_digest),
    )
    origins = [item for item in entities[repository]["observations"]
               if item["field"] == "identity" and item["pointer"] == common]
    if len(origins) != 1 or origins[0]["value"].casefold() != remote:
        raise DiscoveryConflict("the Skills index lacks a matching origin for an admitted checkout in the Git family")
    return (*observed, _require_current_observation(repository, origins[0], now, snapshot_digest))


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
    family_parents = {repository: None, clone: repository, worktree: clone}
    peers = []
    for path in sorted(related - {binding.checkout}):
        peer = by_path.get(path)
        if peer is None or peer["git"] is None:
            raise DiscoveryRepairRequired(
                "the Skills projection lacks an admitted Git checkout related to the selection",
                snapshot_digest=receipt["snapshot_digest"],
            )
        peer_key = "inventory/" + peer["key"]
        required.update((peer_key, peer_key + "/git"))
        peer_clone = typed("clone", peer["git"]["common_dir"])
        peer_worktree = typed("worktree", peer["git"]["git_dir"] + "\0" + path)
        family_parents[peer_clone] = repository
        family_parents[peer_worktree] = peer_clone
        peers.append((peer, (repository, peer_clone, peer_worktree)))
    diagnostics = [receipt["input_diagnostics"][name] for name in required.intersection(receipt["input_diagnostics"])]
    if any(item["state"] in {"conflict", "omitted"} for item in diagnostics):
        raise DiscoveryConflict("the Skills refresh reports an unresolved input for the selected identity")
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
    if any(eid not in entities or entities[eid]["parent"] != parent
           for eid, parent in family_parents.items()):
        raise DiscoveryConflict("the Skills index does not establish a matching shared Git checkout parent")
    _check_conflicts(entities, repository, related, inventory_rows, provider, family_parents)

    snapshot_digest = receipt["snapshot_digest"]
    remote = ("remote:github.com/" + hosted.full_name).casefold()
    observed = _root_observations(entities, selected_ids, selected_root, provider, remote, now, snapshot_digest)
    related_observations = tuple(
        observation for peer, members in peers
        for observation in _root_observations(entities, members, peer, provider, remote, now, snapshot_digest)
    )

    # Validate claims across the full receipt first. The context query checks
    # only the selected Git family, so unrelated timestamps cannot veto it.
    context_entities = [entity for eid, entity in entities.items() if eid in family_parents]
    context = query({**snapshot, "entities": context_entities}, "context", id=worktree)
    if context["status"] != "matches" or context["source_owner"] != repository:
        raise DiscoveryConflict("the Skills index has ambiguous ownership for the selected checkout")

    warnings = []
    if snapshot["coverage"] == "partial" or omitted:
        warnings.append("Skills discovery has incomplete coverage outside the required checkout evidence.")
    if any(timestamp(item["observed_at"]) > now for eid, entity in entities.items() if eid not in family_parents
           for item in entity["observations"]):
        warnings.append("Skills discovery includes future-dated evidence outside the selected checkout's Git family.")
    if "title" in entities[repository]["conflicts"]:
        warnings.append("Skills discovery reports differing display titles for this repository.")
    return DiscoveryEvidence(
        receipt["skills_revision"], receipt["snapshot_generation"], receipt["snapshot_digest"],
        snapshot["observed_at"], snapshot["coverage"], repository, clone, worktree,
        observed, tuple(warnings), related_observations=related_observations,
    )


def _check_conflicts(entities, repository, related, inventory_rows, provider, family_parents):
    claimants = {}
    git_roles = {}
    family_locations = {}
    for path in related:
        row = inventory_rows[path]
        common, git_dir = row["common_directory"], row["git_directory"]
        clone = typed("clone", common)
        worktree = typed("worktree", git_dir + "\0" + path)
        claimants[path] = {"repository": repository, "clone": clone, "worktree": worktree}
        git_roles.setdefault(common, set()).add(clone)
        git_roles.setdefault(git_dir, set()).add(worktree)
        family_locations[clone] = common
        family_locations[worktree] = path
    for entity in entities.values():
        entity_id, kind = entity["id"], entity["kind"]
        matched = entity_id in family_parents
        for item in entity["observations"]:
            if item["state"] == "missing":
                continue
            if entity_id in family_parents and item["field"] == "edges" and any(
                    edge["predicate"] == "belongs_to" and edge["classification"] in {"declared", "observed"}
                    and edge["target"] != family_parents[entity_id] for edge in item["value"]):
                raise DiscoveryConflict("the Skills index contains a contradictory shared Git checkout parent claim")
            if (item["field"] == "identity" and item["value"] == "provider:" + provider
                    and entity_id != repository):
                raise DiscoveryConflict("the Skills index contains a conflicting claim on the immutable repository ID")
            if (entity_id in family_locations and item["field"] == "location"
                    and item["value"] != family_locations[entity_id]):
                raise DiscoveryConflict("the Skills index contains conflicting locations for the selected Git family")
            locations = ([item["value"]] if item["field"] == "location"
                         else item["value"] if item["field"] == "aliases" else [])
            for path in locations:
                if path in claimants and kind in claimants[path]:
                    if entity_id != claimants[path][kind]:
                        raise DiscoveryConflict("the Skills index contains a conflicting claim on an admitted checkout")
                    matched = True
                if path in git_roles and kind in {"clone", "worktree"} and entity_id not in git_roles[path]:
                    raise DiscoveryConflict("the Skills index contains a conflicting claim on an admitted Git location")
        if matched and {"identity", "parent"}.intersection(entity["conflicts"]):
            raise DiscoveryConflict("the Skills index contains an unresolved identity or parent conflict")
