import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path


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
    req = urllib.request.Request(url, headers={"User-Agent": "RF-Eye-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if sha256:
        got = hashlib.sha256(data).hexdigest().lower()
        if got != sha256.lower():
            raise ValueError("SHA256 mismatch")
    return data


def install_zip_bytes(data, app_root="/opt/rfeye/rfeye"):
    root = Path(app_root)

    # RF Eye normally runs as an unprivileged desktop user. /opt/rfeye is
    # root-owned on the appliance, so a sibling backup such as
    # /opt/rfeye/rfeye.backup cannot be created by the running application.
    # Keep rollback data in the user's state directory instead.
    state_root = Path.home() / ".local" / "state" / "rfeye"
    state_root.mkdir(parents=True, exist_ok=True)
    backup = state_root / (root.name + ".backup")
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(root, backup, ignore=shutil.ignore_patterns("__pycache__"))

    with tempfile.TemporaryDirectory(prefix="rfeye-update-") as td:
        zpath = Path(td) / "update.zip"
        zpath.write_bytes(data)
        out = Path(td) / "unpack"
        out.mkdir()
        with zipfile.ZipFile(zpath, "r") as z:
            for member in z.infolist():
                target = (out / member.filename).resolve()
                if not str(target).startswith(str(out.resolve())):
                    raise ValueError("unsafe zip path")
            z.extractall(out)

        candidates = [p for p in out.rglob("app.py") if p.parent.name == "rfeye"]
        source = candidates[0].parent if candidates else out
        for src in source.iterdir():
            if src.name == "__pycache__":
                continue
            dst = root / src.name
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
    return str(backup)
