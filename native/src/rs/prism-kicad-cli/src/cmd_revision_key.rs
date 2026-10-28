//! `prism-kicad revision-key` (cli-spec.md §5.5, revision-key.md §3, §9,
//! §11): list the commit's tree or the working tree, select and hash it with
//! the pure layer in `prism_bundle::revision_key`, print the record.

use crate::args::Args;
use crate::git::{Git, git_failed, lfs_on_path, records};
use crate::inputs;
use prism_bundle::revision_key::{
    self, EffectiveConfig, Entry, KeyError, KeySource, ListingRecord, ObjectFormat, RecordWarning,
    Selection, default_config,
};
use prism_bundle::{Failure, LimitName, Limits, Outcome};
use serde_json::{Value, json};
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsStr;
use std::path::Path;

/// What the command printed.
pub enum KeyOutcome {
    /// The record (exit 0).
    Record(Value),
    /// The project is absent at this revision (exit 10).
    Absent(Value),
}

fn key_failure(error: KeyError) -> Failure {
    let failure = Failure::input(error.code, error.message);
    match error.code {
        "config-invalid" => failure.argument("config"),
        _ => failure,
    }
}

fn parse_listing(output: &[u8]) -> Outcome<Vec<ListingRecord>> {
    records(output)
        .map(|record| {
            let tab = record
                .iter()
                .position(|byte| *byte == b'\t')
                .ok_or_else(|| git_failed("git ls-tree printed a record without a tab"))?;
            let header = std::str::from_utf8(&record[..tab])
                .map_err(|_| git_failed("git ls-tree printed a bad header"))?;
            let fields: Vec<&str> = header.split(' ').collect();
            let [mode, kind, object_id] = fields.as_slice() else {
                return Err(git_failed("git ls-tree printed a bad header"));
            };
            Ok(ListingRecord {
                mode: (*mode).to_owned(),
                kind: (*kind).to_owned(),
                object_id: (*object_id).to_owned(),
                path: record[tab + 1..].to_vec(),
            })
        })
        .collect()
}

fn object_format(git: &Git) -> Outcome<ObjectFormat> {
    let output = git.must(&["rev-parse", "--show-object-format"], None)?;
    ObjectFormat::parse(String::from_utf8_lossy(&output).trim())
        .ok_or_else(|| git_failed("git printed an unknown object format"))
}

fn commit_listing(git: &Git, revision: &str) -> Outcome<(String, Vec<ListingRecord>)> {
    let spec = format!("{revision}^{{commit}}");
    let commit = git
        .run(
            &[
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                &spec,
            ],
            None,
        )?
        .map_err(|_| {
            Failure::input(
                "commit-not-found",
                "the revision does not resolve to a commit",
            )
            .argument("commit")
        })?;
    let commit = String::from_utf8_lossy(&commit).trim().to_owned();
    let listing = git.must(&["ls-tree", "-r", "-z", "--full-tree", &commit], None)?;
    Ok((commit, parse_listing(&listing)?))
}

struct Candidate {
    path: Vec<u8>,
    index: Option<(String, String)>,
    embedded: bool,
}

fn index_entries(git: &Git) -> Outcome<BTreeMap<Vec<u8>, (String, String)>> {
    let flags = git.must(&["ls-files", "-z", "-v", "--cached"], None)?;
    for record in records(&flags) {
        let tag = record.first().copied().unwrap_or(b'H');
        if tag == b'S' || tag.is_ascii_lowercase() {
            return Err(Failure::input(
                "worktree-unsupported-index",
                "the index has skip-worktree or assume-unchanged entries",
            )
            .argument("worktree"));
        }
    }
    let stage = git.must(&["ls-files", "-z", "--stage"], None)?;
    let mut entries: BTreeMap<Vec<u8>, (String, String, u8)> = BTreeMap::new();
    for record in records(&stage) {
        let Some(tab) = record.iter().position(|byte| *byte == b'\t') else {
            continue;
        };
        let header = String::from_utf8_lossy(&record[..tab]).into_owned();
        let fields: Vec<&str> = header.split(' ').collect();
        let [mode, oid, number] = fields.as_slice() else {
            continue;
        };
        let number: u8 = number.parse().unwrap_or(0);
        let path = record[tab + 1..].to_vec();
        let keep = entries
            .get(&path)
            .is_none_or(|(_, _, existing)| number == 0 || *existing != 0);
        if keep {
            entries.insert(path, ((*mode).to_owned(), (*oid).to_owned(), number));
        }
    }
    Ok(entries
        .into_iter()
        .map(|(path, (mode, oid, _))| (path, (mode, oid)))
        .collect())
}

