"""The adoption command invoked by a Dev-owned session."""

import argparse
from dataclasses import asdict, dataclass
from functools import partial
import json
import sys
import time
from typing import Callable

from personal_checkout_inventory import Inventory, Refused
from physical_checkout_binding import OwnerPhysicalBindings
from repo_platform.adoption import preview
from repo_platform.adoption_results import AdoptionResults
from repo_platform.discovery import SkillsDiscovery


ADOPTION_BUDGET_SECONDS = 90


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


def run(argv, *, owner: OwnerSession, output=None, errors=None):
    """Run the read-only command within its caller's trusted owner session."""
    parser = argparse.ArgumentParser(prog="repo-platform")
    commands = parser.add_subparsers(dest="command", required=True)
    adopt = commands.add_parser("adopt", help="qualify and record one explicitly selected Codex checkout")
    adopt.add_argument("checkout", help="absolute path of the selected checkout")
    adopt.add_argument("--project-id", required=True, help="registered Codex project ID")
    adopt.add_argument("--thread-id", help="Codex chat ID for a linked checkout")
    adopt.add_argument("--repository-id", type=int, help="expected immutable GitHub repository ID")
    adopt.add_argument("--expected-title", help="display title to compare with the live Codex project")
    args = parser.parse_args(argv)

    try:
        if not callable(owner.read_delivery):
            raise Refused("the Dev owner session has no delivery policy reader")
        if not isinstance(owner.discovery, SkillsDiscovery):
            raise Refused("the Dev owner session has no qualified Skills index reader")
        if not isinstance(owner.results, AdoptionResults):
            raise Refused("the Dev owner session has no private adoption result store")
        result = preview(
            owner.inventory, args.checkout, args.project_id, owner.operation,
            physical=owner.physical, read_native=partial(owner.read_native, thread_id=args.thread_id),
            read_hosting=owner.read_hosting, read_delivery=owner.read_delivery,
            deadline=time.monotonic() + ADOPTION_BUDGET_SECONDS, thread_id=args.thread_id,
            expected_title=args.expected_title, expected_repository_id=args.repository_id,
            discovery=owner.discovery, results=owner.results,
        )
    except Refused as error:
        print(f"Checkout adoption refused. {error}", file=errors if errors is not None else sys.stderr)
        return 1

    print(json.dumps(asdict(result), sort_keys=True), file=output if output is not None else sys.stdout)
    return 0
