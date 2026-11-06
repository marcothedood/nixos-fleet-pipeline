{ modulesPath, ... }:

{
  imports = [
    "${modulesPath}/virtualisation/amazon-image.nix"
    ../../modules/common.nix
    ../../modules/web.nix
  ];
}
