# native/scripts/ — native/'s tooling

Prism's replacements for the fork's Wavenumber infrastructure (ADR §4.2, WP-F3), the CI entry scripts the quality gate calls, the Prism contract tools and the fork's test-vector generators.

- **CI entry scripts** (`.github/workflows/dev-quality-gate.yml`; each runs the same way locally, from anywhere):
  - `ci_native_platform.sh`: the `native-platforms` job (linux arm64, macOS, Windows). The producer, pack, bundle and CLI tests.
  - `ci_native_wasm32.sh`: the `native-wasm32` job. The wasm32 builds, clippy and the wasm-bindgen tests. Needs Node 24 and a `wasm-bindgen-cli` at the `Cargo.lock` version.
  - `ci_native_hygiene.sh`: the `native-hygiene` job, the fast check every long native job waits for. It runs the ratchet and its tests, `parity_corpus.py check`, then `npm ci && npm run check:prism-contracts`. It must not compile Rust.
- **Windows from Docker:** `windows_gnu_container.sh` builds and tests `native/` for `x86_64-pc-windows-gnu` (mingw-w64) in the pinned Rust image, runs the Windows test executables under Wine, and exports `prism-kicad.exe`. It is the dev route on a Windows host without Visual Studio; CI builds the MSVC target.
- **Rust hygiene ratchet:** `rust_hygiene.sh` runs `rust_hygiene.py` with its hashed pins (`rust_hygiene_requirements.txt`). `test_rust_hygiene.py` holds its tests.
  - It checks `../rust-hygiene.toml`'s limits against `../rust-hygiene-baseline.json`.
  - It ports the measurement of wn-dev-std, MIT. On this tree it reports the same violations as wn-dev-std.
  - `--prune` only tightens the baseline. `--record "why"` is a reviewed reset only.
- **Corpus:** `parity_corpus.py` names Prism's parity corpus.
  - The committed fixture roots (which `native/parity/fixtures.json` draws from) and `PRISM_PARITY_CORPUS` for private roots.
  - `check` checks that the committed roots are present. `sexpr-gate` round-trips every source through `prism-kicad-core`'s `sexpr_corpus_gate` example (`--debug` skips the release build; CI's native-workspace job uses it).
- **KiCad oracle:** `kicad_cli_oracle.py` uses the worker image's `kicad-cli`.
  - It resolves `kicad-cli` like `backend/app/services/kicad_tools.py`, or runs it inside an image with `--image`.
  - `capture` writes the oracle documents of ADR §7: a netlist, board stats and a record of the run.
- **Releases:** `native_release.py` builds the `prism-kicad` binaries (linux amd64/arm64, macOS arm64) and the `prism-kicad-wasm` npm package.
  - `dry-run --image-check` runs the whole release locally. `.github/workflows/native-release.yml` publishes it.
  - `check_release_artifact_paths.py` rejects artifacts that embed local paths.
- **Prism contracts:** `contracts/` (`check-contract.mjs`, `build.mjs`; `../contracts/prism/README.md`).
- **Fork generators:** `generate_*.py`, `synthetic_corpus/`, `build_kicad_ir_coverage_report.py` and `generate-contract-bindings.mjs`.
  - They regenerate the committed test vectors and fixtures.
  - Most need the fork's Python `kicad_monkey`, which was not imported (ADR §4.2). They are kept as the vectors' provenance.