fn candidates(git: &Git) -> Outcome<Vec<Candidate>> {
    let index = index_entries(git)?;
    let others = git.must(&["ls-files", "-z", "--others", "--exclude-standard"], None)?;
    let mut all: BTreeMap<Vec<u8>, Candidate> = index
        .into_iter()
        .map(|(path, entry)| {
            let candidate = Candidate {
                path: path.clone(),
                index: Some(entry),
                embedded: false,
            };
            (path, candidate)
        })
        .collect();
    for record in records(&others) {
        let embedded = record.ends_with(b"/");
        let path = record.strip_suffix(b"/").unwrap_or(record).to_vec();
        all.entry(path.clone()).or_insert(Candidate {
            path,
            index: None,
            embedded,
        });
    }
    Ok(all.into_values().collect())
}

struct Worktree<'a> {
    git: &'a Git,
    format: ObjectFormat,
    file_mode: bool,
}

impl Worktree<'_> {
    fn gitlink(&self, candidate: &Candidate, host: &Path) -> Outcome<Option<ListingRecord>> {
        if host.join(".git").exists() {
            let head = self
                .git
                .run_in(
                    host,
                    &[
                        OsStr::new("rev-parse"),
                        OsStr::new("--verify"),
                        OsStr::new("HEAD"),
                    ],
                    None,
                )?
                .map_err(|_| git_failed("a submodule has no resolvable HEAD"))?;
            return Ok(Some(record(
                "160000",
                "commit",
                String::from_utf8_lossy(&head).trim(),
                &candidate.path,
            )));
        }
        Ok(candidate
            .index
            .as_ref()
            .filter(|(mode, _)| mode == "160000")
            .map(|(_, oid)| record("160000", "commit", oid, &candidate.path)))
    }

    fn entry(
        &self,
        candidate: &Candidate,
        pending: &mut Vec<Vec<u8>>,
    ) -> Outcome<Option<ListingRecord>> {
        let host = self.git.repo().join(os_from_bytes(&candidate.path));
        let Ok(metadata) = std::fs::symlink_metadata(&host) else {
            return Ok(None);
        };
        let gitlink = candidate.embedded
            || candidate
                .index
                .as_ref()
                .is_some_and(|(mode, _)| mode == "160000");
        if gitlink {
            return self.gitlink(candidate, &host);
        }
        let kind = metadata.file_type();
        if kind.is_symlink() {
            let target = std::fs::read_link(&host)
                .map_err(|error| git_failed(&format!("a link could not be read: {error}")))?;
            let id = self.format.blob_id(&os_to_bytes(target.as_os_str()));
            return Ok(Some(record("120000", "blob", &id, &candidate.path)));
        }
        if !kind.is_file() {
            return Ok(None);
        }
        let tracked_mode = candidate
            .index
            .as_ref()
            .map(|(mode, _)| mode.as_str())
            .filter(|mode| matches!(*mode, "100644" | "100755"));
        let mode = match (self.file_mode, tracked_mode) {
            (false, Some(mode)) => mode.to_owned(),
            _ if is_executable(&metadata) => "100755".to_owned(),
            _ => "100644".to_owned(),
        };
        pending.push(candidate.path.clone());
        Ok(Some(record(&mode, "blob", "", &candidate.path)))
    }

    /// Hash `paths` with the clean filters of their attributes.
    fn hash(&self, paths: &[Vec<u8>]) -> Outcome<BTreeMap<Vec<u8>, String>> {
        let mut ids = BTreeMap::new();
        let (batch, single): (Vec<&Vec<u8>>, Vec<&Vec<u8>>) =
            paths.iter().partition(|path| !path.contains(&b'\n'));
        if !batch.is_empty() {
            let mut stdin = Vec::new();
            for path in &batch {
                stdin.extend_from_slice(path);
                stdin.push(b'\n');
            }
            let output = self
                .git
                .must(&["hash-object", "--stdin-paths"], Some(&stdin))?;
            let lines: Vec<&[u8]> = output
                .split(|byte| *byte == b'\n')
                .filter(|line| !line.is_empty())
                .collect();
            if lines.len() != batch.len() {
                return Err(git_failed(
                    "git hash-object printed the wrong number of ids",
                ));
            }
            for (path, line) in batch.into_iter().zip(lines) {
                ids.insert(path.clone(), String::from_utf8_lossy(line).into_owned());
            }
        }
        for path in single {
            let name = os_from_bytes(path);
            let args = [
                OsStr::new("hash-object"),
                OsStr::new("--path"),
                name.as_ref(),
                OsStr::new("--"),
                name.as_ref(),
            ];
            let output = self
                .git
                .run_in(self.git.repo(), &args, None)?
                .map_err(|_| git_failed("git hash-object failed"))?;
            ids.insert(
                path.clone(),
                String::from_utf8_lossy(&output).trim().to_owned(),
            );
        }
        Ok(ids)
    }
}

