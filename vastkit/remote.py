"""SSH / file-transfer / detached-job helpers for Vast.ai instances.

Vast SSH sessions drop; hosts are heterogeneous (rsync may be missing from
minimal images). Everything here is built for that reality:

- non-interactive ssh with keepalives and no host-key prompts
- ``push``/``pull`` prefer rsync but transparently fall back to tar-over-ssh
- long jobs run detached under ``nohup`` with logs + exit codes on the remote
  filesystem, so they survive disconnects and are pollable
"""

from __future__ import annotations

import base64
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

JOBS_DIR = "/tmp/vastkit-jobs"
DEFAULT_PUSH_EXCLUDES = [".git", ".venv", "venv", "__pycache__", "*.pyc", ".DS_Store", "*.egg-info"]
_RSYNC_MISSING_MARKERS = ("command not found", "no such file", "not found")


class RemoteError(RuntimeError):
    """A remote operation failed."""


def _ssh_options(key: str = "", connect_timeout: int = 15) -> List[str]:
    opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=8",
    ]
    if key:
        opts += ["-i", str(Path(key).expanduser())]
    return opts


def ssh_argv(host: str, port: int, key: str = "", connect_timeout: int = 15) -> List[str]:
    return ["ssh", "-p", str(port)] + _ssh_options(key, connect_timeout) + [f"root@{host}"]


def env_prefix(env: Optional[Dict[str, str]]) -> str:
    if not env:
        return ""
    parts = [f"export {k}={shlex.quote(str(v))};" for k, v in env.items()]
    return " ".join(parts) + " "


def wrap_command(command: str, env: Optional[Dict[str, str]] = None, cwd: str = "") -> str:
    prefix = env_prefix(env)
    if cwd:
        prefix += f"cd {shlex.quote(cwd)} && "
    return prefix + command


