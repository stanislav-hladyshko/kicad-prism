//! `prism-kicad revision-key` against real repositories (revision-key.md §9,
//! §13 "WP-K and WP-B MUST add these working-tree cases"): a working tree's
//! key equals the key of the commit `git add -A && git commit` would make,
//! a clean tree has the key of HEAD, and CRLF checkouts, modes, links,
//! untracked, ignored and deleted files follow git.

mod support;

use std::path::{Path, PathBuf};
use std::process::Command;
use support::{run, s, scratch};

fn git(repo: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .args([
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .current_dir(repo)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("HOME", repo)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().to_owned()
}

fn write(repo: &Path, path: &str, bytes: &[u8]) {
    let path = repo.join(path);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, bytes).unwrap();
}

fn repository() -> PathBuf {
    let repo = scratch("rk-repo");
    git(&repo, &["init", "-q", "-b", "main"]);
    write(&repo, "p.kicad_pro", b"{}\n");
    write(&repo, "p.kicad_sch", b"(kicad_sch)\n");
    write(&repo, "p.kicad_pcb", b"(kicad_pcb)\n");
    write(&repo, "lib/part.step", b"ISO-10303-21;\n");
    write(&repo, "README.md", b"readme\n");
    git(&repo, &["add", "-A"]);
    git(&repo, &["commit", "-q", "-m", "first"]);
    repo
}

fn key(repo: &Path, mode: &[&str]) -> (String, serde_json::Value) {
    let mut args = vec!["revision-key", s(repo)];
    args.extend_from_slice(mode);
    args.extend(["--project", "p.kicad_pro"]);
    let result = run(&args);
    assert_eq!(result.code, 0, "{}", result.document);
    (
        result.document["revision_key"].as_str().unwrap().to_owned(),
        result.document,
    )
}

fn commit_key(repo: &Path) -> String {
    key(repo, &["--commit", "HEAD"]).0
}

fn worktree_key(repo: &Path) -> String {
    key(repo, &["--worktree"]).0
}

#[test]
fn a_clean_tree_has_the_key_of_head() {
    let repo = repository();
    let (head, record) = key(&repo, &["--commit", "HEAD"]);
    assert_eq!(worktree_key(&repo), head);
    assert_eq!(
        record["source"]["commit"],
        git(&repo, &["rev-parse", "HEAD"])
    );
    assert_eq!(record["counts"]["total"], 4, "README.md is not selected");
    assert_eq!(
        record["config"]["root_schematic"], "p.kicad_sch",
        "the default config pairs by stem"
    );
}

#[test]
fn crlf_on_disk_keeps_the_key_with_text_auto() {
    let repo = repository();
    write(&repo, ".gitattributes", b"* text=auto\n");
    git(&repo, &["add", "-A"]);
    git(&repo, &["commit", "-q", "-m", "attributes"]);
    let head = commit_key(&repo);
    write(&repo, "p.kicad_sch", b"(kicad_sch)\r\n");
    assert_eq!(
        worktree_key(&repo),
        head,
        "the clean filter normalizes CRLF"
    );
}

#[test]
fn a_dirty_tree_has_the_key_of_its_next_commit() {
    let repo = repository();
    write(&repo, ".gitignore", b"ignored.kicad_sch\n");
    write(&repo, "p.kicad_sch", b"(kicad_sch (changed))\n");
    write(&repo, "new.kicad_sym", b"(kicad_symbol_lib)\n");
    write(&repo, "ignored.kicad_sch", b"(kicad_sch)\n");
    std::fs::remove_file(repo.join("lib/part.step")).unwrap();
    #[cfg(unix)]
    {
        std::fs::set_permissions(
            repo.join("p.kicad_pcb"),
            std::os::unix::fs::PermissionsExt::from_mode(0o755),
        )
        .unwrap();
        std::os::unix::fs::symlink("lib", repo.join("models")).unwrap();
    }
    let nested = repo.join("vendor/kicad-libraries");
    std::fs::create_dir_all(&nested).unwrap();
    git(&nested, &["init", "-q"]);
    write(&nested, "a.kicad_mod", b"(footprint)\n");
    git(&nested, &["add", "-A"]);
    git(&nested, &["commit", "-q", "-m", "nested"]);
    let before = commit_key(&repo);
    let dirty = worktree_key(&repo);
    assert_ne!(dirty, before);
    git(&repo, &["add", "-A"]);
    git(&repo, &["commit", "-q", "-m", "everything"]);
    assert_eq!(
        commit_key(&repo),
        dirty,
        "the working-tree key is the next commit's"
    );
    let (_, record) = key(&repo, &["--commit", "HEAD"]);
    let kinds: Vec<(&str, &str)> = record["entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| {
            (
                entry["path"].as_str().unwrap(),
                entry["kind"].as_str().unwrap(),
            )
        })
        .collect();
    // Windows has no mode bits and, without privileges, no symlinks: the test makes neither there.
    #[cfg(unix)]
    {
        assert!(kinds.contains(&("p.kicad_pcb", "executable")));
        assert!(kinds.contains(&("models", "symlink")));
    }
    assert!(kinds.contains(&("vendor/kicad-libraries", "gitlink")));
    assert!(!kinds.iter().any(|(path, _)| *path == "ignored.kicad_sch"));
}

