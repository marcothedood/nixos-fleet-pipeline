# The whole fleet in one place. Replace the placeholders with your own values.
{
  hosts = {
    web-x86 = { system = "x86_64-linux"; };
    web-arm = { system = "aarch64-linux"; }; # e.g. EC2 Graviton
  };

  # Attic cache that CI pushes to and nodes substitute from.
  cache = {
    url = "https://attic.example.com/fleet";
    publicKey = "fleet:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
  };

  # Written by the publish job on every green main build.
  manifestUrl = "https://raw.githubusercontent.com/marcothedood/nixos-fleet-pipeline/deploy/manifest.json";
}
