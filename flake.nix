{
  description = "Pull-based NixOS fleet deploys: CI builds the exact closures, nodes pull, switch and roll back";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;
      fleet = import ./fleet.nix;
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      mkHost = name: { system }: lib.nixosSystem {
        specialArgs = { inherit fleet; };
        modules = [
          { nixpkgs.hostPlatform = system; networking.hostName = name; }
          self.nixosModules.default
          (./hosts + "/${name}")
        ];
      };
    in
    {
      nixosModules.default = { lib, pkgs, ... }: {
        imports = [ ./modules/pull-agent.nix ];
        services.fleet-pull-agent.package =
          lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.fleet-pull-agent;
      };

      nixosConfigurations = lib.mapAttrs mkHost fleet.hosts;

      # The deploy contract. CI evaluates this to get, for every host, the
      # store path of the system it must run. The build jobs build exactly
      # these paths, the cache serves exactly these paths, and the node
      # switches to exactly these paths: one hash from PR to production.
      fleet = {
        toplevels = lib.mapAttrs (_: c: c.config.system.build.toplevel) self.nixosConfigurations;
        hosts = lib.mapAttrs (_: h: h.system) fleet.hosts;
      };

      packages = forAllSystems (pkgs: rec {
        fleet-pull-agent = pkgs.callPackage ./agent { };
        default = fleet-pull-agent;
      });

      checks = forAllSystems (pkgs: {
        agent = self.packages.${pkgs.stdenv.hostPlatform.system}.fleet-pull-agent;
        pull-agent-vm = import ./tests/pull-agent.nix { inherit pkgs self; };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.attic-client pkgs.jq pkgs.python3 pkgs.nixpkgs-fmt ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixpkgs-fmt);
    };
}
