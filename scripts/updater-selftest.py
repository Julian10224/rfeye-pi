#!/usr/bin/env python3
"""Regression tests for RF Eye OTA staging and replacement."""
from __future__ import annotations

import io
import os
import stat
import tempfile
import zipfile
from pathlib import Path

import updater


def make_zip(entries):
    bio=io.BytesIO()
    with zipfile.ZipFile(bio,"w") as z:
        for name,value in entries:
            if isinstance(value,zipfile.ZipInfo):
                z.writestr(value,b"target")
            else:
                z.writestr(name,value)
    return bio.getvalue()


def good_zip():
    return make_zip([
        ("rfeye/app.py","new app\n"),
        ("rfeye/config.py","new config\n"),
        ("rfeye/assets/icon.txt","asset\n"),
    ])


def expect_reject(data, root):
    try:
        updater.install_zip_bytes(data,app_root=str(root))
    except (ValueError,zipfile.BadZipFile):
        return
    raise AssertionError("unsafe/malformed update was accepted")


def main():
    with tempfile.TemporaryDirectory(prefix="rfeye-updater-test-") as td:
        home=Path(td)/"home";home.mkdir()
        old_home=os.environ.get("HOME")
        os.environ["HOME"]=str(home)
        try:
            root=Path(td)/"runtime";root.mkdir()
            (root/"app.py").write_text("old app\n")
            (root/"obsolete.py").write_text("stale\n")
            cache=root/"__pycache__"
            cache.mkdir()
            (cache/"root-owned-simulation.pyc").write_bytes(b"cache")
            cache.chmod(0o555)

            backup=Path(updater.install_zip_bytes(good_zip(),app_root=str(root)))
            assert (root/"app.py").read_text()=="new app\n"
            assert not (root/"obsolete.py").exists()
            assert (root/"assets"/"icon.txt").read_text()=="asset\n"
            assert (cache/"root-owned-simulation.pyc").exists()
            cache.chmod(0o755)
            assert (backup/"app.py").read_text()=="old app\n"
            assert (backup/"obsolete.py").read_text()=="stale\n"

            bad_names=[
                "../unpack_evil/pwn.txt",
                "/absolute/pwn.txt",
                "rfeye/../../pwn.txt",
                "other/app.py",
                "rfeye\\app.py",
            ]
            for name in bad_names:
                expect_reject(make_zip([(name,"x")]),root)

            # Explicit symlink member.
            zi=zipfile.ZipInfo("rfeye/link")
            zi.create_system=3
            zi.external_attr=(stat.S_IFLNK | 0o777) << 16
            expect_reject(make_zip([("ignored",zi)]),root)

            expect_reject(make_zip([("rfeye/config.py","no app")]),root)

            # A failed live copy must restore the previous runtime from backup.
            (root/"app.py").write_text("before failure\n")
            (root/"keep.py").write_text("keep me\n")
            original_copy=updater._copy_runtime
            state={"failed":False}
            def fail_new_source_once(source,dest):
                if Path(source).name=="rfeye" and not state["failed"]:
                    state["failed"]=True
                    raise OSError("synthetic copy failure")
                return original_copy(source,dest)
            updater._copy_runtime=fail_new_source_once
            try:
                try:
                    updater.install_zip_bytes(good_zip(),app_root=str(root))
                except OSError:
                    pass
                else:
                    raise AssertionError("synthetic copy failure did not propagate")
            finally:
                updater._copy_runtime=original_copy
            assert (root/"app.py").read_text()=="before failure\n"
            assert (root/"keep.py").read_text()=="keep me\n"
        finally:
            if old_home is None:
                os.environ.pop("HOME",None)
            else:
                os.environ["HOME"]=old_home

    print("RF Eye updater self-test: OK")


if __name__=="__main__":
    main()
