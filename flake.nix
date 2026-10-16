{
  description = "anki-claude-bridge dev environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in {
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.python313
            pkgs.jujutsu
            pkgs.git
          ];

          # anki and mcp 2.x are not reliably in nixpkgs, so they come from
          # PyPI into a project-local venv pinned by requirements.txt.
          # The prebuilt anki wheel needs libstdc++ at runtime.
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib ];

          shellHook = ''
            if [ ! -d .venv ]; then
              python -m venv .venv
            fi
            . .venv/bin/activate
            if ! cmp -s requirements.txt .venv/.requirements.installed; then
              pip install -q -r requirements.txt && cp requirements.txt .venv/.requirements.installed
            fi
          '';
        };
      });
    };
}
