{
  description = "fuckwayland - the X11 power tools (xdotool, wmctrl, xprop, xrandr) as drop-in clones for Wayland";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in
    {
      packages = forAll (pkgs: rec {
        wdotool = pkgs.python3Packages.buildPythonApplication {
          pname = "fuckwayland";
          version = "0.1.0";
          src = ./.;
          pyproject = true;
          build-system = [ pkgs.python3Packages.setuptools ];
          postInstall = ''
            ln -s $out/bin/wdotool $out/bin/xdotool
            if [ -e $out/bin/wwmctl ]; then ln -s $out/bin/wwmctl $out/bin/wmctrl; fi
            if [ -e $out/bin/wxprop ]; then ln -s $out/bin/wxprop $out/bin/xprop; fi
            if [ -e $out/bin/wxrandr ]; then ln -s $out/bin/wxrandr $out/bin/xrandr; fi
          '';
        };
        default = wdotool;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            python3
            # in-sandbox compositor testbed (incl. XWayland legacy-app plane)
            sway foot grim jq
            xwayland xterm xprop xwininfo xeyes xrandr
            # the real things, for parity reference (help text, manpage, behavior)
            xdotool wmctrl man
            # VM lifecycle + demo gif
            qemu_kvm xorriso cloud-utils openssh curl
            ffmpeg imagemagick gifsicle
          ];
        };
      });
    };
}
