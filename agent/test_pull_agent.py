"""Unit tests for the pull agent. No Nix, no systemd: commands are faked."""

import json
import tempfile
import unittest
from pathlib import Path

from pull_agent import (
    HTTP_RETRY_TIMEOUT,
    HTTP_TIMEOUT,
    Agent,
    CommandError,
    Result,
    State,
    exit_code,
    reboot_pending,
    switch_mode,
    systemd_major,
    write_metrics,
)

SWITCH = "/bin/switch-to-configuration"


def make_toplevel(root, name, systemd="systemd-260.5"):
    """A fake system toplevel whose `systemd` link names a systemd package."""
    top = Path(root) / name
    top.mkdir()
    if systemd is not None:
        (top / "systemd").symlink_to(f"/nix/store/{'0' * 32}-{systemd}")
    return str(top)


class FakeSystem:
    """Records commands and simulates the system profile and health."""

    def __init__(self, current, healthy=True, switch_fails_for=(), inactive_units=()):
        self.current = current
        self.profile = current
        self.healthy = healthy
        self.switch_fails_for = set(switch_fails_for)
        self.inactive_units = set(inactive_units)
        self.calls = []

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[0] == "readlink":
            return self.current if cmd[2] == "/run/current-system" else self.profile
        if cmd[0] == "nix-env" and "--set" in cmd:
            self.profile = cmd[-1]
            return ""
        if cmd[0].endswith(SWITCH):
            path = cmd[0].removesuffix(SWITCH)
            if cmd[1] != "boot":
                self.current = path
            if path in self.switch_fails_for:
                raise CommandError("switch-to-configuration exited 4")
            return ""
        if cmd[:2] == ["systemctl", "is-active"] and cmd[2] in self.inactive_units:
            raise CommandError(f"systemctl is-active {cmd[2]} exited 3: inactive")
        if cmd[0] == "health" and not self.healthy:
            raise CommandError("health check failed")
        return ""

    def acted(self):
        """Commands that change something, as opposed to reading state."""
        return [c for c in self.calls if c[0] != "readlink"]


