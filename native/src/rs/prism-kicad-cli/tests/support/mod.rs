//! Shared helpers of the `prism-kicad` integration tests: running the binary,
//! the committed fixtures and their KiCad recordings, and checking every
//! stdout document and stderr line against the contracts.

#![allow(dead_code, reason = "each test binary uses a subset of the helpers")]

use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

/// The repository root.
pub fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../..")
        .canonicalize()
        .unwrap()
}

/// The `prism-kicad` binary under test.
pub fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_prism-kicad")
}

/// A fresh, empty scratch directory.
pub fn scratch(label: &str) -> PathBuf {
    static SERIAL: AtomicU64 = AtomicU64::new(0);
    let serial = SERIAL.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!(
        "prism-kicad-test-{label}-{}-{serial}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// One committed fixture project.
#[derive(Clone, Debug)]
pub struct Fixture {
    /// Repository-relative directory, the recording key.
    pub dir: String,
    /// The `.kicad_pro`.
    pub project: PathBuf,
    /// The recorded refilled board, when KiCad could refill it.
    pub refilled: Option<PathBuf>,
    /// The recorded component GLB.
    pub glb: Option<PathBuf>,
}

fn projects(dir: &Path, found: &mut Vec<PathBuf>) {
    let mut entries: Vec<PathBuf> = std::fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    entries.sort();
    for path in entries {
        if path.is_dir() {
            projects(&path, found);
        } else if path
            .extension()
            .is_some_and(|extension| extension == "kicad_pro")
        {
            found.push(path);
        }
    }
}

/// Inflate a `.zlib` recording into `scratch`; return its path.
pub fn recording(relative: &str, name: &str, into: &Path) -> Option<PathBuf> {
    let source = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/recordings")
        .join(relative)
        .join(format!("{name}.zlib"));
    let compressed = std::fs::read(source).ok()?;
    let bytes = miniz_oxide::inflate::decompress_to_vec_zlib(&compressed).unwrap();
    let dir = into.join(relative);
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join(name);
    std::fs::write(&path, bytes).unwrap();
    Some(path)
}

/// Every committed fixture project, with its recordings inflated under
/// `scratch`.
pub fn fixtures(scratch: &Path) -> Vec<Fixture> {
    let repo = repo();
    let mut found = Vec::new();
    for root in [
        "fixtures/release-studio",
        "backend/tests/fixtures/design_variants",
        "backend/tests/fixtures/system_builder",
    ] {
        projects(&repo.join(root), &mut found);
    }
    found
        .into_iter()
        .map(|project| {
            let dir = project
                .parent()
                .unwrap()
                .strip_prefix(&repo)
                .unwrap()
                .to_string_lossy()
                .replace('\\', "/");
            let stem = project.file_stem().unwrap().to_string_lossy().into_owned();
            Fixture {
                refilled: recording(&dir, &format!("{stem}.kicad_pcb"), scratch),
                glb: recording(&dir, "components.glb", scratch),
                dir,
                project,
            }
        })
        .collect()
}

/// A finished invocation.
#[derive(Debug)]
pub struct Run {
    /// Exit code (`-1` for a signal).
    pub code: i32,
    /// stdout bytes.
    pub stdout: Vec<u8>,
    /// stdout as JSON, when there is one document.
    pub document: Value,
    /// stderr lines as JSON.
    pub diagnostics: Vec<Value>,
}

impl Run {
    /// The stdout document's `code`.
    pub fn code_field(&self) -> &str {
        self.document["code"].as_str().unwrap_or_default()
    }
}

/// Check one stdout document or stderr line against its schema.
pub fn check_contract(document: &Value) {
    let schema = document["schema"].as_str().unwrap_or_default();
    if let Err(violation) = prism_bundle::schema::validate(schema, document) {
        panic!("{schema} breaks its schema: {violation:?}\n{document}");
    }
}

fn finish(output: Output) -> Run {
    let document = if output.stdout.is_empty() {
        Value::Null
    } else {
        assert!(output.stdout.ends_with(b"\n"), "stdout ends in one LF");
        let document: Value = serde_json::from_slice(&output.stdout).unwrap();
        check_contract(&document);
        document
    };
    let diagnostics: Vec<Value> = output
        .stderr
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| {
            let value: Value = serde_json::from_slice(line).unwrap_or_else(|_| {
                panic!(
                    "a stderr line is not JSON: {}",
                    String::from_utf8_lossy(line)
                )
            });
            check_contract(&value);
            value
        })
        .collect();
    Run {
        code: output.status.code().unwrap_or(-1),
        stdout: output.stdout,
        document,
        diagnostics,
    }
}

/// Run `prism-kicad args` in `cwd` (the repository root when `None`).
pub fn run_in(cwd: Option<&Path>, args: &[&str]) -> Run {
    let output = Command::new(binary())
        .args(args)
        .current_dir(cwd.map_or_else(repo, Path::to_path_buf))
        .stdin(std::process::Stdio::null())
        .output()
        .unwrap();
    finish(output)
}

/// Run `prism-kicad args` from the repository root.
pub fn run(args: &[&str]) -> Run {
    run_in(None, args)
}

/// `path` as a `&str`.
pub fn s(path: &Path) -> &str {
    path.to_str().unwrap()
}

/// The archive bytes of the stages a fixture can reach, built into `out`
/// with `threads`; the stdout of each stage.
pub fn build_all(
    fixture: &Fixture,
    out: &Path,
    threads: &str,
    cwd: Option<&Path>,
) -> (Vec<u8>, Vec<Vec<u8>>) {
    let archive = out.join("bundle.prism");
    let mut stdouts = Vec::new();
    let parse = run_in(
        cwd,
        &[
            "bundle",
            "build",
            s(&fixture.project),
            "--stage",
            "parse",
            "-o",
            s(&archive),
            "--threads",
            threads,
            "--quiet",
        ],
    );
    assert!(
        matches!(parse.code, 0 | 9),
        "{}: parse exited {}: {}",
        fixture.dir,
        parse.code,
        parse.document
    );
    stdouts.push(parse.stdout.clone());
    let board_pending = parse.document["stages"][1]["status"] == "pending";
    if let (true, Some(refilled)) = (board_pending, &fixture.refilled) {
        let board = run_in(
            cwd,
            &[
                "bundle",
                "build",
                "--stage",
                "board",
                "--in",
                s(&archive),
                "-o",
                s(&archive),
                "--refilled-board",
                s(refilled),
                "--refill-tool",
                "10.0.6",
                "--threads",
                threads,
                "--quiet",
            ],
        );
        assert!(
            matches!(board.code, 0 | 9),
            "{}: board exited {}: {}",
            fixture.dir,
            board.code,
            board.document
        );
        stdouts.push(board.stdout.clone());
        if let (0, Some(glb)) = (board.code, &fixture.glb) {
            let models = run_in(
                cwd,
                &[
                    "bundle",
                    "build",
                    "--stage",
                    "models",
                    "--in",
                    s(&archive),
                    "-o",
                    s(&archive),
                    "--models-glb",
                    s(glb),
                    "--models-tool",
                    "10.0.6",
                    "--threads",
                    threads,
                    "--quiet",
                ],
            );
            assert_eq!(
                models.code, 0,
                "{}: models: {}",
                fixture.dir, models.document
            );
            stdouts.push(models.stdout.clone());
        }
    }
    (std::fs::read(&archive).unwrap(), stdouts)
}

/// SHA-256 hex.
pub fn sha256(bytes: &[u8]) -> String {
    prism_bundle::generator::sha256_hex(bytes)
}
