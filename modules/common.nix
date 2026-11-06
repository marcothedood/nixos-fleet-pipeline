# Baseline every fleet node gets.
{ config, lib, pkgs, fleet, ... }:

{
  system.stateVersion = "26.05";

  nix.settings = {
    experimental-features = [ "nix-command" "flakes" ];
    # Nodes never build: every closure comes from the fleet cache, and Nix
    # refuses anything not signed by a key listed here.
    substituters = [ fleet.cache.url ];
    trusted-public-keys = [ fleet.cache.publicKey ];
    max-jobs = 0;
  };
  nix.gc = {
    automatic = true;
    options = "--delete-older-than 14d"; # keeps enough generations to roll back
  };

  services.fleet-pull-agent = {
    enable = true;
    manifestUrl = fleet.manifestUrl;
  };

  # Deploy metrics are picked up by node_exporter's textfile collector and
  # scraped over the private network, never from the public internet.
  services.prometheus.exporters.node = {
    enable = true;
    enabledCollectors = [ "systemd" "textfile" ];
    extraFlags = [
      "--collector.textfile.directory=${dirOf config.services.fleet-pull-agent.metricsFile}"
    ];
  };

  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "prohibit-password";
    };
  };

  networking.firewall.enable = true;
}
