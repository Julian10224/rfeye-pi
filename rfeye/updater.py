import hashlib
import json
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

        if backup.exists() or backup.is_symlink():
            _remove_path(backup)
        shutil.copytree(root, backup, ignore=shutil.ignore_patterns("__pycache__"))

        try:
            # Replace runtime contents instead of overlaying them so files
            # removed by a release cannot survive as stale executable modules.
            _clear_runtime(root)
            _copy_runtime(source, root)
        except Exception:
            # Best-effort in-process rollback for copy/delete failures.
            try:
                _clear_runtime(root)
                _copy_runtime(backup, root)
            except Exception:
                pass
            raise

    return str(backup)
