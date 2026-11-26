"""Load the Dev-owned installation state for the repo-platform command."""

from hashlib import sha256
from pathlib import Path
import re
import sys
import time

from lib.contracts import INPUT_MAX, Refusal, json_load
from lib.io_safe import directory, outside_repositories, private_dir, read_at
from personal_checkout_inventory import Inventory, Refused
from physical_checkout_binding import OwnerPhysicalBinding, OwnerPhysicalBindings
from skills_inventory_projection import load_projection

from repo_platform.adoption_results import AdoptionResults
from repo_platform.command import OwnerSession, run
from repo_platform.discovery import SkillsDiscovery
from repo_platform.skills_repair import SkillsRepair


OWNER_SESSION_SCHEMA = "repo-platform/dev-owner-session/v1"
VERSIONED_OWNER_SESSION_SCHEMA = "repo-platform/dev-owner-session/v2"
OWNER_SESSION_FIELDS = {"schema", "operation", "skills_revision", "projection_digest", "physical_bindings"}
PHYSICAL_FIELDS = {"provider", "repository_id", "clone_id", "worktree_id", "locations"}


def read_owner_session(root, *, read_native, read_hosting, read_delivery):
    """Join owner-maintained state to the installed caller's trusted readers."""
    root = Path(root)
    discovery = root / "discovery"
    results = root / "results"
    try:
        outside_repositories(str(root))
        with directory(str(root)) as fd:
            private_dir(fd)
            descriptor = json_load(read_at(fd, "owner-session.json", INPUT_MAX, True), INPUT_MAX)
        physical = _descriptor_proofs(descriptor)
        settings_digest = descriptor.get("settings_digest")
        settings_name = f"settings-{settings_digest}.json" if settings_digest is not None else "settings.json"
        with directory(str(discovery)) as fd:
            private_dir(fd)
            settings = read_at(fd, settings_name, INPUT_MAX, True)
        with directory(str(results)) as fd:
            private_dir(fd)
        inventory = Inventory(root / "inventory")
        scope = inventory.current_owner_evidence(descriptor["operation"], deadline=time.monotonic() + 2)
        if scope.record is None:
            raise Refused("the installed owner session requires committed checkout evidence")
        record = json_load(scope.record, INPUT_MAX)
        committed = OwnerPhysicalBindings(tuple(physical.find(binding) for binding in scope.bindings))
        if committed.digest() != record["physical_binding_digest"]:
            raise Refused("the installed physical bindings differ from committed Dev evidence")
        observed_settings = sha256(settings).hexdigest()
        if settings_digest is not None and settings_digest != observed_settings:
            raise Refused("the installed Skills settings differ from the owner descriptor")
        projection = load_projection(discovery, descriptor["projection_digest"], scope.current, observed_settings)
        snapshot_name = (f"snapshot-{projection.manifest_digest()}.json"
                         if settings_digest is not None else "snapshot.json")
    except (OSError, Refusal) as error:
        raise Refused("the installed Dev owner state is unavailable or unverified") from error
    return OwnerSession(
        inventory, descriptor["operation"], physical, read_native, read_hosting, read_delivery,
        SkillsDiscovery(projection, discovery / snapshot_name, descriptor["skills_revision"]),
        AdoptionResults(results), SkillsRepair(settings, discovery),
    )


def _descriptor_proofs(descriptor):
    if not isinstance(descriptor, dict):
        raise Refused("the installed Dev owner descriptor has an invalid shape")
    schema = descriptor.get("schema")
    if schema == VERSIONED_OWNER_SESSION_SCHEMA:
        digest = descriptor.get("settings_digest")
        if (descriptor.keys() != OWNER_SESSION_FIELDS | {"settings_digest"} or not isinstance(digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", digest) is None):
            raise Refused("the installed Dev owner descriptor has invalid Skills settings")
    elif schema != OWNER_SESSION_SCHEMA or descriptor.keys() != OWNER_SESSION_FIELDS:
        raise Refused("the installed Dev owner descriptor has an invalid shape")
    if (not isinstance(descriptor["operation"], str) or not 0 < len(descriptor["operation"]) <= 128
            or not isinstance(descriptor["skills_revision"], str)
            or not re.fullmatch(r"[a-f0-9]{40}", descriptor["skills_revision"])
            or not isinstance(descriptor["physical_bindings"], list) or not descriptor["physical_bindings"]):
        raise Refused("the installed Dev owner descriptor has an invalid shape")
    proofs = []
    for row in descriptor["physical_bindings"]:
        if (not isinstance(row, dict) or row.keys() != PHYSICAL_FIELDS
                or type(row["repository_id"]) is not int or row["repository_id"] < 1
                or any(not isinstance(row[key], str) or not row[key] for key in ("provider", "clone_id", "worktree_id"))
                or not isinstance(row["locations"], list)
                or any(not isinstance(location, list) or len(location) != 4
                       or not all(isinstance(value, str) for value in location[:2])
                       or not all(type(value) is int and value > 0 for value in location[2:])
                       for location in row["locations"])):
            raise Refused("the installed Dev physical binding record has an invalid shape")
        locations = tuple(tuple(location) for location in row["locations"])
        proofs.append(OwnerPhysicalBinding(**(row | {"locations": locations})))
    return OwnerPhysicalBindings(tuple(proofs))


def run_installed(argv, *, owner_state, read_native, read_hosting, read_delivery, output=None, errors=None):
    """Invoke the command from the Dev owner's installed launcher."""
    try:
        owner = read_owner_session(owner_state, read_native=read_native,
                                   read_hosting=read_hosting, read_delivery=read_delivery)
    except Refused as error:
        print(f"Repo-platform could not open its owner session. {error}",
              file=errors if errors is not None else sys.stderr)
        return 1
    return run(argv, owner=owner, output=output, errors=errors)
