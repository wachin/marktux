"""Filesystem primitives shared by the whole core.

Everything here is Qt-free and safe to call from any thread. The two rules the
rest of the package relies on:

* every file that matters is written **atomically** (temp file in the same
  directory, fsync, ``os.replace``, directory fsync on POSIX);
* text is decoded and re-encoded with BOM and line-ending preservation, so a
  save never silently rewrites a note's bytes.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from .errors import JsonError, PathError
from .models import LineEnding

#: Files larger than this are compared by mtime only (external-change check).
MAX_COMPARE_BYTES = 50 * 1024 * 1024

_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_LE_BOM = b"\xff\xfe"
_UTF16_BE_BOM = b"\xfe\xff"
_UTF32_LE_BOM = b"\xff\xfe\x00\x00"
_UTF32_BE_BOM = b"\x00\x00\xfe\xff"

_BOMS = (
    (_UTF32_LE_BOM, "utf-32-le"),
    (_UTF32_BE_BOM, "utf-32-be"),
    (_UTF8_BOM, "utf-8"),
    (_UTF16_LE_BOM, "utf-16-le"),
    (_UTF16_BE_BOM, "utf-16-be"),
)


@dataclass(frozen=True, slots=True)
class DecodedText:
    """Result of decoding a file's bytes."""

    text: str
    encoding: str
    bom: bytes
    line_ending: LineEnding
    lossy: bool = False


# -- clocks and identifiers -------------------------------------------------

def now_ms() -> int:
    """Current wall-clock time in integer milliseconds (vxcore convention)."""
    return time.time_ns() // 1_000_000


def mtime_ms(path: str | os.PathLike[str]) -> int:
    """File modification time in integer milliseconds, or 0 when missing."""
    try:
        return os.stat(path).st_mtime_ns // 1_000_000
    except OSError:
        return 0


def new_uuid() -> str:
    return str(uuid.uuid4())


