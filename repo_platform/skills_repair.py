"""Regenerate discovery through the Dev-admitted Skills publisher."""

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import time

from checkout_git_config import owner_config_proofs
from lib.contracts import INPUT_MAX, Refusal, json_load
from lib.engine import refresh_admitted
from personal_checkout_inventory import Refused
from physical_checkout_binding import OwnerPhysicalBindings
from skills_inventory_projection import guarded_admission, project_inventory, store_projection


@dataclass(frozen=True)
class SkillsRepair:
    """Exact refresh settings and private state supplied by the Dev host owner."""

    trusted_settings: bytes
    state_directory: os.PathLike | str

    def refresh(self, discovery, *, inventory, operation, physical, version, failed_snapshot, expires):
        if discovery.projection.version != version or sha256(self.trusted_settings).hexdigest() != (
            discovery.projection.settings_digest
        ):
            raise Refused("the Dev owner must supply current Skills refresh settings for this projection")
        if time.monotonic() >= expires:
            raise Refused("the adoption budget elapsed before the Skills refresh")

        try:
            scope = inventory.current_owner_evidence(operation, deadline=min(expires, time.monotonic() + 2))
            if scope.current != version or scope.record is None:
                raise Refused("Dev authority changed before the Skills refresh")
            record = json_load(scope.record, INPUT_MAX)
            paths = {binding.checkout for binding in scope.bindings}
            proofs = OwnerPhysicalBindings(tuple(physical.find(binding) for binding in scope.bindings))
            if record["physical_binding_digest"] != proofs.digest():
                raise Refused("the Skills refresh physical evidence differs from committed Dev authority")
            config_proofs = owner_config_proofs(record, paths)
            projection = project_inventory(inventory, version, self.trusted_settings,
                                           deadline=min(expires, time.monotonic() + 2))
            if projection != discovery.projection:
                raise Refused("the verified Skills projection changed before the refresh")
            roots = json_load(projection.config_bytes, INPUT_MAX)["roots"]
            if not paths.issubset(root["path"] for root in roots):
                raise Refused("the Skills projection lacks an admitted checkout in this owner operation")

            existing = discovery.refreshed_reference(inventory, version, physical, expires)
            if existing is not None and existing != failed_snapshot:
                return existing

            def confirm_owner_evidence():
                current = inventory.current_owner_evidence(operation,
                                                           deadline=min(expires, time.monotonic() + 2))
                if current.current != version or current.digest != scope.digest:
                    raise Refused("Dev authority changed during the Skills refresh")

            stored = store_projection(Path(self.state_directory), projection)
            admitted = guarded_admission(inventory, projection, discovery.skills_revision, physical=physical,
                                         owner_config_digests=config_proofs)
            receipt = refresh_admitted(str(stored.config), os.fspath(discovery.cache), admitted,
                                      before_replace=confirm_owner_evidence)
        except Refusal as error:
            raise Refused(f"the owner-supported Skills refresh refused ({error.code})") from error
        observed = discovery.refreshed_reference(inventory, version, physical, expires)
        if observed is None or observed == failed_snapshot or observed != receipt["snapshot_digest"]:
            raise Refused("the refreshed Skills receipt could not be independently verified")
        return observed
