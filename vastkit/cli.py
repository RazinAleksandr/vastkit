"""vastkit command-line interface.

Design rules: every command supports ``--json`` for scripting; humans get
aligned tables; anything that spends or destroys asks for confirmation unless
``--yes``; exit codes are meaningful (0 ok, 1 failure, 130 interrupted).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Set

from . import __version__, remote
from .api import VastAPI, VastAPIError
from .lifecycle import (
    DEFAULT_IMAGE,
    RentSpec,
    destroy_instance,
    find_ssh_key,
    rent,
    resolve_instance,
)
from .models import Instance, Offer
from .query import build_query, expand_geolocation, match_geolocation
from .ranking import SORT_MODES, cap_bandwidth_cost, rank_offers
from .remote import RemoteError

# --------------------------------------------------------------------- style

_BOLD, _DIM, _RED, _GREEN, _YELLOW, _CYAN = "1", "2", "31", "32", "33", "36"


def _use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, code: str) -> str:
    if not _use_color():
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


_STATUS_COLORS = {"running": _GREEN, "loading": _YELLOW, "created": _YELLOW,
                  "starting": _YELLOW, "exited": _RED, "stopped": _RED, "offline": _RED}


def status_str(status: str) -> str:
    return paint(status or "unknown", _STATUS_COLORS.get(status, _DIM))


def money(x: float) -> str:
    return f"${x:,.3f}" if abs(x) < 10 else f"${x:,.2f}"


def fmt_table(headers: List[str], rows: List[List[str]], rjust: Optional[Set[int]] = None) -> str:
    """Plain-text aligned table. ANSI codes in cells are width-compensated."""
    import re

    ansi = re.compile(r"\x1b\[[0-9;]*m")

    def vis(s: str) -> int:
        return len(ansi.sub("", s))

    cols = len(headers)
    widths = [vis(h) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], vis(row[i]))

    def fmt_row(cells: List[str], bold: bool = False) -> str:
        out = []
        for i, cell in enumerate(cells):
            pad = widths[i] - vis(cell)
            text = paint(cell, _BOLD) if bold else cell
            out.append(" " * pad + text if (rjust and i in rjust) else text + " " * pad)
        return "  ".join(out).rstrip()

    lines = [fmt_row(headers, bold=True),
             paint("  ".join("-" * w for w in widths), _DIM)]
    lines += [fmt_row(r) for r in rows]
    return "\n".join(lines)


def die(message: str, code: int = 1) -> NoReturn:  # noqa: F821
    print(paint(f"error: {message}", _RED), file=sys.stderr)
    sys.exit(code)


def confirm_or_die(prompt: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        die("refusing to proceed without --yes in non-interactive mode")
    answer = input(f"{prompt} [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        die("aborted", 1)


def parse_env(pairs: Optional[List[str]]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            die(f"--env expects KEY=VALUE, got {pair!r}")
        k, v = pair.split("=", 1)
        env[k.strip()] = v
    return env


def _api(args: argparse.Namespace) -> VastAPI:
    return VastAPI(api_key=args.api_key)


# ------------------------------------------------------------------- offers

def _offer_json(o: Offer, hours: float, download_gb: float) -> dict:
    return {
        "id": o.id, "gpu_name": o.gpu_name, "num_gpus": o.num_gpus,
        "gpu_ram_gb": round(o.gpu_ram_gb, 1), "disk_space": round(o.disk_space, 1),
        "dph_total": o.dph_total, "bw_cost_per_gb": round(o.bw_cost_per_gb, 5),
        "est_session_cost": round(o.session_cost(hours, download_gb), 4),
        "dlperf": o.dlperf, "cuda_max_good": o.cuda_max_good,
        "inet_down": o.inet_down, "inet_up": o.inet_up,
        "reliability": round(o.reliability, 4), "geolocation": o.geolocation,
        "verified": o.verified, "machine_id": o.machine_id,
    }


def run_search(api: VastAPI, args: argparse.Namespace) -> List[Offer]:
    query = build_query(
        gpus=args.gpu or None,
        min_vram_gb=args.vram,
        min_disk_gb=args.disk,
        max_dph=args.max_price,
        min_inet_down=args.inet_down,
        min_reliability=args.reliability,
        min_cuda=args.cuda,
        num_gpus=args.num_gpus,
        extra_filters=args.filter,
    )
    offers = api.search_offers(query)
    codes = expand_geolocation(args.geo)
    if codes:
        offers = [o for o in offers if match_geolocation(o.geolocation, codes)]
    offers = cap_bandwidth_cost(offers, args.max_bw)
    return rank_offers(offers, sort=args.sort, hours=args.hours, download_gb=args.download_gb)


def cmd_search(args: argparse.Namespace) -> int:
    api = _api(args)
    offers = run_search(api, args)[: args.limit]
    if args.json:
        print(json.dumps([_offer_json(o, args.hours, args.download_gb) for o in offers], indent=2))
        return 0
    if not offers:
        print("no offers matched — relax filters or check `vastkit search --help`")
        return 1
    rows = []
    for o in offers:
        rows.append([
            str(o.id), o.gpu_name, f"{o.gpu_ram_gb:.0f}", f"{o.disk_space:.0f}",
            money(o.dph_total) + "/h", f"{o.bw_cost_per_gb * 1000:.1f}",
            paint(money(o.session_cost(args.hours, args.download_gb)), _GREEN),
            f"{o.dlperf:.0f}", f"{o.cuda_max_good:.1f}",
            f"{o.inet_down:.0f}", f"{o.reliability:.3f}", o.geolocation,
        ])
    print(fmt_table(
        ["OFFER", "GPU", "VRAM", "DISK", "PRICE", "BW$/TB", "SESSION", "DLPERF",
         "CUDA", "DOWN", "REL", "GEO"],
        rows, rjust={0, 2, 3, 4, 5, 6, 7, 8, 9, 10},
    ))
    print(paint(
        f"\nSESSION = est. cost of {args.hours:g}h rent + {args.download_gb:g}GB download; "
        f"sorted by --sort {args.sort}", _DIM))
    return 0


# ---------------------------------------------------------------- instances

def _instance_json(i: Instance, ssh_reachable: Optional[bool] = None) -> dict:
    d = {
        "id": i.id, "label": i.label, "gpu_name": i.gpu_name, "num_gpus": i.num_gpus,
        "status": i.actual_status, "ssh_host": i.ssh_host, "ssh_port": i.ssh_port,
        "dph_total": i.dph_total, "age_hours": round(i.age_hours, 3),
        "accrued_cost": round(i.accrued_cost, 4), "image": i.image,
        "geolocation": i.geolocation, "public_ipaddr": i.public_ipaddr,
    }
    if ssh_reachable is not None:
        d["ssh_reachable"] = ssh_reachable
    return d


def _ssh_hint(i: Instance, key: str = "") -> str:
    key_part = f" -i {key}" if key else ""
    return f"ssh -p {i.ssh_port}{key_part} root@{i.ssh_host}"


def cmd_rent(args: argparse.Namespace) -> int:
    api = _api(args)
    if args.offer:
        offers = [Offer(id=args.offer)]
    else:
        offers = run_search(api, args)
        if not offers:
            die("no offers matched the search filters")
        best = offers[0]
        print(
            f"best offer: {best.gpu_name} {best.gpu_ram_gb:.0f}GB @ "
            f"{money(best.dph_total)}/hr in {best.geolocation or '?'} "
            f"(est. session {money(best.session_cost(args.hours, args.download_gb))})",
            file=sys.stderr,
        )
        confirm_or_die(f"rent it (image {args.image}, {args.disk:g}GB disk)?", args.yes)

    onstart = args.onstart or ""
    if args.onstart_file:
        onstart = open(args.onstart_file).read()

    spec = RentSpec(
        image=args.image, disk=args.disk, label=args.label, onstart=onstart,
        env=parse_env(args.env), runtype=args.runtype, ssh_key=args.ssh_key or "",
        attempts=args.attempts, boot_timeout=args.boot_timeout,
    )
    instance = rent(api, offers, spec)
    key = find_ssh_key(args.ssh_key or "")
    if args.json:
        print(json.dumps(_instance_json(instance, ssh_reachable=True), indent=2))
        return 0
    print()
    print(paint(f"instance {instance.id} is ready", _GREEN))
    print(f"  gpu:    {instance.gpu_name} x{instance.num_gpus}")
    print(f"  price:  {money(instance.dph_total)}/hr")
    print(f"  ssh:    {_ssh_hint(instance, key)}")
    print(f"  label:  {instance.label}")
    print(paint(f"\nbilling runs until you destroy it:  vastkit destroy {instance.id}", _YELLOW))
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    api = _api(args)
    instances = api.list_instances()
    if args.json:
        print(json.dumps([_instance_json(i) for i in instances], indent=2))
        return 0
    if not instances:
        print("no instances")
        return 0
    rows = [[
        str(i.id), i.label or "-", f"{i.gpu_name} x{i.num_gpus}", status_str(i.actual_status),
        money(i.dph_total) + "/h", f"{i.age_hours:.1f}h", money(i.accrued_cost),
        f"{i.ssh_host}:{i.ssh_port}" if i.has_ssh else "-",
    ] for i in instances]
    print(fmt_table(["ID", "LABEL", "GPU", "STATUS", "PRICE", "AGE", "COST", "SSH"],
                    rows, rjust={0, 4, 5, 6}))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    api = _api(args)
    inst = resolve_instance(api, args.instance)
    key = find_ssh_key(args.ssh_key or "")
    reachable = inst.has_ssh and remote.check_ssh(inst.ssh_host, inst.ssh_port, key)
    if args.json:
        print(json.dumps(_instance_json(inst, ssh_reachable=reachable), indent=2))
    else:
        print(f"instance {inst.id}  ({inst.label})")
        print(f"  gpu:     {inst.gpu_name} x{inst.num_gpus}")
        print(f"  status:  {status_str(inst.actual_status)}"
              + ("  [ssh ok]" if reachable else "  [ssh unreachable]"))
        print(f"  price:   {money(inst.dph_total)}/hr")
        print(f"  uptime:  {inst.age_hours:.2f}h  (~{money(inst.accrued_cost)})")
        print(f"  image:   {inst.image}")
        print(f"  geo:     {inst.geolocation or '?'}")
        if inst.has_ssh:
            print(f"  ssh:     {_ssh_hint(inst, key)}")
    return 0 if (inst.actual_status == "running" and reachable) else 1


def cmd_wait(args: argparse.Namespace) -> int:
    api = _api(args)
    inst = resolve_instance(api, args.instance)
    key = find_ssh_key(args.ssh_key or "")
    start = time.time()
    while time.time() - start < args.timeout:
        inst = api.get_instance(inst.id)
        if inst.actual_status == "running" and inst.has_ssh and \
                remote.check_ssh(inst.ssh_host, inst.ssh_port, key):
            print(f"instance {inst.id} ready after {int(time.time() - start)}s")
            return 0
        print(f"[{int(time.time() - start)}s] status={inst.actual_status or 'starting'}",
              file=sys.stderr)
        time.sleep(10)
    die(f"instance {inst.id} not ready within {args.timeout}s")


def _strip_dashes(cmd: List[str]) -> List[str]:
    return cmd[1:] if cmd and cmd[0] == "--" else cmd


def cmd_ssh(args: argparse.Namespace) -> int:
    api = _api(args)
    inst = resolve_instance(api, args.instance)
    if not inst.has_ssh:
        die(f"instance {inst.id} has no SSH endpoint yet (status={inst.actual_status})")
    key = find_ssh_key(args.ssh_key or "")
    command = " ".join(_strip_dashes(args.cmd))
    if command:
        proc = remote.run(inst.ssh_host, inst.ssh_port, command, key=key, check=False)
        return proc.returncode
    return remote.interactive(inst.ssh_host, inst.ssh_port, key)


def cmd_exec(args: argparse.Namespace) -> int:
    api = _api(args)
    inst = resolve_instance(api, args.instance)
    if not inst.has_ssh:
        die(f"instance {inst.id} has no SSH endpoint yet (status={inst.actual_status})")
    key = find_ssh_key(args.ssh_key or "")
    command = " ".join(_strip_dashes(args.cmd))
    if not command:
        die("no command given (usage: vastkit exec ID -- CMD ...)")
    env = parse_env(args.env)
    if args.detach:
        job = args.job or f"job-{int(time.time())}"
        remote.start_detached(inst.ssh_host, inst.ssh_port, command, job,
                              key=key, env=env, cwd=args.cwd)
        print(f"detached job {job!r} started")
        print(f"  follow:  vastkit logs {inst.id} --job {job} -f")
        print(f"  status:  vastkit jobs {inst.id}")
        return 0
    proc = remote.run(inst.ssh_host, inst.ssh_port, command, key=key,
                      env=env, cwd=args.cwd, check=False)
    return proc.returncode


def cmd_jobs(args: argparse.Namespace) -> int:
    api = _api(args)
    inst = resolve_instance(api, args.instance)
    key = find_ssh_key(args.ssh_key or "")
    jobs = remote.list_jobs(inst.ssh_host, inst.ssh_port, key)
    if args.json:
        print(json.dumps(jobs, indent=2))
        return 0
    if not jobs:
        print("no jobs")
        return 0
    rows = [[j["job"],
             paint(j["status"], _GREEN if j["status"] == "0"
                   else (_YELLOW if j["status"] == "running" else _RED))]
            for j in jobs]
    print(fmt_table(["JOB", "EXIT/STATUS"], rows))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    api = _api(args)
    inst = resolve_instance(api, args.instance)
    key = find_ssh_key(args.ssh_key or "")
    job = args.job
    if not job:
        jobs = remote.list_jobs(inst.ssh_host, inst.ssh_port, key)
        if len(jobs) == 1:
            job = jobs[0]["job"]
        else:
            die("specify --job (see `vastkit jobs`)" if jobs else "no jobs on this instance")
    if not args.follow:
        proc = remote.run(
            inst.ssh_host, inst.ssh_port,
            f"tail -n {args.tail} {remote.JOBS_DIR}/{job}/out.log 2>/dev/null || true",
            key=key, capture=True, check=False)
        sys.stdout.write(proc.stdout or "")
        return 0
    offset = 0
    while True:
        text, offset = remote.read_log(inst.ssh_host, inst.ssh_port, job, key, offset)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
        code = remote.poll_detached(inst.ssh_host, inst.ssh_port, job, key)
        if code is not None:
            text, offset = remote.read_log(inst.ssh_host, inst.ssh_port, job, key, offset)
            if text:
                sys.stdout.write(text)
            print(paint(f"\n[job {job} finished with exit code {code}]",
                        _GREEN if code == 0 else _RED), file=sys.stderr)
            return code
        time.sleep(args.interval)


def cmd_push(args: argparse.Namespace) -> int:
    api = _api(args)
    inst = resolve_instance(api, args.instance)
    key = find_ssh_key(args.ssh_key or "")
    remote.push(inst.ssh_host, inst.ssh_port, args.local, args.remote, key=key,
                excludes=args.exclude if args.exclude else None, delete=args.delete)
    print(f"pushed {args.local} -> {inst.id}:{args.remote}")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    api = _api(args)
    inst = resolve_instance(api, args.instance)
    key = find_ssh_key(args.ssh_key or "")
    remote.pull(inst.ssh_host, inst.ssh_port, args.remote, args.local, key=key)
    print(f"pulled {inst.id}:{args.remote} -> {args.local}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    api = _api(args)
    inst = resolve_instance(api, args.instance)
    api.stop_instance(inst.id)
    print(f"stopped {inst.id} (storage still billed; `vastkit start {inst.id}` to resume,"
          f" `vastkit destroy {inst.id}` to stop paying)")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    api = _api(args)
    inst = resolve_instance(api, args.instance)
    api.start_instance(inst.id)
    print(f"start requested for {inst.id}; `vastkit wait {inst.id}` for SSH")
    return 0


def cmd_destroy(args: argparse.Namespace) -> int:
    api = _api(args)
    targets: List[int] = [int(t) for t in args.instances]
    if args.all:
        instances = api.list_instances()
        if args.label:
            instances = [i for i in instances if i.label == args.label]
        targets = [i.id for i in instances]
    if not targets:
        die("nothing to destroy (give instance ids, or --all [--label X])")
    confirm_or_die(f"destroy {len(targets)} instance(s): {targets}?", args.yes)
    failures = 0
    for iid in targets:
        try:
            inst, accrued = destroy_instance(api, iid)
            desc = ""
            if inst:
                desc = f" ({inst.gpu_name}, ran {inst.age_hours:.2f}h, ~{money(accrued)})"
            print(f"destroyed {iid}{desc}")
        except VastAPIError as e:
            failures += 1
            print(paint(f"failed to destroy {iid}: {e}", _RED), file=sys.stderr)
    return 1 if failures else 0


def cmd_whoami(args: argparse.Namespace) -> int:
    api = _api(args)
    user = api.current_user()
    if args.json:
        print(json.dumps({"id": user.get("id"), "email": user.get("email"),
                          "username": user.get("username"),
                          "credit": user.get("credit")}, indent=2))
        return 0
    print(f"user:    {user.get('email') or user.get('username')}")
    credit = user.get("credit")
    if credit is not None:
        print(f"credit:  {money(float(credit))}")
    return 0


# ------------------------------------------------------------------- parser

def _add_search_flags(p: argparse.ArgumentParser, disk_default: float) -> None:
    p.add_argument("--gpu", action="append", metavar="NAME",
                   help="GPU name as shown on Vast (e.g. 'L40S', 'RTX 6000Ada'); repeatable")
    p.add_argument("--vram", type=float, default=0, metavar="GB", help="min VRAM per GPU")
    p.add_argument("--disk", type=float, default=disk_default, metavar="GB",
                   help="min offered disk (and allocation size when renting)")
    p.add_argument("--max-price", type=float, default=2.0, metavar="$/HR",
                   help="max hourly price (default: 2.0)")
    p.add_argument("--inet-down", type=float, default=200, metavar="MBPS",
                   help="min download bandwidth (default: 200)")
    p.add_argument("--max-bw", type=float, default=0, metavar="$/TB",
                   help="drop hosts charging more than this per TB downloaded "
                        "(e.g. 10 = $0.01/GB; default: no cap)")
    p.add_argument("--reliability", type=float, default=0.90,
                   help="min host reliability 0..1 (default: 0.90)")
    p.add_argument("--cuda", type=float, default=0, metavar="VER",
                   help="min host CUDA version (e.g. 12.4)")
    p.add_argument("--num-gpus", type=int, default=1, help="exact GPU count (default: 1)")
    p.add_argument("--geo", default="", metavar="CODES",
                   help="country codes 'SE,NO' or 'EU' (client-side filter)")
    p.add_argument("--filter", action="append", metavar="K=OP:V",
                   help="raw API filter, e.g. datacenter=eq:true; repeatable")
    p.add_argument("--sort", choices=SORT_MODES, default="effective",
                   help="ranking (default: effective session cost)")
    p.add_argument("--hours", type=float, default=1.0,
                   help="expected session length for cost estimate (default: 1)")
    p.add_argument("--download-gb", type=float, default=50.0,
                   help="expected download volume for cost estimate (default: 50)")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-key", default=None, help=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--ssh-key", default=None, help="private key path "
                        "(default: $VASTKIT_SSH_KEY or auto-detected in ~/.ssh)")

    ap = argparse.ArgumentParser(
        prog="vastkit",
        description="Search, rent and drive GPU servers on Vast.ai — zero dependencies.",
    )
    ap.add_argument("--version", action="version", version=f"vastkit {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("search", parents=[common], help="search & rank GPU offers")
    _add_search_flags(p, disk_default=30)
    p.add_argument("-n", "--limit", type=int, default=20, help="rows to show (default: 20)")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("rent", parents=[common],
                       help="rent the best matching offer and wait until SSH is ready")
    _add_search_flags(p, disk_default=60)
    p.add_argument("--offer", type=int, help="rent this exact offer id (skip search)")
    p.add_argument("--image", default=DEFAULT_IMAGE,
                   help=f"docker image (default: {DEFAULT_IMAGE})")
    p.add_argument("--label", default="vastkit", help="instance label (default: vastkit)")
    p.add_argument("--onstart", default="", help="shell command(s) to run at boot")
    p.add_argument("--onstart-file", default="", help="file with onstart script")
    p.add_argument("--env", action="append", metavar="K=V", help="container env var; repeatable")
    p.add_argument("--runtype", default="ssh_direc", help=argparse.SUPPRESS)
    p.add_argument("--attempts", type=int, default=5, help="max offers to try (default: 5)")
    p.add_argument("--boot-timeout", type=float, default=600,
                   help="seconds to wait per offer for SSH (default: 600)")
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p.set_defaults(func=cmd_rent)

    p = sub.add_parser("ls", parents=[common], help="list rented instances")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("status", parents=[common], help="instance detail + SSH reachability")
    p.add_argument("instance", nargs="?", help="instance id (optional if only one)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("wait", parents=[common], help="block until instance is SSH-ready")
    p.add_argument("instance", nargs="?")
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("ssh", parents=[common], help="interactive shell (or one-off command)")
    p.add_argument("instance", nargs="?")
    p.add_argument("cmd", nargs="*", default=[], help="optional command after --")
    p.set_defaults(func=cmd_ssh)

    p = sub.add_parser("exec", parents=[common], help="run a command on the instance")
    p.add_argument("instance")
    p.add_argument("cmd", nargs="*", default=[], help="command after --")
    p.add_argument("--env", action="append", metavar="K=V", help="remote env var; repeatable")
    p.add_argument("--cwd", default="", help="remote working directory")
    p.add_argument("--detach", action="store_true",
                   help="run under nohup, survive disconnects (see `logs -f`)")
    p.add_argument("--job", default="", help="name for the detached job")
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("jobs", parents=[common], help="list detached jobs")
    p.add_argument("instance", nargs="?")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("logs", parents=[common], help="show/follow a detached job's log")
    p.add_argument("instance", nargs="?")
    p.add_argument("--job", default="", help="job name (optional if only one)")
    p.add_argument("-f", "--follow", action="store_true",
                   help="follow until the job exits; exits with the job's code")
    p.add_argument("-n", "--tail", type=int, default=100, help="lines to show (default: 100)")
    p.add_argument("--interval", type=float, default=5.0, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("push", parents=[common], help="upload files (rsync, tar fallback)")
    p.add_argument("instance")
    p.add_argument("local")
    p.add_argument("remote")
    p.add_argument("--exclude", action="append", metavar="PAT",
                   help="exclude pattern; repeatable (default: .git, __pycache__, ...)")
    p.add_argument("--delete", action="store_true", help="delete remote files not present locally")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("pull", parents=[common], help="download files (rsync, tar fallback)")
    p.add_argument("instance")
    p.add_argument("remote")
    p.add_argument("local")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("stop", parents=[common], help="stop (hibernate) an instance")
    p.add_argument("instance", nargs="?")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("start", parents=[common], help="start a stopped instance")
    p.add_argument("instance", nargs="?")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("destroy", parents=[common], help="destroy instance(s) — stops billing")
    p.add_argument("instances", nargs="*", help="instance ids")
    p.add_argument("--all", action="store_true", help="destroy all instances")
    p.add_argument("--label", default="", help="with --all: only this label")
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p.set_defaults(func=cmd_destroy)

    p = sub.add_parser("whoami", parents=[common], help="verify API key, show credit")
    p.set_defaults(func=cmd_whoami)

    return ap


def split_remote_command(argv: List[str]) -> tuple[List[str], List[str]]:
    """Split argv at the first standalone ``--``: left is parsed by argparse,
    right is the verbatim remote command (may contain its own flags)."""
    if argv and argv[0] in ("exec", "ssh") and "--" in argv:
        idx = argv.index("--")
        return argv[:idx], argv[idx + 1:]
    return argv, []


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    head, tail = split_remote_command(raw)
    args = parser.parse_args(head)
    if tail:
        args.cmd = tail
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except (VastAPIError, RemoteError) as e:
        print(paint(f"error: {e}", _RED), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(paint("\ninterrupted", _YELLOW), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
