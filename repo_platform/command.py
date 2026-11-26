"""The adoption command invoked by a Dev-owned session."""

import argparse
from dataclasses import asdict, dataclass
from functools import partial
import json
import sys
from typing import Callable

from native_checkout_project import selection_digest
from personal_checkout_inventory import Inventory, Refused
from physical_checkout_binding import OwnerPhysicalBindings
from repo_platform.adoption import preview
from repo_platform.adoption_results import AdoptionResults
from repo_platform.discovery import SkillsDiscovery
from repo_platform.recovery import (
    DiscoveryRepairRequired, GitHubResetPending, RecoveryCase, check_github_cooldown, read_only_preview,
)
from repo_platform.skills_repair import SkillsRepair


@dataclass(frozen=True)
class OwnerSession:
    """Bindings supplied by the host entry point, outside the public arguments."""

    inventory: Inventory
    operation: str
    physical: OwnerPhysicalBindings
    read_native: Callable
    read_hosting: Callable
    read_delivery: Callable
    discovery: SkillsDiscovery
    results: AdoptionResults
    skills_repair: SkillsRepair | None = None


def run(argv, *, owner: OwnerSession, output=None, errors=None):
    """Run adoption within the caller's trusted owner session."""
    parser = argparse.ArgumentParser(prog="repo-platform")
    commands = parser.add_subparsers(dest="command", required=True)
    for verb, summary in (
        ("preview", "check one explicitly selected Codex checkout without recording an adoption"),
        ("adopt", "qualify and record one explicitly selected Codex checkout"),
    ):
        selection = commands.add_parser(verb, help=summary)
        selection.add_argument("checkout", help="absolute path of the selected checkout")
        selection.add_argument("--project-id", required=True, help="registered Codex project ID")
        selection.add_argument("--thread-id", help="Codex chat ID for a linked checkout")
        selection.add_argument("--repository-id", type=int, help="expected immutable GitHub repository ID")
        selection.add_argument("--expected-title", help="display title to compare with the live Codex project")
    args = parser.parse_args(argv)

    try:
        if not callable(owner.read_delivery):
            raise Refused("the Dev owner session has no delivery policy reader")
        if not isinstance(owner.discovery, SkillsDiscovery):
            raise Refused("the Dev owner session has no qualified Skills index reader")
        def verify(deadline, begin_attempt=None, *, results=None):
            return preview(
                owner.inventory, args.checkout, args.project_id, owner.operation,
                physical=owner.physical, read_native=partial(owner.read_native, thread_id=args.thread_id),
                read_hosting=owner.read_hosting, read_delivery=owner.read_delivery,
                deadline=deadline, thread_id=args.thread_id,
                expected_title=args.expected_title, expected_repository_id=args.repository_id,
                discovery=owner.discovery, results=results, begin_attempt=begin_attempt,
            )

        if args.command == "preview":
            def verify_preview(deadline):
                def begin_attempt(_version):
                    if isinstance(owner.results, AdoptionResults):
                        selected = selection_digest(args.project_id, args.checkout, args.thread_id)
                        check_github_cooldown(owner.results.path, selected)
                    return deadline

                return verify(deadline, begin_attempt)

            result = read_only_preview(verify_preview)
        else:
            result = _adopt(owner, args, verify)
    except DiscoveryRepairRequired as error:
        print(f"Checkout preview could not qualify discovery. {error}. "
              "Run adopt to request a refresh through the Skills owner.",
              file=errors if errors is not None else sys.stderr)
        return 1
    except GitHubResetPending as error:
        print(f"Checkout is waiting for GitHub. {error}", file=errors if errors is not None else sys.stderr)
        return 1
    except Refused as error:
        operation = "preview" if args.command == "preview" else "adoption"
        print(f"Checkout {operation} refused. {error}", file=errors if errors is not None else sys.stderr)
        return 1

    print(json.dumps(asdict(result), sort_keys=True), file=output if output is not None else sys.stdout)
    return 0


def _adopt(owner, args, verify):
    if not isinstance(owner.results, AdoptionResults):
        raise Refused("the Dev owner session has no private adoption result store")
    if owner.skills_repair is not None and not isinstance(owner.skills_repair, SkillsRepair):
        raise Refused("the Dev owner session has an invalid Skills repair binding")
    selected = selection_digest(args.project_id, args.checkout, args.thread_id)

    def refreshed_snapshot(version, expires, *, material=False):
        return owner.discovery.refreshed_reference(owner.inventory, version, owner.physical, expires, material=material)

    repair = None
    if owner.skills_repair is not None:
        repair = partial(owner.skills_repair.refresh, owner.discovery, inventory=owner.inventory,
                         operation=owner.operation, physical=owner.physical)

    recovery = RecoveryCase(owner.results.path, selected, owner.discovery.projection,
                            owner.discovery.skills_revision, owner.operation, refreshed_snapshot)
    return recovery.run(partial(verify, results=owner.results), repair_skills=repair)
