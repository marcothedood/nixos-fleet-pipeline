# End-to-end test of the deploy loop in a VM: a good deploy switches, a
# broken one rolls back on its own, stale, known-bad, failed and paused
# manifests are ignored, and the agent can deploy a change to its own unit.
# The candidate generations are specialisations, so their closures are
# already in the VM's store, exactly like after a cache substitution.
#
# The agent unit is Type=exec, so `systemctl start` returns as soon as the
# agent runs. Each run is awaited explicitly and its outcome is read from
# the agent's counters, not from an exit code.
{ pkgs, self }:

pkgs.testers.runNixOSTest {
  name = "fleet-pull-agent";

  nodes.machine = { lib, pkgs, ... }: {
    imports = [ self.nixosModules.default ];

    services.fleet-pull-agent = {
      enable = true;
      manifestUrl = "file:///var/lib/fleet-test/manifest.json";
      settleSeconds = 1;
      # Test VMs have no boot loader to install.
      switchAction = "test";
      metricsFile = "/var/lib/fleet-pull-agent/fleet_deploy.prom";
      criticalUnits = [ "app" ];
    };
    # The test drives the agent by hand instead of waiting for the timer.
    systemd.timers.fleet-pull-agent.wantedBy = lib.mkForce [ ];

    # The test only checks that garbage collection waits for the deploy
    # lock; it must not delete anything from the VM's store.
    nix.gc.options = "--dry-run";

    # A long-running workload the agent treats as critical.
    systemd.services.app = {
      wantedBy = [ "multi-user.target" ];
      serviceConfig.ExecStart = "${pkgs.coreutils}/bin/sleep infinity";
    };

    environment.etc."fleet-version".text = lib.mkDefault "base";

    specialisation.good.configuration = {
      environment.etc."fleet-version".text = "good";
    };

    specialisation.broken.configuration = {
      environment.etc."fleet-version".text = "broken";
      systemd.services.always-fails = {
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${pkgs.coreutils}/bin/false";
        };
      };
    };

    # The workload exits cleanly: "inactive", not "failed". The system
    # stays "running"; only the critical unit check notices.
    specialisation.app-stopped.configuration = {
      environment.etc."fleet-version".text = "app-stopped";
      systemd.services.app.serviceConfig.ExecStart = lib.mkForce "${pkgs.coreutils}/bin/true";
    };

    # A generation that changes the agent's own unit.
    specialisation.agent-changed.configuration = {
      environment.etc."fleet-version".text = "agent-changed";
      systemd.services.fleet-pull-agent.environment.FLEET_TEST_MARKER = "changed";
    };
  };

  testScript = ''
    import json

    OUTCOMES = ["noop", "switched", "rolled_back", "boot_staged", "post_boot_check", "skipped", "error"]
    STATE_DIR = "/var/lib/fleet-pull-agent"

    machine.wait_for_unit("multi-user.target")

    def resolve(path):
        return machine.succeed(f"readlink -f {path}").strip()

    def current():
        return resolve("/run/current-system")

    def version():
        return machine.succeed("cat /etc/fleet-version").strip()

    def publish(seq, path, status="ok", enabled=True):
        hosts = {"machine": path} if path else {}
        manifest = json.dumps({"seq": seq, "rev": "test", "status": status, "enabled": enabled, "hosts": hosts})
        machine.succeed(
            "mkdir -p /var/lib/fleet-test && "
            f"echo '{manifest}' > /var/lib/fleet-test/manifest.json"
        )

    def counters():
        return json.loads(machine.succeed(f"cat {STATE_DIR}/counters.json 2>/dev/null || echo '{{}}'"))

    def last_seq():
        return json.loads(machine.succeed(f"cat {STATE_DIR}/state.json"))["last_seq"]

    def run_agent():
        """Run the agent once, wait for it to exit, return (outcome, invocation id)."""
        before = counters()
        machine.succeed("systemctl start fleet-pull-agent.service")
        invocation = machine.succeed("systemctl show -P InvocationID fleet-pull-agent.service").strip()
        machine.wait_until_fails("systemctl is-active --quiet fleet-pull-agent.service", timeout=600)
        after = counters()
        changed = [o for o in OUTCOMES if after.get(o, 0) != before.get(o, 0)]
        assert len(changed) == 1, (before, after)
        return changed[0], invocation

    base = current()
    good = resolve("/run/current-system/specialisation/good")
    broken = resolve("/run/current-system/specialisation/broken")
    app_stopped = resolve("/run/current-system/specialisation/app-stopped")
    agent_changed = resolve("/run/current-system/specialisation/agent-changed")

    with subtest("a good deploy switches the node"):
        publish(1, good)
        outcome, _ = run_agent()
        assert outcome == "switched", outcome
        assert current() == good, current()
        assert version() == "good"

    with subtest("a broken deploy is rolled back automatically"):
        publish(2, broken)
        outcome, _ = run_agent()
        assert outcome == "rolled_back", outcome
        assert current() == good, current()
        assert version() == "good"
        machine.succeed("systemctl is-system-running --wait")
        # A rollback is a handled outcome: the agent unit does not fail.
        machine.fail("systemctl is-failed --quiet fleet-pull-agent.service")

    with subtest("the known-bad closure is not retried"):
        outcome, _ = run_agent()
        assert outcome == "skipped", outcome
        assert current() == good

    with subtest("a cleanly stopped critical unit is caught and rolled back"):
        publish(3, app_stopped)
        outcome, _ = run_agent()
        assert outcome == "rolled_back", outcome
        assert current() == good, current()
        machine.succeed("systemctl is-active app.service")

    with subtest("an older manifest cannot downgrade the node"):
        publish(0, base)
        outcome, _ = run_agent()
        assert outcome == "skipped", outcome
        assert current() == good

    with subtest("failed and paused manifests are not applied"):
        publish(4, None, status="failed", enabled=False)
        outcome, _ = run_agent()
        assert outcome == "skipped", outcome
        publish(5, agent_changed, enabled=False)
        outcome, _ = run_agent()
        assert outcome == "skipped", outcome
        assert current() == good
        assert last_seq() == 1, last_seq()

    with subtest("a deploy that changes the agent's own unit completes"):
        # Re-enabling the paused manifest resumes it under the same seq.
        publish(5, agent_changed)
        outcome, invocation = run_agent()
        assert outcome == "switched", outcome
        assert current() == agent_changed, current()
        assert version() == "agent-changed"
        # The run that started the switch is the one that finished it: the
        # invocation captured at start logged the switch result itself. (The
        # unit's InvocationID property is not compared after the run: systemd
        # does not keep it stable once the unit is inactive.)
        machine.wait_until_succeeds(
            f"journalctl _SYSTEMD_INVOCATION_ID={invocation} -o cat | grep -F '\"action\": \"switched\"'"
        )
        # The new unit definition is loaded for the next run.
        assert "FLEET_TEST_MARKER=changed" in machine.succeed(
            "systemctl show -P Environment fleet-pull-agent.service"
        )
        machine.succeed("systemctl is-system-running --wait")
        assert last_seq() == 5, last_seq()

    with subtest("deploy outcomes are exported as metrics"):
        metrics = machine.succeed(f"cat {STATE_DIR}/fleet_deploy.prom")
        assert 'fleet_deploy_runs_total{action="switched"} 2' in metrics, metrics
        assert 'fleet_deploy_runs_total{action="rolled_back"} 2' in metrics, metrics
        assert 'fleet_deploy_runs_total{action="skipped"} 4' in metrics, metrics
        assert "fleet_deploy_reboot_pending 0" in metrics, metrics

    with subtest("garbage collection waits for the deploy lock"):
        lock = "/run/lock/fleet-nix-store.lock"
        machine.succeed(f"systemd-run --unit=hold-lock $(command -v flock) {lock} $(command -v sleep) 10")
        machine.wait_until_fails(f"flock --nonblock {lock} true")
        machine.succeed("systemctl start --no-block nix-gc.service")
        machine.sleep(3)
        assert machine.succeed("systemctl show -P ActiveState nix-gc.service").strip() == "activating"
        machine.wait_until_succeeds(
            "systemctl show -P ActiveState nix-gc.service | grep -qx inactive", timeout=60
        )
        assert machine.succeed("systemctl show -P Result nix-gc.service").strip() == "success"
  '';
}
