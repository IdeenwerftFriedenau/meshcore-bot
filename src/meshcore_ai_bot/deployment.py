"""Offline deployment checkpoints and atomic release links. No service or radio control."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

UNITS = ("meshcore-ai-bot.service", "meshcore-dashboard.service")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_tree(source, target):
    if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("Backup directories must not contain symbolic links to external state")
    shutil.copytree(source, target, symlinks=True)
    for source_path in [source, *source.rglob("*")]:
        destination = target / source_path.relative_to(source)
        info = source_path.lstat()
        if os.geteuid() == 0:
            os.chown(destination, info.st_uid, info.st_gid, follow_symlinks=False)
        if destination.is_file() and not destination.is_symlink():
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
    sync_directory(target)


def atomic_link(target, link):
    temporary = link.with_name(".link-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
        sync_directory(link.parent)
    finally:
        temporary.unlink(missing_ok=True)


def backup(app_root, config_dir, state_dir, unit_dir, destination):
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    manifest = {
        "version": 1,
        "config_dir": str(config_dir),
        "state_dir": str(state_dir),
        "unit_dir": str(unit_dir),
        "current": os.readlink(app_root / "current") if (app_root / "current").is_symlink() else None,
        "directories": [],
        "units": [],
    }
    for name, source in [("config", config_dir), ("state", state_dir)]:
        if source.exists():
            copy_tree(source, destination / name)
            manifest["directories"].append(name)
    for unit in UNITS:
        source = unit_dir / unit
        if source.exists():
            shutil.copy2(source, destination / unit)
            manifest["units"].append(unit)
    path = destination / "manifest.json"
    with path.open("x") as stream:
        json.dump(manifest, stream)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(destination)
    sync_directory(destination.parent)
    atomic_link(destination, app_root / "last-backup")


def restore(app_root, config_dir, state_dir, unit_dir, source):
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest.get("version") != 1 or any(
        manifest.get(key) != str(value)
        for key, value in [("config_dir", config_dir), ("state_dir", state_dir), ("unit_dir", unit_dir)]
    ):
        raise ValueError("Backup paths do not match this installation")
    # Prepare all restored directories before replacing any live paths.
    staged = []
    try:
        for name, target in [("config", config_dir), ("state", state_dir)]:
            temp = target.with_name(".restore-" + uuid.uuid4().hex)
            if name in manifest["directories"]:
                copy_tree(source / name, temp)
            else:
                temp.mkdir(mode=0o700)
            staged.append((temp, target))
        for temp, target in staged:
            retired = target.with_name(".retired-" + uuid.uuid4().hex)
            if target.exists():
                os.replace(target, retired)
            try:
                os.replace(temp, target)
            except BaseException:
                if retired.exists():
                    os.replace(retired, target)
                raise
            sync_directory(target.parent)
            if retired.exists():
                shutil.rmtree(retired)
        for unit in UNITS:
            target = unit_dir / unit
            if unit in manifest["units"]:
                temp = target.with_name(".unit-" + uuid.uuid4().hex)
                shutil.copy2(source / unit, temp)
                os.replace(temp, target)
            else:
                target.unlink(missing_ok=True)
        if manifest["current"]:
            atomic_link(manifest["current"], app_root / "current")
        else:
            (app_root / "current").unlink(missing_ok=True)
        sync_directory(unit_dir)
    finally:
        for temp, _ in staged:
            if temp.exists():
                shutil.rmtree(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "restore", "activate"])
    parser.add_argument("--app-root", type=Path, default=Path("/opt/meshcore-ai-bot"))
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/meshcore-ai-bot"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/meshcore-ai-bot"))
    parser.add_argument("--unit-dir", type=Path, default=Path("/etc/systemd/system"))
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--release", type=Path)
    args = parser.parse_args()
    if args.action == "activate":
        if not args.release or not (args.release / ".venv/bin/meshcore-ai").is_file():
            parser.error("A validated release with an installed command is required")
        atomic_link(args.release, args.app_root / "current")
    else:
        checkpoint = args.backup or (args.app_root / "last-backup").resolve(strict=True)
        operation = backup if args.action == "backup" else restore
        operation(args.app_root, args.config_dir, args.state_dir, args.unit_dir, checkpoint)


if __name__ == "__main__":
    main()
