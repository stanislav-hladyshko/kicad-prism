//! Every exit path of cli-spec.md §4 and the documents each prints (§3.1).
//! The support module checks every stdout document and stderr line against
//! its JSON Schema (§11 item 1).

mod support;

use prism_bundle::zip::ZipWriter;
use prism_bundle::{BundleReader, Limits, Work};
use serde_json::Value;
use std::path::{Path, PathBuf};
use support::{Run, recording, repo, run, s, scratch};
#[cfg(unix)]
use support::{fixtures, run_in};

fn usb_pd() -> PathBuf {
    repo().join("fixtures/release-studio/usb-pd/USB-PD-Trigger-Board.kicad_pro")
}

fn parse_into(project: &Path, out: &Path) -> Run {
    run(&[
        "bundle",
        "build",
        s(project),
        "--stage",
        "parse",
        "-o",
        s(out),
        "--quiet",
    ])
}

/// Rewrite `archive` with `edit` applied to its manifest JSON and members.
fn rewrite(archive: &Path, out: &Path, edit: impl FnOnce(&mut Value, &mut Vec<(String, Vec<u8>)>)) {
    let limits = Limits::defaults(Work::Verify);
    let reader = BundleReader::open(std::fs::read(archive).unwrap(), &limits).unwrap();
    let mut manifest = reader.manifest_read().value.clone();
    let mut members: Vec<(String, Vec<u8>)> = reader
        .manifest()
        .members
        .iter()
        .map(|member| {
            (
                member.path.0.clone(),
                reader.member(&member.path).unwrap().unwrap(),
            )
        })
        .collect();
    edit(&mut manifest, &mut members);
    let mut writer = ZipWriter::new(Vec::new(), &limits);
    let mut bytes = serde_json::to_vec(&manifest).unwrap();
    bytes.push(b'\n');
    writer.add("bundle.json", &bytes, &limits).unwrap();
    for (path, data) in &members {
        writer.add(path, data, &limits).unwrap();
    }
    std::fs::write(out, writer.finish(&limits).unwrap().0).unwrap();
}

#[test]
fn success_paths_exit_zero() {
    let dir = scratch("exit-zero");
    let archive = dir.join("a.prism");
    let parse = parse_into(&usb_pd(), &archive);
    assert_eq!(parse.code, 0);
    assert_eq!(parse.document["stages"][0]["status"], "complete");
    assert_eq!(run(&["bundle", "verify", s(&archive)]).code, 0);
    let out = scratch("exit-zero-extract");
    let extract = run(&[
        "bundle",
        "extract",
        s(&archive),
        "sources/",
        "bundle.json",
        "-o",
        s(&out),
    ]);
    assert_eq!(extract.code, 0, "{}", extract.document);
    assert!(
        out.join("sources/Subsheets/Output-Connector.kicad_sch")
            .is_file()
    );
    let again = run(&[
        "bundle",
        "extract",
        s(&archive),
        "sources/",
        "bundle.json",
        "-o",
        s(&out),
    ]);
    assert_eq!(again.stdout, extract.stdout, "extract is idempotent");
    let help = std::process::Command::new(support::binary())
        .arg("--help")
        .output()
        .unwrap();
    assert!(help.status.success() && String::from_utf8_lossy(&help.stdout).contains("usage:"));
}

#[test]
fn usage_errors_exit_two() {
    for args in [
        vec!["frobnicate"],
        vec!["bundle", "build", "--stage", "parse", "-o", "x"],
        vec!["bundle", "verify"],
        vec!["revision-key", ".", "--project", "x.kicad_pro"],
    ] {
        let failed = run(&args);
        assert_eq!(failed.code, 2, "{args:?}");
        assert_eq!(failed.document["category"], "usage");
        let last = failed.diagnostics.last().unwrap();
        assert_eq!(
            last["code"], failed.document["code"],
            "the last error line repeats stdout"
        );
    }
}