#[test]
fn outcomes_and_errors_follow_the_cli_spec() {
    let repo = repository();
    let absent = run(&[
        "revision-key",
        s(&repo),
        "--commit",
        "HEAD",
        "--project",
        "gone.kicad_pro",
    ]);
    assert_eq!(absent.code, 10);
    assert_eq!(absent.document["code"], "project-absent");
    assert_eq!(
        absent.document["commit"],
        git(&repo, &["rev-parse", "HEAD"])
    );
    let no_commit = run(&[
        "revision-key",
        s(&repo),
        "--commit",
        "nope",
        "--project",
        "p.kicad_pro",
    ]);
    assert_eq!(
        (no_commit.code, no_commit.code_field()),
        (3, "commit-not-found")
    );
    let not_repo = scratch("rk-not-repo");
    let missing = run(&[
        "revision-key",
        s(&not_repo),
        "--worktree",
        "--project",
        "p.kicad_pro",
    ]);
    assert_eq!((missing.code, missing.code_field()), (3, "repo-not-found"));
    let config = repo.join("config.json");
    std::fs::write(&config, r#"{"schema":"prism.revision_key.effective_config.a1","project_file":"other.kicad_pro","excluded_dirs":[]}"#).unwrap();
    let mismatch = run(&[
        "revision-key",
        s(&repo),
        "--commit",
        "HEAD",
        "--project",
        "p.kicad_pro",
        "--config",
        s(&config),
    ]);
    assert_eq!(
        (mismatch.code, mismatch.code_field()),
        (7, "config-mismatch")
    );
    std::fs::write(&config, r#"{"schema":"prism.revision_key.effective_config.a1","project_file":"p.kicad_pro","board":"nope.kicad_pcb","excluded_dirs":[]}"#).unwrap();
    let path_missing = run(&[
        "revision-key",
        s(&repo),
        "--commit",
        "HEAD",
        "--project",
        "p.kicad_pro",
        "--config",
        s(&config),
    ]);
    assert_eq!(
        (path_missing.code, path_missing.code_field()),
        (3, "config-path-missing")
    );
}

#[test]
fn a_bundle_built_with_the_key_record_carries_its_entries() {
    let repo = repository();
    let (revision_key, record) = key(&repo, &["--commit", "HEAD"]);
    let record_path = repo.join("record.json");
    std::fs::write(&record_path, serde_json::to_vec(&record).unwrap()).unwrap();
    let out = scratch("rk-bundle").join("b.prism");
    let built = run(&[
        "bundle",
        "build",
        s(&repo.join("p.kicad_pro")),
        "--stage",
        "parse",
        "-o",
        s(&out),
        "--revision-key",
        &revision_key,
        "--key-record",
        s(&record_path),
        "--quiet",
    ]);
    assert_eq!(built.code, 0, "{}", built.document);
    let limits = prism_bundle::Limits::defaults(prism_bundle::Work::Verify);
    let reader = prism_bundle::BundleReader::open(std::fs::read(&out).unwrap(), &limits).unwrap();
    let manifest = reader.manifest();
    assert_eq!(manifest.revision_key.as_ref().unwrap().0, revision_key);
    let entries: Vec<(String, String)> = record["entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| {
            (
                entry["path"].as_str().unwrap().to_owned(),
                entry["object_id"].as_str().unwrap().to_owned(),
            )
        })
        .collect();
    for member in manifest
        .members
        .iter()
        .filter(|member| member.path.0.starts_with("sources/"))
    {
        let source = member.source.as_ref().unwrap();
        assert!(
            entries.contains(&(source.path.0.clone(), source.object_id.0.clone())),
            "{}",
            member.path.0
        );
    }
    let wrong = run(&[
        "bundle",
        "build",
        s(&repo.join("p.kicad_pro")),
        "--stage",
        "parse",
        "-o",
        s(&out),
        "--revision-key",
        &"0".repeat(64),
        "--key-record",
        s(&record_path),
    ]);
    assert_eq!(
        (wrong.code, wrong.code_field()),
        (7, "revision-key-mismatch")
    );
}

/// The key of `repository()`, computed on Linux and macOS. The key covers the
/// selected files' bytes and paths, not commit metadata, so a Windows checkout
/// that holds the committed bytes (Prism clones with `core.autocrlf=false`)
/// must give the same key from the commit and from the working tree.
const REPOSITORY_KEY: &str = "a5c06e0458d9eff21b9990d12f6905d20c108bf74bd2b77c9d97848edaa6867e";

#[test]
fn the_key_is_the_same_on_every_os() {
    let repo = repository();
    assert_eq!(commit_key(&repo), REPOSITORY_KEY, "commit");
    assert_eq!(worktree_key(&repo), REPOSITORY_KEY, "working tree");
}