class FakeHttp:
    """Answers health URLs from a script of statuses; None = no response."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.timeouts = []

    def __call__(self, url, timeout):
        self.timeouts.append(timeout)
        return self.answers.pop(0)


def manifest(seq=1, path=None, host="web-x86", status="ok", enabled=True):
    return {
        "seq": seq,
        "rev": "deadbeef",
        "status": status,
        "enabled": enabled,
        "hosts": {host: path},
    }


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.old = make_toplevel(self.root, "old")
        self.new = make_toplevel(self.root, "new")

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self, system, **kwargs):
        kwargs.setdefault("health_checks", [["health"]])
        return Agent(
            hostname="web-x86",
            state_file=self.state,
            runner=system,
            settle_seconds=0,
            health_retry_delay=0,
            **kwargs,
        )

    def step(self, system, **kwargs):
        kwargs.setdefault("path", self.new)
        return self.agent(system).step(manifest(**kwargs))

    # -- live switch and rollback ------------------------------------------

    def test_noop_when_already_on_target(self):
        system = FakeSystem(current=self.new)
        result = self.step(system)
        self.assertEqual(result.action, "noop")
        self.assertEqual(system.acted(), [])
        self.assertEqual(State.load(self.state).last_seq, 1)

    def test_switch_realises_then_activates(self):
        system = FakeSystem(current=self.old)
        result = self.step(system)
        self.assertEqual(result.action, "switched")
        self.assertEqual(system.current, self.new)
        order = [c[0] for c in system.calls]
        self.assertLess(order.index("nix-store"), order.index("nix-env"))
        self.assertEqual(State.load(self.state).last_seq, 1)

    def test_unhealthy_switch_rolls_back_to_the_previous_system(self):
        system = FakeSystem(current=self.old, healthy=False)
        result = self.step(system)
        self.assertEqual(result.action, "rolled_back")
        self.assertEqual(system.current, self.old)
        # The rollback target is the system that ran before, set explicitly.
        sets = [c[-1] for c in system.calls if c[0] == "nix-env"]
        self.assertEqual(sets, [self.new, self.old])
        self.assertNotIn("--rollback", [a for c in system.calls for a in c])
        self.assertIn(["systemctl", "reset-failed"], system.calls)
        state = State.load(self.state)
        self.assertIn(self.new, state.bad_paths)
        self.assertEqual(state.last_seq, -1)

    def test_failed_activation_rolls_back(self):
        system = FakeSystem(current=self.old, switch_fails_for=[self.new])
        result = self.step(system)
        self.assertEqual(result.action, "rolled_back")
        self.assertEqual(system.current, self.old)
        self.assertIn(self.new, State.load(self.state).bad_paths)

    def test_rollback_switch_error_is_tolerated(self):
        system = FakeSystem(current=self.old, switch_fails_for=[self.new, self.old])
        result = self.step(system)
        self.assertEqual(result.action, "rolled_back")
        self.assertEqual(system.profile, self.old)
        self.assertIn("exited 4", result.detail)
        self.assertIn(self.new, State.load(self.state).bad_paths)

    def test_target_is_marked_bad_before_the_rollback_switch(self):
        system = FakeSystem(current=self.old, healthy=False)

        class Crash(Exception):
            pass

        def crashes_during_rollback(cmd):
            if cmd[0] == self.old + SWITCH:
                raise Crash()
            return system(cmd)

        with self.assertRaises(Crash):
            self.agent(crashes_during_rollback).step(manifest(path=self.new))
        self.assertIn(self.new, State.load(self.state).bad_paths)

    def test_cache_failure_changes_nothing(self):
        system = FakeSystem(current=self.old)

        def broken_cache(cmd):
            if cmd[0] == "nix-store":
                raise CommandError("cannot substitute path")
            return system(cmd)

        with self.assertRaises(CommandError):
            self.agent(broken_cache).step(manifest(path=self.new))
        self.assertEqual(system.current, self.old)
        self.assertEqual(State.load(self.state).bad_paths, [])

    def test_known_bad_path_is_not_retried(self):
        system = FakeSystem(current=self.old, healthy=False)
        self.step(system)
        system.calls.clear()
        result = self.step(system, seq=2)
        self.assertEqual(result.action, "skipped")
        self.assertEqual(system.calls, [])

    # -- manifest checks ---------------------------------------------------

    def test_older_manifest_is_refused(self):
        State(last_seq=10).save(self.state)
        system = FakeSystem(current=self.old)
        result = self.step(system, seq=9)
        self.assertEqual(result.action, "skipped")
        self.assertEqual(system.current, self.old)

    def test_host_missing_from_manifest(self):
        result = self.step(FakeSystem(current=self.old), host="web-arm")
        self.assertEqual(result.action, "skipped")

    def test_failed_or_paused_manifest_is_not_applied(self):
        State(last_seq=5).save(self.state)
        failed = {"seq": 6, "rev": "x", "status": "failed", "enabled": False, "hosts": {}}
        paused = manifest(seq=7, path=self.new, enabled=False)
        unknown = {"seq": 8, "hosts": {"web-x86": self.new}}
        for m in (failed, paused, unknown):
            system = FakeSystem(current=self.old)
            result = self.agent(system).step(m)
            self.assertEqual(result.action, "skipped", m)
            self.assertEqual(system.calls, [])
        self.assertEqual(State.load(self.state).last_seq, 5)

    # -- health checks -----------------------------------------------------

    def test_cleanly_stopped_critical_unit_rolls_back(self):
        system = FakeSystem(current=self.old, inactive_units=["nginx"])
        result = self.agent(system, critical_units=["nginx"]).step(manifest(path=self.new))
        self.assertEqual(result.action, "rolled_back")
        self.assertIn(["systemctl", "is-active", "nginx"], system.calls)
        self.assertIn("inactive", result.detail)

    def test_health_url_without_response_is_retried_once(self):
        http = FakeHttp(None, 200)
        agent = self.agent(FakeSystem(current=self.old), health_urls=["http://h/"], http=http)
        result = agent.step(manifest(path=self.new))
        self.assertEqual(result.action, "switched")
        self.assertEqual(http.timeouts, [HTTP_TIMEOUT, HTTP_RETRY_TIMEOUT])

    def test_health_url_server_error_is_not_retried(self):
        http = FakeHttp(503)
        agent = self.agent(FakeSystem(current=self.old), health_urls=["http://h/"], http=http)
        result = agent.step(manifest(path=self.new))
        self.assertEqual(result.action, "rolled_back")
        self.assertIn("HTTP 503", result.detail)
        self.assertEqual(len(http.timeouts), 1)

    def test_health_url_silent_twice_rolls_back(self):
        http = FakeHttp(None, None)
        agent = self.agent(FakeSystem(current=self.old), health_urls=["http://h/"], http=http)
        result = agent.step(manifest(path=self.new))
        self.assertEqual(result.action, "rolled_back")
        self.assertIn("no response", result.detail)

    # -- systemd major version guard ---------------------------------------

    def test_systemd_major_is_read_from_the_toplevel(self):
        self.assertEqual(systemd_major(self.new), 260)

    def test_same_major_switches_live(self):
        patch = make_toplevel(self.root, "patch", systemd="systemd-260.9")
        self.assertEqual(switch_mode(self.old, patch), "live")

    def test_different_major_waits_for_boot(self):
        older = make_toplevel(self.root, "older", systemd="systemd-259.3")
        self.assertEqual(switch_mode(older, self.new), "boot")

    def test_missing_systemd_link_waits_for_boot(self):
        bare = make_toplevel(self.root, "bare", systemd=None)
        self.assertEqual(switch_mode(bare, self.new), "boot")
        self.assertEqual(switch_mode(self.old, bare), "boot")

    def test_unparsable_systemd_name_waits_for_boot(self):
        odd = make_toplevel(self.root, "odd", systemd="systemd-unstable")
        self.assertEqual(switch_mode(self.old, odd), "boot")

    def test_systemd_major_change_is_staged_for_boot(self):
        old = make_toplevel(self.root, "v259", systemd="systemd-259.3")
        system = FakeSystem(current=old)
        result = self.step(system)
        self.assertEqual(result.action, "boot_staged")
        self.assertEqual(system.current, old)
        self.assertEqual(system.profile, self.new)
        self.assertIn([self.new + SWITCH, "boot"], system.calls)
        self.assertNotIn(["health"], system.calls)
        state = State.load(self.state)
        self.assertEqual(state.bad_paths, [])
        self.assertEqual(state.staged, self.new)

    def test_already_staged_target_is_left_alone(self):
        system = FakeSystem(current=self.old)
        system.profile = self.new
        result = self.step(system)
        self.assertEqual(result.action, "boot_staged")
        self.assertEqual(system.acted(), [])

    def test_failed_boot_staging_restores_the_profile(self):
        old = make_toplevel(self.root, "v259", systemd="systemd-259.3")
        system = FakeSystem(current=old, switch_fails_for=[self.new])
        with self.assertRaises(CommandError):
            self.step(system)
        self.assertEqual(system.profile, old)
        self.assertEqual(State.load(self.state).bad_paths, [])

    def test_health_is_checked_once_after_booting_a_staged_system(self):
        State(last_seq=1, staged=self.new).save(self.state)
        system = FakeSystem(current=self.new)
        result = self.step(system)
        self.assertEqual(result.action, "post_boot_check")
        self.assertTrue(result.healthy)
        self.assertIsNone(State.load(self.state).staged)
        self.assertEqual(self.step(system).action, "noop")

    def test_unhealthy_post_boot_check_is_reported_not_rolled_back(self):
        State(last_seq=1, staged=self.new).save(self.state)
        system = FakeSystem(current=self.new, healthy=False)
        result = self.step(system)
        self.assertEqual(result.action, "post_boot_check")
        self.assertFalse(result.healthy)
        self.assertEqual([c for c in system.acted() if c != ["health"]], [])

    # -- reboot detection, exit code, metrics --------------------------------

    def test_reboot_pending(self):
        for f in ("k1", "k2", "initrd"):
            (self.root / f).touch()

        def system(name, kernel):
            top = Path(make_toplevel(self.root, name))
            (top / "kernel").symlink_to(self.root / kernel)
            (top / "initrd").symlink_to(self.root / "initrd")
            return str(top)

        booted, same, newer = system("b", "k1"), system("s", "k1"), system("n", "k2")
        self.assertFalse(reboot_pending(booted, same, profile=same))
        self.assertTrue(reboot_pending(booted, newer, profile=newer))
        self.assertTrue(reboot_pending(booted, same, profile=newer))

    def test_only_errors_fail_the_unit(self):
        self.assertEqual(exit_code(Result("rolled_back")), 0)
        self.assertEqual(exit_code(Result("switched")), 0)
        self.assertEqual(exit_code(Result("error")), 1)

    def test_metrics_file_is_valid_exposition(self):
        out = self.root / "fleet.prom"
        counters = {}
        write_metrics(out, Result("switched", self.new), 3, counters)
        write_metrics(out, Result("post_boot_check", self.new, healthy=False), 3, counters)
        write_metrics(out, Result("boot_staged", self.new), 3, counters, pending=True)
        text = out.read_text()
        self.assertIn('fleet_deploy_runs_total{action="switched"} 1', text)
        self.assertIn('fleet_deploy_runs_total{action="boot_staged"} 1', text)
        self.assertIn("fleet_deploy_manifest_seq 3", text)
        self.assertIn("fleet_deploy_reboot_pending 1", text)
        self.assertIn("fleet_deploy_post_boot_healthy 0", text)
        self.assertIn("fleet_deploy_last_success_timestamp_seconds", text)
        json.dumps(counters)  # counters must stay serialisable


if __name__ == "__main__":
    unittest.main()
