"""Small, CLI-owned helpers for durable dual-capture JSON reports."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Mapping
import uuid


def source_provenance(repository: Path) -> dict:
    """Fingerprint relevant workspace source, including uncommitted implementation."""
    repository = Path(repository)
    digest = hashlib.sha256()
    candidates = [repository / "demo.py", repository / "firmware/esp32/platformio.ini"]
    for folder in ("common", "laptop", "ultra96", "phone", "tools",
                   "firmware/esp32/include", "firmware/esp32/src",
                   "ios-visualizer/CommsNative/Sources"):
        directory = repository / folder
        if directory.is_dir():
            candidates.extend(path for path in directory.rglob("*")
                              if path.suffix in (".py", ".json", ".h", ".cpp", ".cs", ".swift")
                              and "tests" not in path.relative_to(repository).parts)
    for path in sorted(candidates):
        if path.is_file():
            digest.update(path.relative_to(repository).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    try:
        state = subprocess.run(["git", "status", "--porcelain"], cwd=str(repository),
                               capture_output=True, text=True, timeout=1, check=True)
        dirty = bool(state.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        dirty = None
    return {"worktree_dirty": dirty, "workspace_source_sha256": digest.hexdigest()}


class ReportReservation:
    """Own an exclusively-created report placeholder until atomic finalization."""

    def __init__(self, path: Path, token: str, identity: tuple[int, int]):
        self.path = path
        self._token = token
        self._identity = identity
        self._finalized = False

    def finalize(self, payload: str) -> None:
        if self._finalized:
            raise RuntimeError("report reservation is already finalized")
        if not isinstance(payload, str):
            raise TypeError("report payload must be text")
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise ValueError("report payload must contain a JSON object")

        current = self.path.lstat()
        if self.path.is_symlink() or (current.st_dev, current.st_ino) != self._identity:
            raise RuntimeError("report reservation was replaced")
        placeholder = json.loads(self.path.read_text(encoding="utf-8"))
        if placeholder.get("reservation_id") != self._token \
                or placeholder.get("status") != "incomplete":
            raise RuntimeError("report reservation contents changed")

        temporary = self.path.with_name(
            f".{self.path.name}.{self._token}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._finalized = True
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise


def reserve_report(path, metadata: Mapping[str, object]) -> ReportReservation:
    """Exclusively reserve an existing-parent path with an incomplete marker."""
    target = Path(path)
    parent = target.parent
    if not parent.exists():
        raise FileNotFoundError("report parent directory does not exist")
    if not parent.is_dir():
        raise NotADirectoryError("report parent is not a directory")
    token = uuid.uuid4().hex
    placeholder = dict(metadata)
    placeholder.update(status="incomplete", reservation_id=token)
    encoded = (json.dumps(placeholder, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(f"report path already exists: {target}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        stat = target.lstat()
        if target.is_symlink():
            raise RuntimeError("report reservation cannot be a symlink")
        return ReportReservation(target, token, (stat.st_dev, stat.st_ino))
    except BaseException:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise


def local_revision(repository: Path) -> str | None:
    """Return HEAD when available without making report creation depend on Git."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=str(repository),
            capture_output=True, text=True, timeout=1, check=True)
        revision = completed.stdout.strip()
        return revision if len(revision) == 40 else None
    except (OSError, subprocess.SubprocessError):
        return None
