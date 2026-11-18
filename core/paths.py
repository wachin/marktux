"""Notebook-relative path algebra and containment checks.

vxcore paths are notebook-relative, ``/``-separated, with ``""`` meaning the
notebook root. Every name that comes from a ``vx.json`` file is untrusted: it
may be absolute, contain ``..`` or a NUL byte. Nothing in the core joins such a
name to a root without going through :func:`resolve_inside`, which canonicalises
both sides and verifies containment.
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Iterable

from .errors import InvalidNameError, NodeNotFoundError, PathEscapeError

ROOT = ""

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_WINDOWS_FORBIDDEN = set('<>:"|?*')


def normalize_rel(path: str) -> str:
    """Return the canonical notebook-relative form of ``path``.

    ``""`` and ``"."`` both mean the root. Backslashes are accepted as
    separators (they appear in hand-edited configs) and normalised to ``/``.
    Raises :class:`PathEscapeError` for absolute paths and ``..`` escapes.
    """
    if not path or path in (".", "./"):
        return ROOT
    text = path.replace("\\", "/")
    if "\x00" in text:
        raise PathEscapeError("path contains a NUL byte", path=path)
    if text.startswith("/") or _DRIVE_RE.match(text):
        raise PathEscapeError("absolute paths are not allowed", path=path)

    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise PathEscapeError("path escapes the notebook root", path=path)
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def split_rel(path: str) -> tuple[str, str]:
    """Split a relative path into ``(parent, name)``; root yields ``("", "")``."""
    rel = normalize_rel(path)
    if not rel:
        return ROOT, ""
    parent, _, name = rel.rpartition("/")
    return parent, name


def join_rel(*parts: str) -> str:
    joined = "/".join(part for part in parts if part not in ("", ".", None))
    return normalize_rel(joined)


def absolute(root: str, rel: str) -> str:
    """Absolute filesystem path for ``rel`` inside ``root`` (no containment check)."""
    rel = normalize_rel(rel)
    if not rel:
        return root
    return os.path.join(root, *rel.split("/"))


def _canonical(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def is_inside(root: str, path: str) -> bool:
    root_real = _canonical(root)
    path_real = _canonical(path)
    try:
        return os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:  # different drives on Windows
        return False


def relative_to(root: str, path: str) -> str:
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    if rel == ".":
        return ROOT
    return rel.replace(os.sep, "/")


def resolve_inside(
    root: str,
    rel: str,
    *,
    must_exist: bool = False,
    reject_symlinks: bool = False,
) -> str:
    """Resolve ``rel`` inside ``root``, refusing anything that escapes.

    Both sides are canonicalised, so a symlinked notebook root still compares
    correctly. ``reject_symlinks`` additionally refuses any component that is a
    link or Windows reparse point (used for delete/rename/move).
    """
    from .fsutil import is_symlink_or_reparse  # local: avoids a cycle

    rel = normalize_rel(rel)
    root_abs = os.path.abspath(root)
    target = absolute(root_abs, rel)

    if not is_inside(root_abs, target):
        raise PathEscapeError("path escapes the notebook root", path=rel)

    if reject_symlinks and rel:
        current = root_abs
        for part in rel.split("/"):
            current = os.path.join(current, part)
            if os.path.lexists(current) and is_symlink_or_reparse(current):
                raise PathEscapeError("path crosses a symbolic link", path=rel)

    if must_exist and not os.path.lexists(target):
        raise NodeNotFoundError("node does not exist", path=rel)
    return target


# -- names ------------------------------------------------------------------

def validate_name(name: str, *, windows: bool = True) -> str:
    """Validate a name for a *new* node.

    Existing on-disk names are never validated: a notebook may legitimately
    contain a name that is awkward on another platform, and refusing to open it
    would be worse than showing it.
    """
    if not name or not name.strip():
        raise InvalidNameError("name must not be empty", path=name)
    if name in (".", ".."):
        raise InvalidNameError("name must not be '.' or '..'", path=name)
    if "/" in name or "\\" in name:
        raise InvalidNameError("name must not contain a path separator", path=name)
    if "\x00" in name:
        raise InvalidNameError("name must not contain a NUL byte", path=name)
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        raise InvalidNameError("name must not contain control characters", path=name)
    if windows:
        if any(ch in _WINDOWS_FORBIDDEN for ch in name):
            raise InvalidNameError("name contains a reserved character", path=name)
        stem = name.split(".")[0].strip().lower()
        if stem in _WINDOWS_RESERVED:
            raise InvalidNameError("name is reserved on Windows", path=name)
        if name[-1] in (" ", "."):
            raise InvalidNameError("name must not end with a space or dot", path=name)
    return name


def casefold_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def suffix_of(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return ""
    return ext.lower()


def split_name(name: str) -> tuple[str, str]:
    """Split into ``(stem, ".ext")`` preserving dotfiles."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return name, ""
    return stem, "." + ext


def available_name(
    folder_abs: str,
    desired: str,
    *,
    existing: Iterable[str] = (),
    limit: int = 10_000,
) -> str:
    """Return ``desired`` or ``stem_N.ext`` that collides with nothing.

    Checks the directory **and** ``existing`` (names known to the index but not
    yet on disk), case-insensitively, so a case-insensitive filesystem cannot
    produce a silent overwrite.
    """
    taken = {casefold_key(name) for name in existing}
    try:
        taken.update(casefold_key(entry.name) for entry in os.scandir(folder_abs))
    except OSError:
        pass

    if casefold_key(desired) not in taken:
        return desired

    stem, ext = split_name(desired)
    for index in range(1, limit + 1):
        candidate = f"{stem}_{index}{ext}"
        if casefold_key(candidate) not in taken:
            return candidate
    raise InvalidNameError("could not find an available name", path=desired)


__all__ = [
    "ROOT",
    "absolute",
    "available_name",
    "casefold_key",
    "is_inside",
    "join_rel",
    "normalize_rel",
    "relative_to",
    "resolve_inside",
    "split_name",
    "split_rel",
    "suffix_of",
    "validate_name",
]