def check_ssh(host: str, port: int, key: str = "", timeout: int = 12) -> bool:
    argv = ssh_argv(host, port, key, connect_timeout=timeout) + ["echo ok"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 8)
        return proc.returncode == 0 and "ok" in proc.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def run(
    host: str,
    port: int,
    command: str,
    key: str = "",
    env: Optional[Dict[str, str]] = None,
    cwd: str = "",
    capture: bool = False,
    check: bool = True,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run a command remotely. ``capture=False`` streams output to the terminal."""
    argv = ssh_argv(host, port, key) + [wrap_command(command, env, cwd)]
    try:
        proc = subprocess.run(argv, capture_output=capture, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError(
            f"remote command timed out after {timeout}s: {command[:120]}") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:800] if capture else ""
        raise RemoteError(
            f"remote command failed (exit {proc.returncode}): {command[:200]}"
            + (f"\n{detail}" if detail else "")
        )
    return proc


def wait_ssh(
    host: str,
    port: int,
    key: str = "",
    timeout: float = 600,
    interval: float = 10,
    log: Optional[Callable[[str], None]] = None,
) -> bool:
    """Poll until SSH answers. Returns True if it became reachable in time."""
    start = time.time()
    while time.time() - start < timeout:
        if check_ssh(host, port, key):
            return True
        if log:
            log(f"[{int(time.time() - start)}s] waiting for SSH on {host}:{port} ...")
        time.sleep(interval)
    return False


def interactive(host: str, port: int, key: str = "") -> int:
    return subprocess.call(ssh_argv(host, port, key))


# ------------------------------------------------------------------ detached

def _job_dir(job: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in job)
    return f"{JOBS_DIR}/{safe}"


def start_detached(
    host: str,
    port: int,
    command: str,
    job: str,
    key: str = "",
    env: Optional[Dict[str, str]] = None,
    cwd: str = "",
) -> str:
    """Start ``command`` under nohup, detached from the SSH session.

    The script is shipped base64-encoded (immune to quoting issues). Artifacts
    live in ``/tmp/vastkit-jobs/<job>/``: ``cmd.sh``, ``out.log``, ``pid``,
    ``exitcode`` (written when the command finishes).
    """
    jd = _job_dir(job)
    script = "#!/bin/bash\n" + wrap_command(command, env, cwd) + "\n"
    b64 = base64.b64encode(script.encode()).decode()
    # Braces are load-bearing: without them, `... && nohup ... & echo $!`
    # backgrounds the whole && chain and the pid write races the mkdir.
    setup = (
        f"mkdir -p {jd} && rm -f {jd}/exitcode {jd}/out.log && "
        f"echo {b64} | base64 -d > {jd}/cmd.sh && chmod +x {jd}/cmd.sh && "
        f"{{ nohup bash -c '{jd}/cmd.sh; echo $? > {jd}/exitcode' "
        f"> {jd}/out.log 2>&1 & echo $! > {jd}/pid; }}"
    )
    run(host, port, setup, key=key, capture=True)
    return jd


def poll_detached(host: str, port: int, job: str, key: str = "") -> Optional[int]:
    """Exit code of a detached job, or None while still running."""
    jd = _job_dir(job)
    proc = run(
        host, port, f"cat {jd}/exitcode 2>/dev/null || true",
        key=key, capture=True, check=False,
    )
    text = (proc.stdout or "").strip()
    if not text:
        return None
    try:
        return int(text.splitlines()[-1])
    except ValueError:
        return None


def read_log(
    host: str, port: int, job: str, key: str = "", offset: int = 0
) -> Tuple[str, int]:
    """Read new bytes of a detached job's log from ``offset``; returns (text, new_offset)."""
    jd = _job_dir(job)
    proc = run(
        host, port,
        f"tail -c +{offset + 1} {jd}/out.log 2>/dev/null || true",
        key=key, capture=True, check=False,
    )
    text = proc.stdout or ""
    return text, offset + len(text.encode())


def list_jobs(host: str, port: int, key: str = "") -> List[dict]:
    cmd = (
        f"for d in {JOBS_DIR}/*/; do [ -d \"$d\" ] || continue; "
        "name=$(basename \"$d\"); code=$(cat \"$d/exitcode\" 2>/dev/null); "
        "echo \"$name|${code:-running}\"; done 2>/dev/null || true"
    )
    proc = run(host, port, cmd, key=key, capture=True, check=False)
    jobs = []
    for line in (proc.stdout or "").splitlines():
        if "|" in line:
            name, status = line.rsplit("|", 1)
            jobs.append({"job": name.strip(), "status": status.strip()})
    return jobs


# ------------------------------------------------------------------ transfer

def _rsync_transport(port: int, key: str = "") -> str:
    parts = (
        f"ssh -p {port} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        "-o LogLevel=ERROR -o ServerAliveInterval=15 -o ServerAliveCountMax=8"
    )
    if key:
        parts += f" -i {Path(key).expanduser()}"
    return parts


def _rsync_unusable(returncode: int, stderr: str) -> bool:
    low = (stderr or "").lower()
    return returncode in (12, 127, 255) and any(m in low for m in _RSYNC_MISSING_MARKERS)


def push(
    host: str,
    port: int,
    local_path: str,
    remote_path: str,
    key: str = "",
    excludes: Optional[List[str]] = None,
    delete: bool = False,
    quiet: bool = False,
) -> None:
    """Upload a directory (or file). rsync first, tar-over-ssh fallback."""
    local = Path(local_path).expanduser()
    if not local.exists():
        raise RemoteError(f"local path does not exist: {local}")
    excludes = DEFAULT_PUSH_EXCLUDES if excludes is None else excludes

    if local.is_dir():
        src, dst = str(local).rstrip("/") + "/", f"root@{host}:{remote_path.rstrip('/')}/"
        mkdir_target = remote_path.rstrip("/")
    else:
        src, dst = str(local), f"root@{host}:{remote_path}"
        mkdir_target = str(Path(remote_path).parent)

    argv = ["rsync", "-az", "--partial"]
    if not quiet:
        argv.append("--info=progress2") if _rsync_supports_info() else argv.append("-v")
    if delete and local.is_dir():
        argv.append("--delete")
    for pat in excludes:
        argv += ["--exclude", pat]
    argv += [
        "-e", _rsync_transport(port, key),
        "--rsync-path", f"mkdir -p {shlex.quote(mkdir_target)} && rsync",
        src, dst,
    ]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode == 0:
        return
    if not _rsync_unusable(proc.returncode, proc.stderr):
        raise RemoteError(
            f"rsync push failed (exit {proc.returncode}):\n{proc.stderr.strip()[:800]}")

    # Fallback: tar over ssh (remote lacks rsync)
    print("rsync unavailable on remote — falling back to tar-over-ssh", file=sys.stderr)
    if local.is_dir():
        tar_cmd = ["tar", "-czf", "-"]
        for pat in excludes:
            tar_cmd += ["--exclude", pat]
        tar_cmd += ["-C", str(local), "."]
    else:
        tar_cmd = ["tar", "-czf", "-", "-C", str(local.parent), local.name]
    target_q = shlex.quote(mkdir_target)
    remote_cmd = f"mkdir -p {target_q} && tar -xzf - -C {target_q}"
    tar = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE)
    ssh = subprocess.run(
        ssh_argv(host, port, key) + [remote_cmd], stdin=tar.stdout,
        capture_output=True, text=True,
    )
    tar.stdout.close()  # type: ignore[union-attr]
    tar.wait()
    if tar.returncode != 0 or ssh.returncode != 0:
        raise RemoteError(f"tar push failed: {(ssh.stderr or '').strip()[:800]}")


