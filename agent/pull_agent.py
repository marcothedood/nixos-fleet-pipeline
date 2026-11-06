#!/usr/bin/env python3
"""fleet-pull-agent: pull-based NixOS deploys with health checks and rollback.

One run of the agent:

1. Fetch the deploy manifest (JSON) published by CI. Only a manifest with
   status "ok" and enabled true is applied.
2. Look up the store path for this host.
3. If it differs from /run/current-system, realise it (substituted from the
   binary cache, signatures checked by Nix) and activate it:
   - same systemd major version: switch live and run the health checks. On
     failure, mark the path bad and switch back to the system that was
     running before.
   - different or unknown systemd major version: stage it for the next boot
     and report a pending reboot.
4. After a reboot into a staged system, run the health checks once and
   record the outcome.
5. Write Prometheus textfile metrics.

The manifest looks like:

    {
      "seq": 42,
      "rev": "<git sha>",
      "status": "ok",
      "enabled": true,
      "hosts": { "web-x86": "/nix/store/...-nixos-system-web-x86-..." }
    }

`seq` only ever goes up. The agent refuses a manifest older than the last one
it applied, so a stale or replayed manifest cannot downgrade a node.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

SYSTEM_PROFILE = "/nix/var/nix/profiles/system"
CURRENT_SYSTEM = "/run/current-system"
BOOTED_SYSTEM = "/run/booted-system"

# First HTTP attempt, and the longer budget for the single retry when the
# first attempt got no response at all.
HTTP_TIMEOUT = 5.0
HTTP_RETRY_TIMEOUT = 30.0

OUTCOMES = (
    "noop",
    "switched",
    "rolled_back",
    "boot_staged",
    "post_boot_check",
    "skipped",
    "error",
)

# <toplevel>/systemd links to the systemd package of that system, for
# example /nix/store/<hash>-systemd-260.5.
SYSTEMD_STORE_NAME = re.compile(r"[0-9a-z]{32}-systemd-(\d+)(?:[.~-].*)?")


class CommandError(RuntimeError):
    pass


Runner = Callable[[Sequence[str]], str]
HttpGet = Callable[[str, float], "int | None"]


def log(message: str) -> None:
    print(f"fleet-pull-agent: {message}", file=sys.stderr)


def run(cmd: Sequence[str]) -> str:
    """Run a command and return stdout. Raises CommandError on failure."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        output = (proc.stderr or proc.stdout).strip()
        raise CommandError(f"{' '.join(cmd)} exited {proc.returncode}: {output}")
    return proc.stdout.strip()


