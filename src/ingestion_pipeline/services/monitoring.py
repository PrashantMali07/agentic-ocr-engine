"""Bounded, best-effort measurements of a local OCR child process."""

import asyncio
import os
import subprocess
from pathlib import Path


def peak_rss_bytes(pid: int) -> int | None:
    try:
        lines = (Path("/proc") / str(pid) / "status").read_text(encoding="ascii").splitlines()
    except (OSError, ValueError):
        return None
    for key in ("VmHWM:", "VmRSS:"):
        for line in lines:
            if line.startswith(key):
                try:
                    return int(line.split()[1]) * 1024
                except (IndexError, ValueError):
                    return None
    return None


def artifact_bytes(root: Path) -> int:
    """Measure a job's local artifact tree without following symlinks."""
    total = 0
    for directory, subdirs, filenames in os.walk(root, followlinks=False):
        subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()]
        for name in filenames:
            path = Path(directory) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


def _gpu_memory_bytes(pid: int) -> int | None:
    try:
        environment = os.environ.copy()
        environment.pop("INGESTION_API_KEY_SHA256_JSON", None)
        result = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=False, timeout=1, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    total_mib = 0
    found = False
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            if int(parts[0]) == pid:
                total_mib += int(parts[1])
                found = True
        except ValueError:
            continue
    return total_mib * 1024 * 1024 if found else None


async def gpu_memory_bytes(pid: int) -> int | None:
    return await asyncio.to_thread(_gpu_memory_bytes, pid)


def oom_events_path(pid: int) -> Path | None:
    """Locate cgroup-v2 OOM counters without trusting a path outside cgroupfs."""
    try:
        lines = (Path("/proc") / str(pid) / "cgroup").read_text(encoding="ascii").splitlines()
        group = next(line.split("::", 1)[1] for line in lines if line.startswith("0::"))
        root = Path("/sys/fs/cgroup").resolve()
        path = (root / group.lstrip("/") / "memory.events").resolve()
        return path if path.is_relative_to(root) else None
    except (OSError, ValueError, StopIteration):
        return None


def oom_kill_count(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            if line.startswith("oom_kill "):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None
