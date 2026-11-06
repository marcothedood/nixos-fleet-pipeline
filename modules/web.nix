# A stand-in workload: nginx with a health endpoint the pull agent checks.
{ config, lib, pkgs, ... }:

let
  site = pkgs.writeTextDir "index.html" ''
    <!doctype html><title>${config.networking.hostName}</title>
    <h1>${config.networking.hostName}</h1>
    <p>Served from ${config.system.nixos.label}</p>
  '';
in
{
  services.nginx = {
    enable = true;
    recommendedGzipSettings = true;
    recommendedOptimisation = true;
    virtualHosts.default = {
      default = true;
      root = site;
      locations."= /healthz".return = "200 'ok'";
    };
  };

  networking.firewall.allowedTCPPorts = [ 80 ];

  services.fleet-pull-agent = {
    # nginx stopped cleanly would still leave the system "running".
    criticalUnits = [ "nginx" ];
    healthUrls = [ "http://127.0.0.1/healthz" ];
  };
}
