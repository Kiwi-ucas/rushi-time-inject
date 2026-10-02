{
  description = "rushi-time-inject — model.before time-injection hook for rushi";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    fenix = {
      url = "github:nix-community/fenix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, fenix }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      pkgLib = nixpkgs.lib;

      buildFor = system:
        let
          pkgs = import nixpkgs {
            inherit system;
            overlays = [ fenix.overlays.default ];
          };
          rustToolchain = fenix.packages.${system}.stable.withComponents [
            "cargo" "clippy" "rust-src" "rustc" "rustfmt" "rust-analyzer"
          ];

          # Build one standalone cargo crate from a subpath of this
          # flake's source tree. The output binary name comes from the
          # crate's [[bin]] name in Cargo.toml (harness-hook-time-inject),
          # not from crateName.
          buildCrate = { crateDir, crateName }:
            pkgs.rustPlatform.buildRustPackage {
              pname = crateName;
              version = "0.1.0";
              src = "${self}";
              buildAndTestSubdir = crateDir;
              cargoRoot = crateDir;
              nativeBuildInputs = [ rustToolchain ];
              cargoLock = { lockFile = "${self}/${crateDir}/Cargo.lock"; };
              doCheck = false;
            };
        in
        # Per-system package attrset. `nix build .#<name>` resolves
        # packages.<host>.<name>.
        rec {
          # The hook (a bare buildRustPackage result IS the hook source);
          # its binary name must match the `command` in config.toml
          # [hooks.defs.time-inject].
          hook-time-inject = buildCrate { crateDir = "hook-time-inject"; crateName = "hook-time-inject"; };

          default = hook-time-inject;
        };
    in
    {
      packages = pkgLib.genAttrs supportedSystems (system: buildFor system);
    };
}
