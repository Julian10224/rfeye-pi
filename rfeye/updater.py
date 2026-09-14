import hashlib
import json
import os
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


def version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("v").split("."))
    except Exception:
        return (0,)


def fetch_manifest(url, timeout=6):
    if not url:
        raise ValueError("update URL not configured")
    req = urllib.request.Request(url, headers={"User-Agent": "RF-Eye-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def download_update(url, sha256="", timeout=20):
    if not url:
        raise ValueError("update URL not configured")
    expected = str(sha256).strip().lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise ValueError("valid SHA256 is required")
    req = urllib.request.Request(url, headers={"User-Agent": "RF-Eye-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest().lower()
    if got != expected:
        raise ValueError("SHA256 mismatch")
    return data


def _remove_path(path):
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _fsync_path(path):
    """Flush one file, or one directory's entries, to the storage medium."""
    path = Path(path)
    is_dir = path.is_dir()
    if os.name == "nt":
        # Windows commits a file only through a writable handle and cannot
        # open a directory at all. The units run Linux; this keeps the test
        # suite honest on a development machine.
        if is_dir:
            return
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    else:
        flags = os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0) if is_dir else 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        if is_dir:
            # Not every platform can open a directory; the file data is what
            # matters most, and os.sync() at the end covers the rest.
            return
        raise
    try:
        os.fsync(fd)
    except OSError:
        if not is_dir:
            raise
    finally:
        os.close(fd)


def _fsync_tree(root):
    """Force everything under ``root`` onto the storage medium.

    A copy that has returned is still only in the page cache, and ext4 can
    leave freshly written file data there for about 30 seconds. On 14 September
    2026 a unit lost power inside that window after an update and came back
    with every runtime module at 0 bytes -- and every module of the backup the
    same update had just written, so it could neither start nor roll back.
    systemd restarted it 165 times behind a black panel.
    """
    root = Path(root)
    if not root.exists():
        return
    items = [root] if root.is_file() else sorted(
        p for p in root.rglob("*")
        if "__pycache__" not in p.parts and not p.is_symlink())
    for path in items:
        _fsync_path(path)
    if root.is_dir():
        _fsync_path(root)


def _runtime_intact(root):
    """True when ``root`` holds a runtime that could actually start.

    Every shipped module has content, so a 0-byte ``.py`` file is damage, not
    a release -- the signature of a copy that never reached the disk.
    """
    root = Path(root)
    app = root / "app.py"
    if not app.is_file() or app.stat().st_size == 0:
        return False
    return all(p.stat().st_size > 0 for p in root.glob("*.py"))


def _clear_runtime(root):
    root = Path(root)
    for child in list(root.iterdir()):
        # __pycache__ is disposable interpreter state, not shipped runtime.
        # Older/fresh installs may contain a root-owned cache created by an
        # installer syntax check; the unprivileged OTA must not fail on it.
        if child.name == "__pycache__":
            continue
        _remove_path(child)


def _copy_runtime(source, root):
    source = Path(source)
    root = Path(root)
    for src in source.iterdir():
        if src.name == "__pycache__":
            continue
        dst = root / src.name
        if src.is_symlink():
            raise ValueError("runtime update may not contain symlinks")
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _safe_extract_runtime(data, temp_root):
    """Validate and extract one RF Eye runtime ZIP into temp_root.

    OTA archives must contain only relative paths below the top-level rfeye
    directory. Reject traversal, absolute paths, backslashes and symlinks
    before extraction; do not rely on zipfile path sanitising.
    """
    temp_root = Path(temp_root)
    zpath = temp_root / "update.zip"
    zpath.write_bytes(data)
    out = temp_root / "unpack"
    out.mkdir()
    out_resolved = out.resolve()
    seen = set()

    with zipfile.ZipFile(zpath, "r") as z:
        for member in z.infolist():
            name = member.filename
            if not name or "\\" in name:
                raise ValueError("unsafe zip path")
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError("unsafe zip path")
            if not pure.parts or pure.parts[0] != "rfeye":
                raise ValueError("update must contain only rfeye runtime files")
            norm = pure.as_posix().rstrip("/")
            if norm in seen:
                raise ValueError("duplicate zip path")
            seen.add(norm)

            mode = (member.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError("update zip may not contain symlinks")

            target = (out / Path(*pure.parts)).resolve()
            if not target.is_relative_to(out_resolved):
                raise ValueError("unsafe zip path")
        z.extractall(out)

    source = out / "rfeye"
    if not source.is_dir() or not (source / "app.py").is_file():
        raise ValueError("update zip does not contain rfeye/app.py")
    return source


def install_zip_bytes(data, app_root="/opt/rfeye/rfeye"):
    root = Path(app_root)
    if not root.is_dir():
        raise ValueError("RF Eye runtime directory does not exist")

    state_root = Path.home() / ".local" / "state" / "rfeye"
    state_root.mkdir(parents=True, exist_ok=True)
    backup = state_root / (root.name + ".backup")

    # Validate and fully stage the archive before touching the live runtime.
    with tempfile.TemporaryDirectory(prefix="rfeye-update-") as td:
        source = _safe_extract_runtime(data, td)

        if _runtime_intact(root) or not _runtime_intact(backup):
            if backup.exists() or backup.is_symlink():
                _remove_path(backup)
            shutil.copytree(root, backup, ignore=shutil.ignore_patterns("__pycache__"))
        # Otherwise keep the backup. The live runtime is the damaged one, and
        # copying it over the last good copy destroys the only way back --
        # which is what repairing that unit did to its backup.
        #
        # The backup has to be on disk, not merely copied, before the live
        # runtime is touched.
        _fsync_tree(backup)
        _fsync_path(state_root)

        try:
            # Replace runtime contents instead of overlaying them so files
            # removed by a release cannot survive as stale executable modules.
            _clear_runtime(root)
            _copy_runtime(source, root)
            _fsync_tree(root)
        except Exception:
            # Best-effort in-process rollback for copy/delete failures.
            try:
                _clear_runtime(root)
                _copy_runtime(backup, root)
                _fsync_tree(root)
            except Exception:
                pass
            raise

    if hasattr(os, "sync"):
        os.sync()
    return str(backup)
