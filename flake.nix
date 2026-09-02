{
  description = "wdotool - drop-in xdotool clone for Wayland";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in
    {
      packages = forAll (pkgs: rec {
        wdotool = pkgs.python3Packages.buildPythonApplication {
          pname = "wdotool";
          version = "0.1.0";
          src = ./.;
          pyproject = true;
          build-system = [ pkgs.python3Packages.setuptools ];
          postInstall = "ln -s $out/bin/wdotool $out/bin/xdotool";
        };
        default = wdotool;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            python3
            # in-sandbox compositor testbed
            sway foot grim jq
            # the real thing, for parity reference (help text, manpage)
            xdotool man
            # VM lifecycle + demo gif
            qemu_kvm xorriso cloud-utils openssh curl
            ffmpeg imagemagick gifsicle
          ];
        };
      });
    };
}