/// A git path as a host path. Git prints UTF-8 on Windows.
#[cfg(unix)]
fn os_from_bytes(bytes: &[u8]) -> Cow<'_, OsStr> {
    Cow::Borrowed(std::os::unix::ffi::OsStrExt::from_bytes(bytes))
}

#[cfg(windows)]
fn os_from_bytes(bytes: &[u8]) -> Cow<'_, OsStr> {
    Cow::Owned(String::from_utf8_lossy(bytes).into_owned().into())
}

/// A symlink target as git stores it: raw bytes, with `/` on Windows.
#[cfg(unix)]
fn os_to_bytes(name: &OsStr) -> Cow<'_, [u8]> {
    Cow::Borrowed(std::os::unix::ffi::OsStrExt::as_bytes(name))
}

#[cfg(windows)]
fn os_to_bytes(name: &OsStr) -> Cow<'_, [u8]> {
    Cow::Owned(name.to_string_lossy().replace('\\', "/").into_bytes())
}

/// The owner-execute bit. Windows has none: the index mode decides there.
#[cfg(unix)]
fn is_executable(metadata: &std::fs::Metadata) -> bool {
    std::os::unix::fs::PermissionsExt::mode(&metadata.permissions()) & 0o100 != 0
}

#[cfg(windows)]
fn is_executable(_metadata: &std::fs::Metadata) -> bool {
    false
}

fn record(mode: &str, kind: &str, object_id: &str, path: &[u8]) -> ListingRecord {
    ListingRecord {
        mode: mode.to_owned(),
        kind: kind.to_owned(),
        object_id: object_id.to_owned(),
        path: path.to_vec(),
    }
}

fn selected_by_name(record: &ListingRecord, excluded: &[String]) -> bool {
    let path = record.path.as_slice();
    if excluded.iter().any(|dir| {
        path == dir.as_bytes()
            || (path.starts_with(dir.as_bytes()) && path.get(dir.len()) == Some(&b'/'))
    }) {
        return false;
    }
    let name = path.rsplit(|byte| *byte == b'/').next().unwrap_or(path);
    revision_key::category_for_name(name, record.mode == "120000").is_some()
}

fn worktree_listing(
    git: &mut Git,
    format: ObjectFormat,
    excluded: &[String],
) -> Outcome<(Vec<ListingRecord>, Vec<RecordWarning>)> {
    let lfs = lfs_on_path();
    if lfs {
        git.configure("filter.lfs.clean=git-lfs clean -- %f".to_owned());
    }
    let file_mode = git
        .run(&["config", "--bool", "core.fileMode"], None)?
        .map_or(true, |output| {
            String::from_utf8_lossy(&output).trim() != "false"
        });
    let worktree = Worktree {
        git,
        format,
        file_mode,
    };
    let mut pending = Vec::new();
    let mut listing = Vec::new();
    for candidate in candidates(git)? {
        if let Some(entry) = worktree.entry(&candidate, &mut pending)? {
            listing.push(entry);
        }
    }
    let selected: BTreeSet<Vec<u8>> = listing
        .iter()
        .filter(|entry| {
            entry.mode != "160000" && entry.mode != "120000" && selected_by_name(entry, excluded)
        })
        .map(|entry| entry.path.clone())
        .collect();
    pending.retain(|path| selected.contains(path));
    let ids = worktree.hash(&pending)?;
    // Unselected regular files are never hashed: selection depends only on
    // mode and path (revision-key.md §5.3, §9 step 4).
    listing.retain(|entry| !entry.object_id.is_empty() || ids.contains_key(&entry.path));
    for entry in &mut listing {
        if let Some(id) = ids.get(&entry.path) {
            entry.object_id.clone_from(id);
        }
    }
    let warnings = if lfs {
        Vec::new()
    } else {
        lfs_warning(git, &pending)?
    };
    Ok((listing, warnings))
}

fn lfs_warning(git: &Git, paths: &[Vec<u8>]) -> Outcome<Vec<(String, String)>> {
    if paths.is_empty() {
        return Ok(Vec::new());
    }
    let configured = git
        .run(&["config", "--get", "filter.lfs.clean"], None)?
        .is_ok_and(|output| !output.trim_ascii().is_empty());
    if configured {
        return Ok(Vec::new());
    }
    let mut stdin = Vec::new();
    for path in paths {
        stdin.extend_from_slice(path);
        stdin.push(0);
    }
    let output = git.must(&["check-attr", "-z", "--stdin", "filter"], Some(&stdin))?;
    let fields: Vec<&[u8]> = records(&output).collect();
    let lfs = fields
        .chunks(3)
        .any(|triple| triple.get(2) == Some(&b"lfs".as_slice()));
    Ok(if lfs {
        vec![(
            "lfs-filter-unavailable".to_owned(),
            "git-lfs is not on PATH, so LFS files were hashed as their content and the key differs from the commit's".to_owned(),
        )]
    } else {
        Vec::new()
    })
}

