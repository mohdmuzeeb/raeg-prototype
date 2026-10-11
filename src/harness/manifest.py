"""Provenance manifest for test and experiment runs (DESIGN §7)."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TREE_PATHS = ["src", "config", "tests", "scripts", "docker", "docker-compose.yml", "pyproject.toml", "uv.lock", "Makefile"]
IMAGES = ["raeg/python:dev"]


def _run(cmd: list[str]) -> str | None:
    try:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def source_tree_sha256() -> tuple[str, int]:
    """Hash of every source file (path + content), independent of git state."""
    digest = hashlib.sha256()
    count = 0
    files: list[Path] = []
    for entry in TREE_PATHS:
        p = ROOT / entry
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts)
    for f in sorted(files):
        digest.update(str(f.relative_to(ROOT)).encode() + b"\0")
        digest.update(hashlib.sha256(f.read_bytes()).digest())
        count += 1
    return digest.hexdigest(), count


def file_sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def docker_images() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for image in IMAGES:
        out[image] = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    out["compose_images"] = _run(["docker", "compose", "images", "--format", "json"])
    return out


def build_manifest(kind: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    tree_hash, n_files = source_tree_sha256()
    head = _run(["git", "rev-parse", "HEAD"])
    return {
        "kind": kind,
        "created_at": datetime.now(UTC).isoformat(),
        "source_tree_sha256": tree_hash,
        "source_files": n_files,
        "git": {
            "head": head,  # None until the first commit exists
            # results/ is excluded: a run's own evidence directory must not mark the tree dirty.
            "dirty": bool(_run(["git", "status", "--porcelain", "--", ".", ":(exclude)results"])) if head else None,
        },
        "uv_lock_sha256": file_sha256(ROOT / "uv.lock"),
        "requirements_lock_sha256": file_sha256(ROOT / "docker" / "requirements.lock"),
        "python": sys.version,
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu": _run(["sysctl", "-n", "machdep.cpu.brand_string"]),
            "mem_bytes": _run(["sysctl", "-n", "hw.memsize"]),
        },
        "docker": {
            "context": os.environ.get("DOCKER_CONTEXT"),
            "server_version": _run(["docker", "version", "--format", "{{.Server.Version}}"]),
            "images": docker_images(),
        },
        **(extra or {}),
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
