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
          version = "0.4.0";
          src = ./.;
          pyproject = true;
          build-system = [ pkgs.python3Packages.setuptools ];

          # pyproject.toml declares six console scripts, warandr among them,
          # so this derivation has always installed $out/bin/warandr -- it
          # just could not run: warandr is the one GUI here, and `import gi`
          # needs PyGObject while gi.require_version("Gtk", "3.0") needs the
          # Gtk/Gdk/Pango typelibs, which are found through $GI_TYPELIB_PATH
          # and nowhere else. These four attributes are the standard nixpkgs
          # arrangement for a GTK 3 Python application (its own arandr package
          # is built exactly this way):
          #
          #   gobject-introspection  collects the typelib directory of every
          #                          buildInput into $GI_TYPELIB_PATH,
          #   wrapGAppsHook3         turns that -- plus the XDG_DATA_DIRS entry
          #                          for the GSettings schemas Gtk.Settings
          #                          reads on startup -- into wrapper arguments,
          #   gtk3 + schemas         are what there is to collect,
          #   pygobject3             is the `gi` module itself, at run time.
          #
          # The four CLI clones import none of this; they stay stdlib-only and
          # the extra environment is inert for them.
          nativeBuildInputs = [
            pkgs.gobject-introspection
            pkgs.wrapGAppsHook3
          ];
          buildInputs = [
            pkgs.gsettings-desktop-schemas
            pkgs.gtk3
          ];
          dependencies = [ pkgs.python3Packages.pygobject3 ];

          # buildPythonApplication writes its own wrapper around every script
          # in postFixup. Let it write this one too rather than wrapping twice:
          # with dontWrapGApps the hook stops at assembling $gappsWrapperArgs
          # in preFixup, and makeWrapperArgs hands them to the Python wrapper
          # that runs after it.
          dontWrapGApps = true;
          makeWrapperArgs = [ "\${gappsWrapperArgs[@]}" ];

          # No --prefix PATH here, deliberately: warandr and the clones look
          # up the real xrandr/xdotool/wmctrl/xprop on the *user's* PATH (see
          # fwcommon/passthrough.py), and a store xrandr baked into the wrapper
          # would change which binary the handover finds.
          postInstall = ''
            ln -s $out/bin/wdotool $out/bin/xdotool
            if [ -e $out/bin/wwmctl ]; then ln -s $out/bin/wwmctl $out/bin/wmctrl; fi
            if [ -e $out/bin/wxprop ]; then ln -s $out/bin/wxprop $out/bin/xprop; fi
            if [ -e $out/bin/wxrandr ]; then ln -s $out/bin/wxrandr $out/bin/xrandr; fi
            if [ -e $out/bin/warandr ]; then ln -s $out/bin/warandr $out/bin/arandr; fi
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