def stable_uuid(value: str) -> str:
    """Deterministic UUID for a filesystem identity (raw notebooks/nodes)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


# -- atomic writes ----------------------------------------------------------

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def atomic_write_bytes(
    path: str,
    data: bytes,
    *,
    fsync_dir: bool = True,
    retries: int = 3,
    mode: int | None = None,
) -> None:
    """Write ``data`` to ``path`` atomically.

    The temp file is created in the destination directory so ``os.replace`` is
    a same-filesystem rename. On failure the temp file is removed and the
    original file is untouched.
    """
    path = os.path.abspath(path)
    parent = os.path.dirname(path) or "."
    ensure_dir(parent)

    if mode is None:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            mode = None

    prefix = f".{os.path.basename(path)}.tmp-"
    fd, tmp = tempfile.mkstemp(prefix=prefix, dir=parent)
    try:
        if mode is not None:
            try:
                os.fchmod(fd, mode)
            except (AttributeError, OSError):
                pass
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        delay = 0.05
        for attempt in range(retries + 1):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: a reader (indexer, antivirus) can briefly hold the
                # destination open. Retry with backoff before giving up.
                if attempt >= retries:
                    raise
                time.sleep(delay)
                delay *= 2

        if fsync_dir and os.name != "nt":
            _fsync_directory(parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fsync_directory(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as error:  # EINVAL/ENOTSUP on some filesystems
        if error.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
            raise
    finally:
        os.close(fd)


def atomic_write_text(
    path: str,
    text: str,
    *,
    encoding: str = "utf-8",
    bom: bytes = b"",
    line_ending: LineEnding = LineEnding.LF,
    fsync_dir: bool = True,
) -> None:
    atomic_write_bytes(
        path,
        encode_text(text, encoding=encoding, bom=bom, line_ending=line_ending),
        fsync_dir=fsync_dir,
    )


def sweep_temp_files(directory: str, *, older_than_s: int = 3600) -> int:
    """Remove stale ``.<name>.tmp-*`` files left by a crashed write."""
    removed = 0
    cutoff = time.time() - older_than_s
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        name = entry.name
        if not (name.startswith(".") and ".tmp-" in name):
            continue
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat(
                follow_symlinks=False
            ).st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


# -- raw reads --------------------------------------------------------------

def read_bytes(path: str, *, max_size: int | None = None) -> bytes:
    if max_size is not None:
        size = os.path.getsize(path)
        if size > max_size:
            raise PathError(f"file is too large ({size} bytes)", path=path)
    with open(path, "rb") as handle:
        return handle.read()


# -- JSON -------------------------------------------------------------------

def read_json(path: str, *, default: Any = None, required: bool = False) -> Any:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        if required:
            raise JsonError("file not found", path=path) from None
        return default
    except (OSError, UnicodeError) as error:
        raise JsonError(f"cannot read JSON: {error}", path=path) from error
    except json.JSONDecodeError as error:
        raise JsonError(
            f"invalid JSON at line {error.lineno} column {error.colno}: {error.msg}",
            path=path,
        ) from error


def write_json(path: str, obj: Any, *, fsync_dir: bool = True) -> None:
    payload = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)
    atomic_write_text(path, payload + "\n", fsync_dir=fsync_dir)


# -- text codecs ------------------------------------------------------------

def detect_line_ending(text: str) -> LineEnding:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf == 0 and lf == 0 and cr == 0:
        return LineEnding.UNSPECIFIED
    if crlf >= lf and crlf >= cr:
        return LineEnding.CRLF
    if cr > lf:
        return LineEnding.CR
    return LineEnding.LF


def _split_bom(data: bytes) -> tuple[bytes, str | None, bytes]:
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return bom, encoding, data[len(bom):]
    return b"", None, data


def decode_text(
    data: bytes,
    *,
    encodings: Sequence[str] = ("utf-8",),
    fallback: str = "utf-8",
) -> DecodedText:
    """Decode file bytes, honouring a BOM first and then ``encodings``.

    Raises ``UnicodeDecodeError`` only when every candidate fails *and* the
    fallback fails too; callers normally pass ``errors="replace"`` through
    :func:`decode_text_lossy`.
    """
    bom, bom_encoding, body = _split_bom(data)
    candidates: list[str] = []
    if bom_encoding:
        candidates.append(bom_encoding)
    candidates.extend(encodings)
    if fallback not in candidates:
        candidates.append(fallback)

    for encoding in candidates:
        try:
            text = body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        return DecodedText(text, encoding, bom, detect_line_ending(text), False)
    raise UnicodeDecodeError(candidates[0], data, 0, len(data), "no encoding matched")


def decode_text_lossy(
    data: bytes,
    *,
    encodings: Sequence[str] = ("utf-8",),
    fallback: str = "utf-8",
) -> DecodedText:
    """Like :func:`decode_text` but never raises; marks the result lossy."""
    try:
        return decode_text(data, encodings=encodings, fallback=fallback)
    except UnicodeDecodeError:
        pass
    bom, _, body = _split_bom(data)
    text = body.decode(fallback, errors="replace")
    return DecodedText(text, fallback, bom, detect_line_ending(text), True)


def normalize_eol(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def apply_eol(text: str, ending: LineEnding) -> str:
    text = normalize_eol(text)
    if ending is LineEnding.CRLF:
        return text.replace("\n", "\r\n")
    if ending is LineEnding.CR:
        return text.replace("\n", "\r")
    return text


def encode_text(
    text: str,
    *,
    encoding: str = "utf-8",
    bom: bytes = b"",
    line_ending: LineEnding = LineEnding.LF,
) -> bytes:
    return bom + apply_eol(text, line_ending).encode(encoding)


# -- symlinks / reparse points ---------------------------------------------

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def is_symlink_or_reparse(path: str) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


__all__ = [
    "DecodedText",
    "MAX_COMPARE_BYTES",
    "apply_eol",
    "atomic_write_bytes",
    "atomic_write_text",
    "decode_text",
    "decode_text_lossy",
    "detect_line_ending",
    "encode_text",
    "ensure_dir",
    "is_symlink_or_reparse",
    "mtime_ms",
    "new_uuid",
    "normalize_eol",
    "now_ms",
    "read_bytes",
    "read_json",
    "stable_uuid",
    "sweep_temp_files",
    "write_json",
]