def pull(
    host: str,
    port: int,
    remote_path: str,
    local_path: str,
    key: str = "",
    quiet: bool = False,
) -> None:
    """Download a remote directory (or file). rsync first, tar fallback."""
    local = Path(local_path).expanduser()
    local.mkdir(parents=True, exist_ok=True) if remote_path.endswith("/") else local.parent.mkdir(
        parents=True, exist_ok=True
    )

    argv = ["rsync", "-az", "--partial"]
    if not quiet:
        argv.append("--info=progress2") if _rsync_supports_info() else argv.append("-v")
    argv += ["-e", _rsync_transport(port, key), f"root@{host}:{remote_path}", str(local)]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode == 0:
        return
    if not _rsync_unusable(proc.returncode, proc.stderr):
        raise RemoteError(
            f"rsync pull failed (exit {proc.returncode}):\n{proc.stderr.strip()[:800]}")

    print("rsync unavailable on remote — falling back to tar-over-ssh", file=sys.stderr)
    rp = remote_path.rstrip("/")
    local.mkdir(parents=True, exist_ok=True)
    remote_cmd = (
        f"if [ -d {shlex.quote(rp)} ]; then tar -czf - -C {shlex.quote(rp)} .; "
        f"else tar -czf - -C {shlex.quote(str(Path(rp).parent))} {shlex.quote(Path(rp).name)}; fi"
    )
    ssh = subprocess.Popen(ssh_argv(host, port, key) + [remote_cmd], stdout=subprocess.PIPE)
    tar = subprocess.run(["tar", "-xzf", "-", "-C", str(local)], stdin=ssh.stdout)
    ssh.stdout.close()  # type: ignore[union-attr]
    ssh.wait()
    if ssh.returncode != 0 or tar.returncode != 0:
        raise RemoteError("tar pull failed")


_rsync_info_cache: Optional[bool] = None


def _rsync_supports_info() -> bool:
    """openrsync (macOS default) lacks --info=progress2; probe once."""
    global _rsync_info_cache
    if _rsync_info_cache is None:
        try:
            out = subprocess.run(
                ["rsync", "--version"], capture_output=True, text=True
            ).stdout.lower()
            _rsync_info_cache = "openrsync" not in out
        except OSError:
            _rsync_info_cache = False
    return _rsync_info_cache