fn check_limits(entries: &[Entry], limits: &Limits) -> Outcome<()> {
    let count = u64::try_from(entries.len()).unwrap_or(u64::MAX);
    limits.check(
        LimitName::MaxSourceFiles,
        count,
        "the number of selected entries",
    )?;
    for entry in entries {
        limits.check(
            LimitName::MaxPathBytes,
            u64::try_from(entry.path.len()).unwrap_or(u64::MAX),
            &format!("the length of {:?}", entry.path),
        )?;
    }
    Ok(())
}

fn effective_config(
    args: &Args,
    project: &str,
    listing: &[ListingRecord],
    format: ObjectFormat,
) -> Outcome<EffectiveConfig> {
    if let Some(path) = args.option("config") {
        let config = inputs::config(Path::new(path), &args.limits)?;
        if config.project_file.0 != project {
            return Err(Failure::precondition(
                "config-mismatch",
                "--project differs from the config's project_file",
            )
            .argument("project"));
        }
        return Ok(config);
    }
    let provisional = default_config(project, |_| false);
    let blobs: BTreeSet<String> =
        match revision_key::select(listing, &provisional, format).map_err(key_failure)? {
            Selection::Selected(entries) => entries
                .into_iter()
                .filter(|entry| entry.kind != revision_key::Kind::Gitlink)
                .map(|entry| entry.path)
                .collect(),
            Selection::ProjectAbsent => BTreeSet::new(),
        };
    Ok(default_config(project, |path| blobs.contains(path)))
}

fn check_project(project: &str) -> Outcome<()> {
    let valid = prism_bundle::paths::has_path_form(project)
        && project.to_ascii_lowercase().ends_with(".kicad_pro");
    if valid {
        Ok(())
    } else {
        Err(Failure::input(
            "config-invalid",
            "--project is not a repository path ending in .kicad_pro",
        )
        .argument("project"))
    }
}

/// Run `revision-key`.
pub fn run(args: &Args) -> Outcome<KeyOutcome> {
    let project = args.option("project").unwrap_or_default().to_owned();
    check_project(&project)?;
    let repo = Path::new(&args.positional[0]);
    let mut git = Git::new(repo)?;
    let bare = git
        .run(&["rev-parse", "--is-bare-repository"], None)?
        .map_err(|_| {
            Failure::input("repo-not-found", "<repo> is not a git repository").argument("repo")
        })?;
    let bare = String::from_utf8_lossy(&bare).trim() == "true";
    let format = object_format(&git)?;
    let excluded: Vec<String> = match args.option("config") {
        Some(path) => inputs::config(Path::new(path), &args.limits)?
            .excluded_dirs
            .iter()
            .map(|dir| dir.0.clone())
            .collect(),
        None => Vec::new(),
    };
    let (source, listing, warnings) = match args.option("commit").filter(|_| !args.worktree) {
        Some(revision) => {
            let (commit, listing) = commit_listing(&git, revision)?;
            (KeySource::Commit(commit), listing, Vec::new())
        }
        None if bare => {
            return Err(Failure::input(
                "repo-not-found",
                "--worktree needs a repository with a working tree",
            )
            .argument("repo"));
        }
        None => {
            let (listing, warnings) = worktree_listing(&mut git, format, &excluded)?;
            (KeySource::Worktree, listing, warnings)
        }
    };
    let config = effective_config(args, &project, &listing, format)?;
    let entries = match revision_key::select(&listing, &config, format).map_err(key_failure)? {
        Selection::Selected(entries) => entries,
        Selection::ProjectAbsent => {
            let mut document = json!({
                "schema": "prism.cli.project_absent.a0",
                "code": "project-absent",
                "command": "revision-key",
                "project_file": project,
            });
            if let KeySource::Commit(commit) = &source {
                document["commit"] = commit.clone().into();
            }
            return Ok(KeyOutcome::Absent(document));
        }
    };
    check_limits(&entries, &args.limits)?;
    let record =
        revision_key::record(format, &source, &config, &entries, &warnings).map_err(key_failure)?;
    let value =
        serde_json::to_value(&record).map_err(|error| Failure::internal(error.to_string()))?;
    Ok(KeyOutcome::Record(value))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn listing_records_split_at_the_first_tab() {
        let records =
            parse_listing(b"100644 blob abc\ta\tb.kicad_pro\x00160000 commit def\tlib\x00")
                .unwrap();
        assert_eq!(records[0].path, b"a\tb.kicad_pro");
        assert_eq!(records[1].mode, "160000");
    }
}
