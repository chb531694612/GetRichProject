#!/usr/bin/env python3
"""Build a sanitized score-fourfold-v0.5.0 release tarball."""

from __future__ import annotations

import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.5.0"
OUT = ROOT / "dist" / f"score-fourfold-v{VERSION}.tar.gz"

INCLUDE_PATHS = [
    "pyproject.toml",
    "README.md",
    "UPDATE_TENCENT.md",
    "CURSOR_HANDOFF_V040.md",
    "Dockerfile",
    "docker-entrypoint.sh",
    "compose.yaml",
    ".dockerignore",
    ".gitignore",
    ".env.example",
    "caddy/Caddyfile",
    "examples",
    "scripts",
    "src",
    "tests",
]

FORBIDDEN_NAME_PARTS = (
    ".env",
    ".web-login-password",
    "root.key",
    "auth_code",
    "SMTP_AUTH_CODE",
)
FORBIDDEN_SUFFIXES = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".pem",
    ".key",
)


def is_forbidden(path: Path) -> bool:
    name = path.name
    lower = str(path).replace("\\", "/").lower()
    if name == ".env.example":
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if any(part.lower() in lower for part in FORBIDDEN_NAME_PARTS if part != ".env"):
        return True
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    if "caddy-data" in lower or "/data/" in f"/{lower}/" and path.suffix.lower() == ".db":
        return True
    return False


def iter_files() -> list[Path]:
    files: list[Path] = []
    for item in INCLUDE_PATHS:
        path = ROOT / item
        if not path.exists():
            raise SystemExit(f"missing required path: {item}")
        if path.is_file():
            files.append(path)
            continue
        for child in path.rglob("*"):
            if child.is_file():
                files.append(child)
    return files


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    selected = []
    for path in iter_files():
        if is_forbidden(path):
            raise SystemExit(f"refusing to package forbidden path: {path}")
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        selected.append(path)

    with tarfile.open(OUT, "w:gz") as archive:
        for path in selected:
            archive.add(path, arcname=str(path.relative_to(ROOT)).replace("\\", "/"))

    with tarfile.open(OUT, "r:gz") as archive:
        names = archive.getnames()
    joined = "\n".join(names)
    for needle in (".env\n", ".env/", "root.key", ".web-login-password", ".db", "SMTP_AUTH_CODE="):
        if needle in joined or any(name.endswith(".db") for name in names):
            if needle == ".env\n" and ".env.example" in joined and ".env\n" not in (joined + "\n"):
                continue
            if needle == ".env\n":
                offending = [name for name in names if name == ".env" or name.endswith("/.env")]
                if not offending:
                    continue
            if needle == ".db":
                offending = [name for name in names if name.endswith(".db")]
                if not offending:
                    continue
            raise SystemExit(f"package contains forbidden content matching {needle!r}: check {OUT}")

    print(f"wrote {OUT} with {len(names)} files")
    for name in names:
        print(name)


if __name__ == "__main__":
    main()
