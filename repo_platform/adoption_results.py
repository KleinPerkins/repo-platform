"""Private compare-and-swap publication for accepted checkout results."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import os
import re
import stat
import time

from lib.contracts import Refusal, encode, json_load
from lib.io_safe import directory, outside_repositories, private_dir, read_at
from lib.receipt_store import AdmittedPublisher
from personal_checkout_inventory import Refused


RESULT_SCHEMA = "repo-platform/adoption-result/v1"
MAX_RESULT_BYTES = 65536
SELECTION_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class AdoptionResults:
    """The owner-supplied directory for historical results, outside all repositories."""

    path: os.PathLike | str

    def _name(self, selection):
        if not isinstance(selection, str) or not SELECTION_DIGEST.fullmatch(selection):
            raise Refused("invalid native selection digest for the private adoption result")
        return "adoption-" + selection + ".json"

    def reference(self, selection):
        """Read an expected prior digest without treating that result as authority."""
        name = self._name(selection)
        parent = os.fspath(self.path)
        try:
            outside_repositories(parent)
            with directory(parent) as fd:
                private_dir(fd)
                return _reference(fd, name, selection)
        except Refusal as error:
            raise Refused("the private adoption result could not be read safely") from error

    def publish(self, result, expected, *, expires):
        """Replace the selected result only while its caller holds the Dev guard."""
        selection = result.native_selection_digest
        destination = os.path.join(os.fspath(self.path), self._name(selection))
        data = encode({"schema": RESULT_SCHEMA, "result": asdict(result)})
        if len(data) > MAX_RESULT_BYTES:
            raise Refused("the private adoption result exceeds its publication limit")
        switched = False
        try:
            # Reuse the Skills owner lock protocol. Its flock is released on
            # process exit, including an interruption before atomic replacement.
            with AdmittedPublisher(destination, MAX_RESULT_BYTES) as slot:
                if _reference(slot.fd, slot.name, selection) != expected:
                    raise Refused("another command published a newer adoption result; run a fresh preview")
                staged = "." + slot.name + ".pending"
                _discard_interrupted_stage(slot.fd, staged)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
                staged_fd = os.open(staged, flags, 0o600, dir_fd=slot.fd)
                try:
                    with os.fdopen(staged_fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if read_at(slot.fd, staged, MAX_RESULT_BYTES, True) != data:
                        raise Refused("the staged adoption result could not be verified")
                    if time.monotonic() >= expires:
                        raise Refused("the Dev adoption publication budget expired")
                    os.replace(staged, slot.name, src_dir_fd=slot.fd, dst_dir_fd=slot.fd)
                    switched = True
                    os.fsync(slot.fd)
                    if read_at(slot.fd, slot.name, MAX_RESULT_BYTES, True) != data:
                        raise Refused("the adoption result changed during publication")
                finally:
                    if not switched:
                        os.unlink(staged, dir_fd=slot.fd)
        except (Refusal, OSError, Refused) as error:
            if switched:
                raise Refused("the adoption publication switched but readback failed; recheck its private result") from error
            if isinstance(error, Refused):
                raise
            raise Refused("the private adoption result could not be published safely") from error


def _discard_interrupted_stage(fd, name):
    try:
        staged = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(staged.st_mode) or stat.S_IMODE(staged.st_mode) != 0o600
            or staged.st_uid != os.getuid() or staged.st_nlink != 1):
        raise Refused("the interrupted adoption staging file has an unsafe owner or type")
    os.unlink(name, dir_fd=fd)


def _reference(fd, name, selection):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    raw = read_at(fd, name, MAX_RESULT_BYTES, True)
    record = json_load(raw, MAX_RESULT_BYTES)
    if (not isinstance(record, dict) or record.get("schema") != RESULT_SCHEMA
            or not isinstance(record.get("result"), dict)
            or record["result"].get("native_selection_digest") != selection):
        raise Refused("the stored adoption result does not belong to this selection")
    return sha256(raw).hexdigest()
