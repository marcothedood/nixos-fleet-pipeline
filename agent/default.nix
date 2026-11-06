{ lib, stdenvNoCC, makeWrapper, python3 }:

stdenvNoCC.mkDerivation {
  pname = "fleet-pull-agent";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [ ./pull_agent.py ./test_pull_agent.py ];
  };

  nativeBuildInputs = [ makeWrapper ];

  # Standard library only: the agent has no Python dependencies to pin.
  installPhase = ''
    runHook preInstall
    install -Dm644 pull_agent.py $out/libexec/fleet-pull-agent/pull_agent.py
    makeWrapper ${python3.interpreter} $out/bin/fleet-pull-agent \
      --add-flags $out/libexec/fleet-pull-agent/pull_agent.py
    runHook postInstall
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    PYTHONDONTWRITEBYTECODE=1 ${python3.interpreter} -m unittest -v test_pull_agent
    $out/bin/fleet-pull-agent --help > /dev/null
    runHook postInstallCheck
  '';

  meta = {
    description = "Pull-based NixOS deploy agent with health checks and automatic rollback";
    license = lib.licenses.mit;
    mainProgram = "fleet-pull-agent";
    platforms = lib.platforms.linux;
  };
}
