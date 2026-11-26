"""Read the live selected project through the Dev owner's Desktop connection."""

from dataclasses import dataclass
import time
from typing import Callable

from native_checkout_project import read_desktop_linked_project, read_desktop_project
from personal_checkout_inventory import Refused
from repo_platform.recovery import TransientNativeRead


@dataclass(frozen=True)
class DesktopReader:
    """Read current native tool results on every call, through trusted readers."""

    list_projects: Callable
    read_linked_thread: Callable

    def __call__(self, project_id, checkout, *, thread_id=None, deadline):
        try:
            catalog = self.list_projects(deadline=deadline)
        except (TimeoutError, ConnectionError) as error:
            raise TransientNativeRead("Codex Desktop could not return the live project catalog.") from error
        if thread_id is None:
            return read_desktop_project(catalog, project_id, checkout, observed_at=int(time.time()))
        projects = catalog.get("projects") if isinstance(catalog, dict) else None
        if not isinstance(projects, list):
            raise Refused("Codex Desktop did not return current project evidence")
        primary = [row for row in projects if isinstance(row, dict)
                   and row.get("projectId") == project_id and row.get("hostId") == "local"]
        if len(primary) != 1:
            raise Refused("Codex Desktop did not identify one local primary project for this worktree")
        project = read_desktop_project(catalog, project_id, primary[0].get("path"), observed_at=int(time.time()))
        try:
            membership, thread = self.read_linked_thread(thread_id, deadline=deadline)
        except (TimeoutError, ConnectionError) as error:
            raise TransientNativeRead("Codex Desktop could not verify the linked chat and checkout.") from error
        return read_desktop_linked_project(project, checkout, thread_id, membership, thread,
                                           observed_at=int(time.time()))
