{ config, lib, pkgs, ... }:

let
  cfg = config.services.fleet-pull-agent;

  # Held by the agent for a whole run and by nix-gc while it collects.
  # Between realising the target and finishing a rollback, the system the
  # agent needs may be referenced by nothing but the agent itself, and a
  # concurrent garbage collection could delete it mid-switch.
  storeLock = "/run/lock/fleet-nix-store.lock";
  flock = "${pkgs.util-linux}/bin/flock";
in
{
  options.services.fleet-pull-agent = {
    enable = lib.mkEnableOption "the pull-based deploy agent";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The fleet-pull-agent package.";
    };

    manifestUrl = lib.mkOption {
      type = lib.types.str;
      example = "https://raw.githubusercontent.com/acme/fleet/deploy/manifest.json";
      description = "Where CI publishes the deploy manifest (https:// or file://).";
    };

    interval = lib.mkOption {
      type = lib.types.str;
      default = "5min";
      description = "How often the node polls the manifest.";
    };

    healthChecks = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "systemctl is-system-running --wait" ];
      description = ''
        Commands run after a switch. Any non-zero exit rolls the node back
        to the system that ran before.
      '';
    };

    criticalUnits = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "nginx" ];
      description = ''
        Units that must be active after a switch, checked with
        `systemctl is-active`. A unit that stopped cleanly is "inactive",
        not "failed", so `systemctl is-system-running` does not notice it.
      '';
    };

    healthUrls = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "http://127.0.0.1/healthz" ];
      description = ''
        URLs that must answer with a 2xx or 3xx status after a switch. A URL
        that gives no response is retried once with a longer timeout; an
        HTTP error status is not retried.
      '';
    };

    settleSeconds = lib.mkOption {
      type = lib.types.int;
      default = 10;
      description = "Seconds to wait after switching before running health checks.";
    };

    switchAction = lib.mkOption {
      type = lib.types.enum [ "switch" "test" "boot" ];
      default = "switch";
      description = ''
        Action passed to switch-to-configuration for a live switch. A target
        with a different systemd major version is always staged with `boot`.
      '';
    };

    metricsFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "/var/lib/prometheus-node-exporter-text-files/fleet_deploy.prom";
      description = "Prometheus textfile collector output, or null to disable.";
    };

    extraPath = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [ ];
      example = lib.literalExpression "[ pkgs.curl ]";
      description = "Extra packages available to health checks.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.fleet-pull-agent = {
      description = "Pull the fleet manifest and switch to this host's system closure";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      # The agent runs the switch that may change the agent's own unit, so
      # that switch must leave the unit alone while it is running:
      # - restartIfChanged = false: the switch does not restart a changed
      #   unit, which would kill the agent halfway through.
      # - Type = "exec": the unit is "active" while the agent runs. A
      #   oneshot unit would be "activating", and the switch starts changed
      #   units that are not active: it would wait, with no timeout, for a
      #   start job on the very unit that is running the switch.
      # Do not set stopIfChanged = false either: it moves the unit into the
      # set of units the switch starts, which deadlocks the same way.
      restartIfChanged = false;
      unitConfig.X-StopOnRemoval = false;

      path = [ config.nix.package config.systemd.package pkgs.coreutils ] ++ cfg.extraPath;

      serviceConfig = {
        Type = "exec";
        StateDirectory = "fleet-pull-agent";
        ExecStart = lib.escapeShellArgs (
          # Waits up to 30 minutes for a running garbage collection.
          [ flock "-w" "1800" storeLock ]
          ++ [
            (lib.getExe cfg.package)
            "--manifest-url"
            cfg.manifestUrl
            "--hostname"
            config.networking.hostName
            "--settle-seconds"
            (toString cfg.settleSeconds)
            "--switch-action"
            cfg.switchAction
          ]
          ++ lib.optionals (cfg.metricsFile != null) [ "--metrics-file" cfg.metricsFile ]
          ++ lib.concatMap (check: [ "--health-check" check ]) cfg.healthChecks
          ++ lib.concatMap (unit: [ "--critical-unit" unit ]) cfg.criticalUnits
          ++ lib.concatMap (url: [ "--health-url" url ]) cfg.healthUrls
        );
      };
    };

    # Same command as the upstream nix-gc service, run under the agent's
    # lock so a collection never overlaps a switch or a rollback.
    systemd.services.nix-gc = lib.mkIf config.nix.enable {
      script = lib.mkForce
        "exec ${flock} -w 3600 ${storeLock} ${config.nix.package.out}/bin/nix-collect-garbage ${config.nix.gc.options}";
    };

    systemd.timers.fleet-pull-agent = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "2min";
        OnUnitActiveSec = cfg.interval;
        RandomizedDelaySec = "30s"; # nodes do not all switch in the same second
      };
    };

    systemd.tmpfiles.rules = lib.optional (cfg.metricsFile != null)
      "d ${dirOf cfg.metricsFile} 0755 root root -";
  };
}