def http_status(url: str, timeout: float) -> int | None:
    """HTTP status of a GET request, or None when there was no response."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (OSError, http.client.HTTPException):  # refused, reset, timed out
        return None


@dataclass
class State:
    last_seq: int = -1
    bad_paths: list[str] = field(default_factory=list)
    staged: str | None = None  # path staged for the next boot, not yet verified

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        return cls(
            last_seq=int(data.get("last_seq", -1)),
            bad_paths=list(data.get("bad_paths", [])),
            staged=data.get("staged"),
        )

    def save(self, path: Path) -> None:
        atomic_write(
            path,
            json.dumps(
                {
                    "last_seq": self.last_seq,
                    "bad_paths": self.bad_paths[-20:],
                    "staged": self.staged,
                },
                indent=2,
            ),
        )


@dataclass
class Result:
    action: str  # one of OUTCOMES
    target: str | None = None
    detail: str = ""
    healthy: bool | None = None  # set by a post-boot check


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def fetch_manifest(url: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


# -- systemd version guard ----------------------------------------------------


def systemd_major(toplevel: str) -> int | None:
    """Major systemd version of a system toplevel, or None if unknown."""
    try:
        name = os.path.basename(os.readlink(os.path.join(toplevel, "systemd")))
    except OSError:
        return None
    match = SYSTEMD_STORE_NAME.fullmatch(name)
    return int(match.group(1)) if match else None


def switch_mode(current: str, target: str) -> str:
    """"live" if the target can be switched to in place, otherwise "boot".

    A live switch across a systemd major version re-executes systemd in the
    middle of the switch. That can break the switch, and a rollback on a
    half re-executed system can stop units without starting them again.
    Fail closed: if either version is unknown, never switch live.
    """
    a, b = systemd_major(current), systemd_major(target)
    return "live" if a is not None and a == b else "boot"


def reboot_pending(
    booted: str = BOOTED_SYSTEM,
    current: str = CURRENT_SYSTEM,
    profile: str = SYSTEM_PROFILE,
) -> bool:
    """True when the next boot changes what runs.

    Either a system is staged for the next boot (the profile points past the
    running system), or the running system uses a kernel or initrd that the
    machine has not booted yet.
    """
    if Path(profile).exists() and Path(profile).resolve() != Path(current).resolve():
        return True
    for name in ("kernel", "initrd", "kernel-modules"):
        a, b = Path(booted, name), Path(current, name)
        if (a.exists() or b.exists()) and a.resolve() != b.resolve():
            return True
    return False


# -- the agent ----------------------------------------------------------------


class Agent:
    def __init__(
        self,
        hostname: str,
        state_file: Path,
        health_checks: Sequence[Sequence[str]] = (),
        critical_units: Sequence[str] = (),
        health_urls: Sequence[str] = (),
        runner: Runner = run,
        http: HttpGet = http_status,
        settle_seconds: float = 5.0,
        health_retry_delay: float = 10.0,
        switch_action: str = "switch",
        dry_run: bool = False,
    ) -> None:
        self.hostname = hostname
        self.state_file = state_file
        self.health_checks = health_checks
        self.critical_units = critical_units
        self.health_urls = health_urls
        self.run = runner
        self.http = http
        self.settle_seconds = settle_seconds
        self.health_retry_delay = health_retry_delay
        self.switch_action = switch_action
        self.dry_run = dry_run

    # -- system operations -------------------------------------------------

    def current_system(self) -> str:
        return self.run(["readlink", "-f", CURRENT_SYSTEM])

    def profile_system(self) -> str:
        # -m: a fresh machine may not have a system profile yet.
        return self.run(["readlink", "-m", SYSTEM_PROFILE])

    def set_profile(self, path: str) -> None:
        self.run(["nix-env", "--profile", SYSTEM_PROFILE, "--set", path])

    def realise(self, path: str) -> None:
        # Substitutes the closure from the configured caches. Nix refuses
        # paths that are not signed by a key in trusted-public-keys.
        self.run(["nix-store", "--realise", path])

    def activate(self, path: str) -> None:
        self.set_profile(path)
        self.run([f"{path}/bin/switch-to-configuration", self.switch_action])

    def stage_for_boot(self, current: str, target: str) -> None:
        self.set_profile(target)
        try:
            self.run([f"{target}/bin/switch-to-configuration", "boot"])
        except CommandError:
            # Keep the profile on what the boot loader still points to, so
            # the next run retries instead of reporting a staged system.
            self.set_profile(current)
            raise

    def rollback(self, previous: str) -> str:
        """Switch back to `previous`, the system that ran before the deploy.

        Returns a warning, or "" if the rollback was clean. Errors here are
        reported, not raised: the target is already marked bad, and the
        node is on the previous generation whatever the switch exit code.
        """
        self.set_profile(previous)
        warnings = []
        # reset-failed first: units that only existed in the bad generation
        # stay "failed" until reset. Anything that fails after this point
        # failed in the previous generation and stays visible.
        for cmd in (
            ["systemctl", "reset-failed"],
            [f"{previous}/bin/switch-to-configuration", self.switch_action],
        ):
            try:
                self.run(cmd)
            except CommandError as e:
                warnings.append(str(e))
        if warnings:
            log("warning: rollback: " + "; ".join(warnings))
        return "; ".join(warnings)

    # -- health checks -----------------------------------------------------

    def healthy(self) -> tuple[bool, str]:
        if self.settle_seconds:
            time.sleep(self.settle_seconds)
        commands = [list(c) for c in self.health_checks]
        # A unit that was stopped cleanly is "inactive", not "failed", and
        # passes `is-system-running` and `--failed` checks. Ask directly.
        commands += [["systemctl", "is-active", unit] for unit in self.critical_units]
        for cmd in commands:
            try:
                self.run(cmd)
            except CommandError as e:
                return False, str(e)
        for url in self.health_urls:
            ok, why = self.check_url(url)
            if not ok:
                return False, why
        return True, ""

    def check_url(self, url: str) -> tuple[bool, str]:
        status = self.http(url, HTTP_TIMEOUT)
        if status is None:
            # No response at all: a cold node right after a switch can be
            # slower than the first timeout. Retry once with a longer budget.
            # An HTTP error status is a real answer and is not retried.
            time.sleep(self.health_retry_delay)
            status = self.http(url, HTTP_RETRY_TIMEOUT)
        if status is None:
            return False, f"{url}: no response"
        if not 200 <= status < 400:
            return False, f"{url}: HTTP {status}"
        return True, ""

    # -- main logic --------------------------------------------------------

    def step(self, manifest: dict) -> Result:
        state = State.load(self.state_file)
        seq = int(manifest.get("seq", -1))
        status, enabled = manifest.get("status"), manifest.get("enabled")

        # A failed CI run, or a fleet-wide pause set by hand. last_seq does
        # not move, so the same manifest applies as soon as it is enabled.
        if status != "ok" or enabled is not True:
            return Result(
                "skipped",
                detail=f"manifest seq {seq} not deployable "
                f"(status={status}, enabled={enabled})",
            )
        target = manifest.get("hosts", {}).get(self.hostname)
        if target is None:
            return Result("skipped", detail=f"{self.hostname} not in manifest")
        if seq < state.last_seq:
            return Result(
                "skipped",
                target,
                f"manifest seq {seq} older than applied {state.last_seq}",
            )
        if target in state.bad_paths:
            return Result("skipped", target, "target previously failed health check")

        current = self.current_system()
        if current == target:
            state.last_seq = max(state.last_seq, seq)
            result = Result("noop", target)
            if state.staged == target:
                # First run after booting into a staged system.
                state.staged = None
                ok, why = self.healthy()
                result = Result("post_boot_check", target, why, healthy=ok)
            state.save(self.state_file)
            return result

        if self.profile_system() == target:
            return Result("boot_staged", target, "staged, waiting for a reboot")

        if self.dry_run:
            return Result("skipped", target, f"dry run: would switch from {current}")

        self.realise(target)  # a cache or network failure changes nothing

        if switch_mode(current, target) == "boot":
            self.stage_for_boot(current, target)
            state.last_seq, state.staged = seq, target
            state.save(self.state_file)
            return Result(
                "boot_staged",
                target,
                "systemd major version differs or is unknown: active after the next boot",
            )

        try:
            # switch-to-configuration exits non-zero when units fail to
            # start: that is a bad deploy too, not just an error.
            self.activate(target)
            ok, why = self.healthy()
        except CommandError as e:
            ok, why = False, str(e)
        if ok:
            state.last_seq, state.staged = seq, None
            state.save(self.state_file)
            return Result("switched", target)

        # Record the failure before touching the system again: if the agent
        # dies during the rollback, the next run must not retry the target.
        state.bad_paths.append(target)
        state.staged = None
        state.save(self.state_file)
        warning = self.rollback(current)
        detail = f"{why}; back on {current}"
        return Result("rolled_back", target, f"{detail}; {warning}" if warning else detail)


def exit_code(result: Result) -> int:
    """Exit status of the agent unit for a run.

    Only an agent error fails the unit. A rollback is a handled outcome and
    is reported through the metrics. A failed agent unit would leave the
    system "degraded", so `systemctl is-system-running` and `--failed` style
    health checks would keep failing on later deploys until someone reset it.
    """
    return 1 if result.action == "error" else 0


# -- metrics ------------------------------------------------------------------


def write_metrics(
    path: Path, result: Result, seq: int, counters: dict, pending: bool = False
) -> None:
    now = int(time.time())
    counters[result.action] = counters.get(result.action, 0) + 1
    if result.action == "switched":
        counters["last_success"] = now
    if result.healthy is not None:
        counters["post_boot_healthy"] = int(result.healthy)

    lines = [
        "# HELP fleet_deploy_last_run_timestamp_seconds Last agent run.",
        "# TYPE fleet_deploy_last_run_timestamp_seconds gauge",
        f"fleet_deploy_last_run_timestamp_seconds {now}",
        "# HELP fleet_deploy_manifest_seq Sequence number of the last manifest seen.",
        "# TYPE fleet_deploy_manifest_seq gauge",
        f"fleet_deploy_manifest_seq {seq}",
        "# HELP fleet_deploy_reboot_pending 1 if the next boot changes the running system.",
        "# TYPE fleet_deploy_reboot_pending gauge",
        f"fleet_deploy_reboot_pending {int(pending)}",
        "# HELP fleet_deploy_runs_total Agent runs by outcome.",
        "# TYPE fleet_deploy_runs_total counter",
    ]
    for action in OUTCOMES:
        lines.append(
            f'fleet_deploy_runs_total{{action="{action}"}} {counters.get(action, 0)}'
        )
    if "last_success" in counters:
        lines += [
            "# HELP fleet_deploy_last_success_timestamp_seconds Last successful switch.",
            "# TYPE fleet_deploy_last_success_timestamp_seconds gauge",
            f"fleet_deploy_last_success_timestamp_seconds {counters['last_success']}",
        ]
    if "post_boot_healthy" in counters:
        lines += [
            "# HELP fleet_deploy_post_boot_healthy Health of the last system verified after a reboot.",
            "# TYPE fleet_deploy_post_boot_healthy gauge",
            f"fleet_deploy_post_boot_healthy {counters['post_boot_healthy']}",
        ]
    atomic_write(path, "\n".join(lines) + "\n")


def load_counters(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# -- CLI ----------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--manifest-url", required=True)
    p.add_argument("--hostname", default=socket.gethostname())
    p.add_argument("--state-dir", default="/var/lib/fleet-pull-agent")
    p.add_argument("--metrics-file", default=None)
    p.add_argument(
        "--health-check",
        action="append",
        default=[],
        help="Command run after switching; repeatable. Non-zero exit = unhealthy.",
    )
    p.add_argument(
        "--critical-unit",
        action="append",
        default=[],
        help="Unit that must be active after switching; repeatable.",
    )
    p.add_argument(
        "--health-url",
        action="append",
        default=[],
        help="URL that must answer 2xx or 3xx after switching; repeatable.",
    )
    p.add_argument(
        "--health-retry-delay",
        type=float,
        default=10.0,
        help="Seconds before retrying a health URL that gave no response.",
    )
    p.add_argument("--settle-seconds", type=float, default=5.0)
    p.add_argument(
        "--switch-action",
        choices=["switch", "test", "boot"],
        default="switch",
        help="switch-to-configuration action (use 'test' where no boot loader exists).",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    state_dir = Path(args.state_dir)
    checks = [c.split() for c in args.health_check] or [
        ["systemctl", "is-system-running", "--wait"]
    ]
    agent = Agent(
        hostname=args.hostname,
        state_file=state_dir / "state.json",
        health_checks=checks,
        critical_units=args.critical_unit,
        health_urls=args.health_url,
        settle_seconds=args.settle_seconds,
        health_retry_delay=args.health_retry_delay,
        switch_action=args.switch_action,
        dry_run=args.dry_run,
    )

    seq = -1
    try:
        manifest = fetch_manifest(args.manifest_url)
        seq = int(manifest.get("seq", -1))
        result = agent.step(manifest)
    except Exception as e:  # report every failure as a metric, then exit 1
        result = Result("error", detail=str(e))

    if args.metrics_file:
        counters_file = state_dir / "counters.json"
        counters = load_counters(counters_file)
        try:
            pending = reboot_pending()
        except OSError:
            pending = False
        write_metrics(Path(args.metrics_file), result, seq, counters, pending)
        atomic_write(counters_file, json.dumps(counters))

    print(json.dumps(result.__dict__))
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