#[test]
fn input_errors_exit_three() {
    let dir = scratch("exit-three");
    let missing = run(&["bundle", "verify", s(&dir.join("none.prism"))]);
    assert_eq!((missing.code, missing.code_field()), (3, "input-not-found"));
    let not_file = run(&["bundle", "verify", s(&dir)]);
    assert_eq!(
        (not_file.code, not_file.code_field()),
        (3, "input-not-regular-file")
    );
    let not_pro = run(&[
        "bundle",
        "build",
        s(&dir.join("x.kicad_pcb")),
        "--stage",
        "parse",
        "-o",
        s(&dir.join("o")),
    ]);
    assert_eq!(
        (not_pro.code, not_pro.code_field()),
        (3, "project-not-kicad-pro")
    );
    let config = dir.join("config.json");
    std::fs::write(&config, r#"{"schema":"prism.revision_key.effective_config.a1","project_file":"USB-PD-Trigger-Board.kicad_pro","excluded_dirs":[],"zone_fills":"refill"}"#).unwrap();
    let invalid = run(&[
        "bundle",
        "build",
        s(&usb_pd()),
        "--stage",
        "parse",
        "-o",
        s(&dir.join("o")),
        "--config",
        s(&config),
    ]);
    assert_eq!((invalid.code, invalid.code_field()), (3, "config-invalid"));
    let garbage = dir.join("garbage.prism");
    std::fs::write(&garbage, b"PK\x05\x06 not a bundle").unwrap();
    let extract = run(&[
        "bundle",
        "extract",
        s(&garbage),
        "bundle.json",
        "-o",
        s(&dir),
    ]);
    assert_eq!((extract.code, extract.code_field()), (3, "bundle-invalid"));
    assert!(!dir.join("o").exists(), "-o is untouched on failure");
}

fn source_tree(files: &[(&str, &str)]) -> PathBuf {
    let dir = scratch("tree");
    for (path, text) in files {
        let path = dir.join(path);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }
    dir
}

const SHEET: &str = "(kicad_sch (version 20260306) (generator \"eeschema\") (uuid \"6c5f0000-0000-4000-8000-000000000001\") (paper \"A4\") (lib_symbols))\n";

#[test]
fn source_root_rules_exit_three() {
    #[cfg(unix)]
    {
        let linked = source_tree(&[("p.kicad_pro", "{}"), ("p.kicad_sch", SHEET)]);
        std::os::unix::fs::symlink("p.kicad_sch", linked.join("q.kicad_sch")).unwrap();
        let link = run(&[
            "bundle",
            "build",
            s(&linked.join("p.kicad_pro")),
            "--stage",
            "parse",
            "-o",
            s(&linked.join("o.prism")),
        ]);
        assert_eq!(
            (link.code, link.code_field()),
            (3, "source-not-regular-file")
        );
    }
    let escaping = SHEET.replace(
        "(lib_symbols))",
        "(lib_symbols) (sheet (at 0 0) (size 1 1) (uuid \"6c5f0000-0000-4000-8000-000000000002\") (property \"Sheetname\" \"S\") (property \"Sheetfile\" \"../outside.kicad_sch\")))",
    );
    let outside = source_tree(&[("p/p.kicad_pro", "{}"), ("p/p.kicad_sch", &escaping)]);
    let escaped = run(&[
        "bundle",
        "build",
        s(&outside.join("p/p.kicad_pro")),
        "--stage",
        "parse",
        "-o",
        s(&outside.join("o.prism")),
    ]);
    assert_eq!(
        (escaped.code, escaped.code_field()),
        (3, "source-outside-root")
    );
    assert_eq!(escaped.document["location"]["path"], "sources/p.kicad_sch");
}

#[test]
fn recorded_content_failures_exit_nine() {
    let dir = scratch("exit-nine");
    let malformed =
        repo().join("backend/tests/fixtures/design_variants/malformed/broken.kicad_pro");
    let failed = parse_into(&malformed, &dir.join("m.prism"));
    assert_eq!(failed.code, 9);
    let error = &failed.document["stages"][0]["error"];
    assert_eq!(error["code"], "source-unparseable");
    assert_eq!(error["location"]["path"], "sources/broken.kicad_sch");
    assert_eq!(
        (
            error["location"]["line"].as_u64(),
            error["location"]["offset"].as_u64()
        ),
        (Some(1), Some(0))
    );
    assert_eq!(
        failed.document["stages"][1]["skip_reason"],
        "earlier-stage-failed"
    );
    assert_eq!(
        run(&["bundle", "verify", s(&dir.join("m.prism"))]).code,
        0,
        "a failed bundle is valid"
    );
    let no_copper = "(kicad_pcb (version 20260206) (generator \"pcbnew\") (general (thickness 1.6)) (layers (25 \"Edge.Cuts\" user)) (setup) (net 0 \"\"))\n";
    let board = source_tree(&[("x.kicad_pro", "{}"), ("x.kicad_pcb", no_copper)]);
    let archive = board.join("x.prism");
    assert_eq!(parse_into(&board.join("x.kicad_pro"), &archive).code, 0);
    let partial = run(&[
        "bundle",
        "build",
        "--stage",
        "board",
        "--in",
        s(&archive),
        "-o",
        s(&archive),
        "--refilled-board",
        s(&board.join("x.kicad_pcb")),
        "--refill-tool",
        "10.0.6",
    ]);
    assert_eq!(partial.code, 9);
    assert_eq!(partial.document["stages"][1]["status"], "partial");
    assert_eq!(
        partial.document["stages"][1]["failed_members"][0]["code"],
        "materialization-failed"
    );
    assert_eq!(
        partial.document["stages"][2]["skip_reason"],
        "earlier-stage-failed"
    );
    assert_eq!(run(&["bundle", "verify", s(&archive)]).code, 0);
}

#[test]
fn upgrade_records_are_kept_or_refused() {
    let tree = source_tree(&[("p.kicad_pro", "{}"), ("p.kicad_sch", SHEET)]);
    let out = tree.join("o.prism");
    let build = |upgraded: &str| {
        run(&[
            "bundle",
            "build",
            s(&tree.join("p.kicad_pro")),
            "--stage",
            "parse",
            "-o",
            s(&out),
            "--upgraded",
            upgraded,
            "--quiet",
        ])
    };
    assert_eq!(build("p.kicad_sch=20230121").code, 0);
    let limits = Limits::defaults(Work::Verify);
    let reader = BundleReader::open(std::fs::read(&out).unwrap(), &limits).unwrap();
    let sheet = reader
        .manifest()
        .members
        .iter()
        .find(|member| member.path.0 == "sources/p.kicad_sch")
        .unwrap();
    assert_eq!(
        sheet.source.as_ref().unwrap().upgraded_from_version,
        Some(20_230_121)
    );
    for refused in ["p.kicad_sch=20260306", "gone.kicad_sch=20230121"] {
        let failed = build(refused);
        assert_eq!(
            (failed.code, failed.code_field()),
            (3, "upgrade-record-invalid"),
            "{refused}"
        );
        assert_eq!(failed.document["argument"], "upgraded");
    }
}

#[test]
fn absent_projects_exit_ten() {
    let dir = scratch("exit-ten");
    let absent = run(&[
        "bundle",
        "build",
        s(&dir.join("Missing.kicad_pro")),
        "--stage",
        "parse",
        "-o",
        s(&dir.join("o.prism")),
    ]);
    assert_eq!(absent.code, 10);
    assert_eq!(absent.document["project_file"], "Missing.kicad_pro");
    assert_eq!(absent.document["command"], "bundle build");
    assert!(!dir.join("o.prism").exists());
}

fn precondition(args: &[String]) -> String {
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let failed = run(&args);
    assert_eq!(failed.code, 7, "{args:?}: {}", failed.document);
    failed.code_field().to_owned()
}

fn board_args(input: &Path, out: &Path, refilled: Option<&Path>, tool: &str) -> Vec<String> {
    let mut args: Vec<String> = [
        "bundle",
        "build",
        "--stage",
        "board",
        "--in",
        s(input),
        "-o",
        s(out),
    ]
    .map(str::to_owned)
    .to_vec();
    if let Some(refilled) = refilled {
        args.extend([
            "--refilled-board".to_owned(),
            s(refilled).to_owned(),
            "--refill-tool".to_owned(),
            tool.to_owned(),
        ]);
    }
    args
}

fn models_args(input: &Path, out: &Path, glb: &Path) -> Vec<String> {
    [
        "bundle",
        "build",
        "--stage",
        "models",
        "--in",
        s(input),
        "-o",
        s(out),
        "--models-glb",
        s(glb),
        "--models-tool",
        "10.0.6",
    ]
    .map(str::to_owned)
    .to_vec()
}

struct Seven {
    dir: PathBuf,
    archive: PathBuf,
    out: PathBuf,
    refilled: PathBuf,
    glb: PathBuf,
    recordings: PathBuf,
}

fn seven(label: &str) -> Seven {
    let dir = scratch(label);
    let recordings = scratch(&format!("{label}-recordings"));
    let archive = dir.join("a.prism");
    assert_eq!(parse_into(&usb_pd(), &archive).code, 0);
    let usb = "fixtures/release-studio/usb-pd";
    Seven {
        refilled: recording(usb, "USB-PD-Trigger-Board.kicad_pcb", &recordings).unwrap(),
        glb: recording(usb, "components.glb", &recordings).unwrap(),
        out: dir.join("o.prism"),
        archive,
        dir,
        recordings,
    }
}

#[test]
fn stage_preconditions_exit_seven() {
    let case = seven("exit-seven-stages");
    let (a, o) = (&case.archive, &case.out);
    assert_eq!(precondition(&models_args(a, o, &case.glb)), "stage-order");
    assert_eq!(
        precondition(&board_args(a, o, None, "")),
        "zone-fills-mismatch"
    );
    for other in ["10.1.0", "11.0.0"] {
        assert_eq!(
            precondition(&board_args(a, o, Some(&case.refilled), other)),
            "kicad-version-mismatch",
            "{other}"
        );
    }
    let other = recording(
        "fixtures/release-studio/synthetic",
        "synthetic.kicad_pcb",
        &case.recordings,
    )
    .unwrap();
    assert_eq!(
        precondition(&board_args(a, o, Some(&other), "10.0.6")),
        "refilled-board-mismatch"
    );
    let mut keyed = board_args(a, o, Some(&case.refilled), "10.0.6");
    keyed.extend(["--revision-key".to_owned(), "a".repeat(64)]);
    assert_eq!(precondition(&keyed), "revision-key-mismatch");
    let sch_only =
        repo().join("backend/tests/fixtures/design_variants/sch_only/sch_only.kicad_pro");
    let schematic = case.dir.join("s.prism");
    assert_eq!(parse_into(&sch_only, &schematic).code, 0);
    assert_eq!(
        precondition(&board_args(&schematic, o, Some(&case.refilled), "10.0.6")),
        "stage-not-applicable"
    );
    assert!(!o.exists(), "-o is untouched on every failure");
}

#[test]
fn a_kicad_patch_release_serves_the_pinned_generator() {
    let case = seven("patch-release");
    let a = &case.archive;
    let mut board = board_args(a, a, Some(&case.refilled), "10.0.7");
    board.push("--quiet".to_owned());
    let built = run(&board.iter().map(String::as_str).collect::<Vec<_>>());
    assert_eq!(built.code, 0, "{}", built.document);
    let mut models = models_args(a, a, &case.glb);
    let tool = models.iter().position(|arg| arg == "10.0.6").unwrap();
    models[tool] = "10.0.7".to_owned();
    assert_eq!(
        run(&models.iter().map(String::as_str).collect::<Vec<_>>()).code,
        0
    );
    let refused = models_args(a, &case.out, &case.glb)
        .into_iter()
        .map(|arg| {
            if arg == "10.0.6" {
                "11.0.0".to_owned()
            } else {
                arg
            }
        })
        .collect::<Vec<_>>();
    assert_eq!(precondition(&refused), "kicad-version-mismatch");
}

#[test]
fn bundle_preconditions_exit_seven() {
    let case = seven("exit-seven-bundles");
    let (a, o) = (&case.archive, &case.out);
    let foreign = case.dir.join("foreign.prism");
    rewrite(a, &foreign, |manifest, _| {
        manifest["generator"]["crate_version"] = "9.9.9".into()
    });
    assert_eq!(
        precondition(&board_args(&foreign, o, Some(&case.refilled), "10.0.6")),
        "generator-mismatch"
    );
    let a1 = case.dir.join("a1.prism");
    rewrite(a, &a1, |manifest, _| {
        *manifest = serde_json::json!({"schema": "prism.bundle.manifest", "version": "a1"});
    });
    assert_eq!(
        precondition(&board_args(&a1, o, Some(&case.refilled), "10.0.6")),
        "manifest-unsupported"
    );
    let extract = |member: &str, out: &Path| {
        ["bundle", "extract", s(a), member, "-o", s(out)]
            .map(str::to_owned)
            .to_vec()
    };
    assert_eq!(
        precondition(&extract("nope.json", &case.dir)),
        "member-not-found"
    );
    let out = scratch("exit-seven-extract");
    std::fs::write(out.join("bundle.json"), b"other bytes").unwrap();
    assert_eq!(
        precondition(&extract("bundle.json", &out)),
        "archive-path-collision"
    );
    let mut renamed = std::fs::read(&case.glb).unwrap();
    let at = renamed
        .windows(14)
        .position(|window| window == b"components.glb")
        .unwrap();
    renamed[at] = b'C';
    let renamed_path = case.dir.join("components.glb");
    std::fs::write(&renamed_path, renamed).unwrap();
    let mut board = board_args(a, a, Some(&case.refilled), "10.0.6");
    board.push("--quiet".to_owned());
    assert_eq!(
        run(&board.iter().map(String::as_str).collect::<Vec<_>>()).code,
        0
    );
    assert_eq!(
        precondition(&models_args(a, o, &renamed_path)),
        "models-glb-name"
    );
    assert!(!o.exists(), "-o is untouched on every failure");
}

#[test]
fn output_errors_exit_six_and_invalid_bundles_exit_eight() {
    let dir = scratch("exit-six");
    let missing_dir = run(&[
        "bundle",
        "build",
        s(&usb_pd()),
        "--stage",
        "parse",
        "-o",
        s(&dir.join("no/such/dir/o.prism")),
    ]);
    assert_eq!(
        (missing_dir.code, missing_dir.code_field()),
        (6, "output-write-failed")
    );
    let archive = dir.join("a.prism");
    assert_eq!(parse_into(&usb_pd(), &archive).code, 0);
    let not_dir = run(&[
        "bundle",
        "extract",
        s(&archive),
        "bundle.json",
        "-o",
        s(&archive),
    ]);
    assert_eq!(
        (not_dir.code, not_dir.code_field()),
        (6, "output-not-directory")
    );
    let tampered = dir.join("t.prism");
    rewrite(&archive, &tampered, |_, members| {
        let board = members
            .iter_mut()
            .find(|(path, _)| path.ends_with(".kicad_pcb"))
            .unwrap();
        board.1.push(b'\n');
    });
    let invalid = run(&["bundle", "verify", s(&tampered)]);
    assert_eq!(invalid.code, 8);
    assert_eq!(invalid.document["status"], "invalid");
    assert_eq!(
        invalid.document["findings"][0]["code"],
        "member-digest-mismatch"
    );
    let inconsistent = dir.join("i.prism");
    rewrite(&archive, &inconsistent, |manifest, _| {
        manifest["stages"]["board"]["status"] = "complete".into()
    });
    let report = run(&["bundle", "verify", s(&inconsistent)]);
    assert_eq!(report.code, 8);
    let codes: Vec<&str> = report.document["findings"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|f| f["code"].as_str())
        .collect();
    assert!(codes.contains(&"stage-inconsistent"), "{codes:?}");
}

fn limit_hit(out: &Path, name: &str, value: &str, args: &[&str]) -> Run {
    let setting = format!("{name}={value}");
    let mut all: Vec<&str> = args.to_vec();
    all.extend(["--limit", &setting]);
    let failed = run(&all);
    assert_eq!(failed.code, 4, "{name}: {}", failed.document);
    assert_eq!(failed.document["limit"]["name"], name);
    assert!(!out.exists(), "{name}: -o is untouched");
    failed
}

#[test]
fn source_limits_exit_four() {
    let dir = scratch("exit-four");
    let out = dir.join("o.prism");
    let limit = |name: &str, value: &str, args: &[&str]| limit_hit(&out, name, value, args);
    let project = usb_pd();
    let parse = [
        "bundle",
        "build",
        s(&project),
        "--stage",
        "parse",
        "-o",
        s(&out),
    ];
    for (name, value) in [
        ("max-source-files", "3"),
        ("max-source-file-bytes", "400000"),
        ("max-total-source-bytes", "900000"),
        ("max-path-bytes", "20"),
        ("max-sexpr-depth", "3"),
        ("max-sexpr-nodes", "1000"),
        ("max-sexpr-string-bytes", "4"),
        ("max-json-depth", "2"),
        ("max-json-nodes", "10"),
        ("max-member-bytes", "100000"),
        ("max-archive-bytes", "100000"),
        ("max-archive-members", "5"),
    ] {
        limit(name, value, &parse);
    }
    let memory = limit("max-memory-bytes", "3000000", &parse);
    assert_eq!(memory.document["limit"]["kind"], "bytes");
    let config = dir.join("c.json");
    std::fs::write(&config, r#"{"schema":"prism.revision_key.effective_config.a1","project_file":"USB-PD-Trigger-Board.kicad_pro","excluded_dirs":[]}"#).unwrap();
    let mut with_config: Vec<&str> = parse.to_vec();
    with_config.extend(["--config", s(&config)]);
    limit("max-config-bytes", "10", &with_config);
}

#[test]
fn reader_limits_and_timeouts() {
    let dir = scratch("exit-five");
    let out = dir.join("o.prism");
    let limit = |name: &str, value: &str, args: &[&str]| limit_hit(&out, name, value, args);
    let archive = dir.join("a.prism");
    assert_eq!(parse_into(&usb_pd(), &archive).code, 0);
    let extract_dir = scratch("exit-four-extract");
    limit(
        "max-total-unpacked-bytes",
        "1000",
        &[
            "bundle",
            "extract",
            s(&archive),
            "bundle.json",
            "-o",
            s(&extract_dir),
        ],
    );
    let finding = run(&[
        "bundle",
        "verify",
        s(&archive),
        "--limit",
        "max-total-unpacked-bytes=1000",
    ]);
    assert_eq!(
        (
            finding.code,
            finding.document["findings"][0]["code"].as_str()
        ),
        (8, Some("limit-exceeded"))
    );
    let truncated = run(&[
        "bundle",
        "verify",
        s(&archive),
        "--limit",
        "max-diagnostic-lines=0",
    ]);
    assert_eq!(truncated.code, 0);
    let timeout = run(&[
        "bundle",
        "build",
        s(&usb_pd()),
        "--stage",
        "parse",
        "-o",
        s(&out),
        "--limit",
        "timeout-ms=0",
    ]);
    assert_eq!(timeout.code, 5, "{}", timeout.document);
    assert_eq!(timeout.document["limit"]["kind"], "time");
    assert!(!out.exists());
    let leftovers = std::fs::read_dir(&dir)
        .unwrap()
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().to_string_lossy().ends_with(".tmp"))
        .count();
    assert_eq!(leftovers, 0, "no temp file is left behind");
}

#[test]
#[cfg(unix)]
fn a_signal_leaves_no_document_and_no_temp_file() {
    let recordings = scratch("signal-recordings");
    let cynthion = fixtures(&recordings)
        .into_iter()
        .find(|fixture| fixture.dir == "fixtures/release-studio/cynthion")
        .unwrap();
    let dir = scratch("signal");
    let out = dir.join("o.prism");
    let child = std::process::Command::new(support::binary())
        .args([
            "bundle",
            "build",
            s(&cynthion.project),
            "--stage",
            "parse",
            "-o",
            s(&out),
            "--quiet",
        ])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    // A freshly linked debug binary is code-signature checked on first
    // execution, which can take over a second on macOS; wait long enough for
    // the signal handlers to be installed before sending SIGTERM.
    std::thread::sleep(std::time::Duration::from_millis(3000));
    let pid = i32::try_from(child.id()).unwrap();
    std::process::Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status()
        .unwrap();
    let output = child.wait_with_output().unwrap();
    if output.status.code() == Some(0) {
        return; // the build finished before the signal arrived
    }
    assert_eq!(output.status.code(), Some(128 + 15));
    assert!(output.stdout.is_empty());
    assert!(!out.exists());
    assert_eq!(
        std::fs::read_dir(&dir).unwrap().count(),
        0,
        "the temp file is removed"
    );
    let _ = run_in;
}

/// `artwork` checks `--view` before any work (exit 2) and requires stage
/// models (exit 7), per cli-spec.md §5.7.
#[test]
fn artwork_refuses_an_unknown_view_and_a_bundle_without_models() {
    let dir = scratch("artwork-exits");
    let archive = dir.join("parse.prism");
    assert_eq!(parse_into(&usb_pd(), &archive).code, 0);
    let config = repo().join("native/src/rs/prism-artwork/configs/assembly.json");
    let out = dir.join("out");
    let unknown = run(&[
        "artwork",
        "--in",
        s(&archive),
        "--config",
        s(&config),
        "--view",
        "no_such_view",
        "-o",
        s(&out),
    ]);
    assert_eq!(unknown.code, 2);
    assert_eq!(unknown.document["code"], "view-unknown");
    let pending = run(&[
        "artwork",
        "--in",
        s(&archive),
        "--config",
        s(&config),
        "--view",
        "assembly_top_view",
        "-o",
        s(&out),
    ]);
    assert_eq!(pending.code, 7);
    assert_eq!(pending.document["code"], "stage-order");
}

/// KiCad loads a sub-sheet whose file is absent as an empty sheet, and reads one whose
/// reference differs only in letter case; stage parse does both, with a warning.
#[test]
fn absent_and_miscased_sub_sheets_warn() {
    let missing_sheet = SHEET.replace(
        "(lib_symbols))",
        "(lib_symbols) (sheet (at 0 0) (size 1 1) (uuid \"6c5f0000-0000-4000-8000-000000000002\") (property \"Sheetname\" \"S\")\n (property \"Sheetfile\" \"gone.kicad_sch\")))",
    );
    // KiCad loads a sheet whose file is absent as an empty sheet; so does stage parse,
    // with a warning instead of failing the design.
    let tree = source_tree(&[("p.kicad_pro", "{}"), ("p.kicad_sch", &missing_sheet)]);
    let gone = parse_into(&tree.join("p.kicad_pro"), &tree.join("o.prism"));
    assert_eq!(gone.code, 0);
    assert_eq!(gone.document["stages"][0]["status"], "complete");
    assert_eq!(gone.document["warnings"][0]["code"], "sheet-missing");
    // A reference that differs from the file only in letter case reads that file, as
    // the Windows and macOS file systems the designs are authored on do.
    let cased_sheet = SHEET.replace(
        "(lib_symbols))",
        "(lib_symbols) (sheet (at 0 0) (size 1 1) (uuid \"6c5f0000-0000-4000-8000-000000000003\") (property \"Sheetname\" \"S\")\n (property \"Sheetfile\" \"subsheets/Child.kicad_sch\")))",
    );
    let tree = source_tree(&[
        ("p.kicad_pro", "{}"),
        ("p.kicad_sch", &cased_sheet),
        ("Subsheets/child.kicad_sch", SHEET),
    ]);
    let out = tree.join("o.prism");
    let cased = parse_into(&tree.join("p.kicad_pro"), &out);
    assert_eq!(cased.code, 0);
    assert_eq!(cased.document["warnings"][0]["code"], "sheet-path-case");
    let limits = Limits::defaults(Work::Verify);
    let reader = BundleReader::open(std::fs::read(&out).unwrap(), &limits).unwrap();
    assert!(
        reader
            .manifest()
            .members
            .iter()
            .any(|member| member.path.0 == "sources/Subsheets/child.kicad_sch"),
        "the case-insensitive match is read"
    );
}

/// KiCad 10 opens each sheet a project lists in `schematic.top_level_sheets`: the
/// bundle reads every one in the source set and warns for a listed one that is not.
#[test]
fn every_top_level_sheet_is_read() {
    let project = r#"{"schematic": {"top_level_sheets": [
        {"filename": "p.kicad_sch", "name": "p", "uuid": "00000000-0000-0000-0000-000000000001"},
        {"filename": "second.kicad_sch", "name": "second", "uuid": "00000000-0000-0000-0000-000000000002"},
        {"filename": "gone.kicad_sch", "name": "gone", "uuid": "00000000-0000-0000-0000-000000000003"}]}}"#;
    let tree = source_tree(&[
        ("p.kicad_pro", project),
        ("p.kicad_sch", SHEET),
        ("second.kicad_sch", SHEET),
    ]);
    let out = tree.join("o.prism");
    let built = parse_into(&tree.join("p.kicad_pro"), &out);
    assert_eq!(built.code, 0, "{}", built.document);
    assert_eq!(built.document["stages"][0]["status"], "complete");
    let warnings = built.document["warnings"].as_array().expect("warnings");
    assert_eq!(warnings.len(), 1);
    assert_eq!(warnings[0]["code"], "top-level-sheet-missing");
    let limits = Limits::defaults(Work::Verify);
    let reader = BundleReader::open(std::fs::read(&out).unwrap(), &limits).unwrap();
    assert!(
        reader
            .manifest()
            .members
            .iter()
            .any(|member| member.path.0 == "sources/second.kicad_sch"),
        "the second top-level sheet is read"
    );
}

/// KiCad turns `\` into `/` in a sheet file name (`SCH_SHEET::SetFileName`); every
/// reader of the hierarchy, the projections included, resolves it the same way.
#[test]
fn a_backslash_sheet_path_resolves() {
    let root = SHEET.replace(
        "(lib_symbols))",
        "(lib_symbols) (sheet (at 0 0) (size 1 1) (uuid \"6c5f0000-0000-4000-8000-000000000004\") (property \"Sheetname\" \"S\")\n (property \"Sheetfile\" \"sub\\\\child.kicad_sch\")))",
    );
    let tree = source_tree(&[
        ("p.kicad_pro", "{}"),
        ("p.kicad_sch", &root),
        ("sub/child.kicad_sch", SHEET),
    ]);
    let built = parse_into(&tree.join("p.kicad_pro"), &tree.join("o.prism"));
    assert_eq!(built.code, 0, "{}", built.document);
    assert_eq!(built.document["stages"][0]["status"], "complete");
    assert!(
        built.document["warnings"]
            .as_array()
            .is_none_or(Vec::is_empty)
    );
}
