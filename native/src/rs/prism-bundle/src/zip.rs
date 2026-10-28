//! The deterministic zip subset of a `.prism` archive (bundle-format.md §4,
//! cli-spec.md §6).
//!
//! **Writer.** Zip 2.0, every member deflated with `miniz_oxide` at level 6
//! or stored when deflating does not make it strictly smaller (empty members
//! are stored), DOS time 1980-01-01 00:00:00, flags `0x0800`, no extra
//! fields, comments, directory entries, data descriptors or zip64, external
//! attributes 0, and a canonical contiguous layout. It streams: one member is
//! compressed at a time and written straight to the output. This is a1's
//! writer (sim: `src/prism-bundle/src/zip.rs`), made streaming.
//!
//! **Reader.** Accepts exactly the writer's subset. [`ZipIndex::open`] checks
//! the container, the central directory, every local header and the layout,
//! the names, duplicates and collisions, and the declared sizes against the
//! limits, before anything is inflated (cli-spec.md §6.2). [`ZipIndex::read`]
//! inflates one member bounded by its declared size and checks its size and
//! CRC-32.

use crate::finding::{Failure, Finding, Location};
use crate::limits::{LimitName, Limits};
use crate::paths::{check_path, collision_key};
use std::collections::BTreeMap;
use std::io::{self, Write};

const LOCAL_SIGNATURE: u32 = 0x0403_4b50;
const CENTRAL_SIGNATURE: u32 = 0x0201_4b50;
const END_SIGNATURE: u32 = 0x0605_4b50;
const ZIP64_LOCATOR_SIGNATURE: u32 = 0x0706_4b50;
/// Size of a local file header without its name.
pub const LOCAL_HEADER_BYTES: u64 = 30;
const CENTRAL_HEADER_BYTES: usize = 46;
/// Size of the end-of-central-directory record without a comment.
pub const END_RECORD_BYTES: u64 = 22;
const VERSION: u16 = 20;
const METHOD_STORED: u16 = 0;
const METHOD_DEFLATED: u16 = 8;
const DEFLATE_LEVEL: u8 = 6;
const UTF8_NAMES: u16 = 0x0800;
const FIXED_TIME: u16 = 0;
const FIXED_DATE: u16 = (1 << 5) | 1;
const RATIO_THRESHOLD: u64 = 1 << 20;

/// CRC-32 (IEEE 802.3) of `bytes`.
#[must_use]
pub fn crc32(bytes: &[u8]) -> u32 {
    crc32fast::hash(bytes)
}

/// The bytes a member is written as, and its method.
fn encode(data: &[u8]) -> (u16, std::borrow::Cow<'_, [u8]>) {
    if !data.is_empty() {
        let deflated = miniz_oxide::deflate::compress_to_vec(data, DEFLATE_LEVEL);
        if deflated.len() < data.len() {
            return (METHOD_DEFLATED, std::borrow::Cow::Owned(deflated));
        }
    }
    (METHOD_STORED, std::borrow::Cow::Borrowed(data))
}

/// A member encoded for the archive: its method, written bytes and CRC-32.
/// Encoding is the expensive part of a write and needs no writer, so callers
/// can run it on other threads ([`encode_member`], [`ZipWriter::add_encoded`]).
pub struct Encoded<'a> {
    method: u16,
    body: std::borrow::Cow<'a, [u8]>,
    crc: u32,
}

/// Encode `data` as [`ZipWriter::add`] does.
#[must_use]
pub fn encode_member(data: &[u8]) -> Encoded<'_> {
    let (method, body) = encode(data);
    Encoded {
        method,
        body,
        crc: crc32(data),
    }
}

#[derive(Clone, Copy)]
struct Header {
    method: u16,
    crc: u32,
    compressed: u32,
    size: u32,
    name_length: u16,
}

fn common_fields(header: &Header) -> Vec<u8> {
    let mut out = Vec::with_capacity(26);
    for value in [VERSION, UTF8_NAMES, header.method, FIXED_TIME, FIXED_DATE] {
        out.extend_from_slice(&value.to_le_bytes());
    }
    for value in [header.crc, header.compressed, header.size] {
        out.extend_from_slice(&value.to_le_bytes());
    }
    out.extend_from_slice(&header.name_length.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out
}

/// The output chunk of a streaming inflate.
const STREAM_CHUNK: usize = 64 * 1024;

/// A streaming writer of the canonical subset.
pub struct ZipWriter<W: Write> {
    out: W,
    offset: u64,
    central: Vec<u8>,
    count: u64,
    max_archive_bytes: u64,
    max_members: u64,
}

fn too_large(limits: &Limits, name: LimitName, observed: Option<u64>, what: &str) -> Failure {
    limits.exceeded(name, observed, what)
}

impl<W: Write> ZipWriter<W> {
    /// Start an archive written to `out`, bounded by `limits`.
    pub fn new(out: W, limits: &Limits) -> Self {
        Self {
            out,
            offset: 0,
            central: Vec::new(),
            count: 0,
            max_archive_bytes: limits
                .get(LimitName::MaxArchiveBytes)
                .min(u64::from(u32::MAX)),
            max_members: limits.get(LimitName::MaxArchiveMembers).min(65_535),
        }
    }

    fn write_out(&mut self, bytes: &[u8], limits: &Limits) -> Result<(), WriteError> {
        let length = u64::try_from(bytes.len()).unwrap_or(u64::MAX);
        let end = self.offset.saturating_add(length);
        if end > self.max_archive_bytes {
            return Err(WriteError::Limit(Box::new(too_large(
                limits,
                LimitName::MaxArchiveBytes,
                None,
                "the archive",
            ))));
        }
        self.out.write_all(bytes).map_err(WriteError::Io)?;
        self.offset = end;
        Ok(())
    }

    /// Append member `name` with `data`. The name must already be a valid
    /// archive path.
    pub fn add(&mut self, name: &str, data: &[u8], limits: &Limits) -> Result<(), WriteError> {
        self.add_encoded(name, data, &encode_member(data), limits)
    }

    /// Add `data` as `name`, already encoded by [`encode_member`]. The archive
    /// bytes equal [`ZipWriter::add`]'s.
    pub fn add_encoded(
        &mut self,
        name: &str,
        data: &[u8],
        encoded: &Encoded<'_>,
        limits: &Limits,
    ) -> Result<(), WriteError> {
        self.add_raw(
            name,
            encoded.method,
            encoded.crc,
            &encoded.body,
            u64::try_from(data.len()).unwrap_or(u64::MAX),
            limits,
        )
    }

    /// Add a member already encoded in an input archive: its method, CRC-32,
    /// uncompressed size and compressed body. The bytes written equal
    /// [`ZipWriter::add`]'s when the input was written by this writer.
    pub fn add_copied(
        &mut self,
        name: &str,
        size: u64,
        crc: u32,
        deflated: bool,
        body: &[u8],
        limits: &Limits,
    ) -> Result<(), WriteError> {
        self.add_raw(
            name,
            if deflated {
                METHOD_DEFLATED
            } else {
                METHOD_STORED
            },
            crc,
            body,
            size,
            limits,
        )
    }

    fn add_raw(
        &mut self,
        name: &str,
        method: u16,
        crc: u32,
        body: &[u8],
        size: u64,
        limits: &Limits,
    ) -> Result<(), WriteError> {
        self.count += 1;
        if self.count > self.max_members {
            return Err(WriteError::Limit(Box::new(too_large(
                limits,
                LimitName::MaxArchiveMembers,
                None,
                "the number of archive members",
            ))));
        }
        let max_member = limits
            .get(LimitName::MaxMemberBytes)
            .min(u64::from(u32::MAX));
        if size > max_member {
            return Err(WriteError::Limit(Box::new(limits.exceeded(
                LimitName::MaxMemberBytes,
                Some(size),
                name,
            ))));
        }
        let header = Header {
            method,
            crc,
            compressed: u32::try_from(body.len()).map_err(|_| WriteError::internal("member"))?,
            size: u32::try_from(size).map_err(|_| WriteError::internal("member"))?,
            name_length: u16::try_from(name.len()).map_err(|_| WriteError::internal("name"))?,
        };
        let local_offset =
            u32::try_from(self.offset).map_err(|_| WriteError::internal("archive offset"))?;
        let fields = common_fields(&header);
        let mut local = Vec::with_capacity(30 + name.len());
        local.extend_from_slice(&LOCAL_SIGNATURE.to_le_bytes());
        local.extend_from_slice(&fields);
        local.extend_from_slice(name.as_bytes());
        self.write_out(&local, limits)?;
        self.write_out(body, limits)?;
        self.central
            .extend_from_slice(&CENTRAL_SIGNATURE.to_le_bytes());
        self.central.extend_from_slice(&VERSION.to_le_bytes());
        self.central.extend_from_slice(&fields);
        // Comment length, disk number, internal and external attributes.
        self.central.extend_from_slice(&[0; 10]);
        self.central.extend_from_slice(&local_offset.to_le_bytes());
        self.central.extend_from_slice(name.as_bytes());
        Ok(())
    }

    /// Write the central directory and the end record; return the output and
    /// the archive size.
    pub fn finish(mut self, limits: &Limits) -> Result<(W, u64), WriteError> {
        let count = u16::try_from(self.count).map_err(|_| WriteError::internal("count"))?;
        let central_offset =
            u32::try_from(self.offset).map_err(|_| WriteError::internal("archive offset"))?;
        let central_size =
            u32::try_from(self.central.len()).map_err(|_| WriteError::internal("directory"))?;
        let central = std::mem::take(&mut self.central);
        self.write_out(&central, limits)?;
        let mut end = Vec::with_capacity(22);
        end.extend_from_slice(&END_SIGNATURE.to_le_bytes());
        end.extend_from_slice(&[0; 4]);
        end.extend_from_slice(&count.to_le_bytes());
        end.extend_from_slice(&count.to_le_bytes());
        end.extend_from_slice(&central_size.to_le_bytes());
        end.extend_from_slice(&central_offset.to_le_bytes());
        end.extend_from_slice(&0u16.to_le_bytes());
        self.write_out(&end, limits)?;
        self.out.flush().map_err(WriteError::Io)?;
        Ok((self.out, self.offset))
    }
}

/// Why writing failed.
#[derive(Debug)]
pub enum WriteError {
    /// The output failed.
    Io(io::Error),
    /// A limit was exceeded.
    Limit(Box<Failure>),
}

impl WriteError {
    fn internal(what: &str) -> Self {
        Self::Limit(Box::new(Failure::internal(format!(
            "{what} does not fit zip32"
        ))))
    }
}

/// Random-access bytes: a file or a buffer.
pub trait ReadAt {
    /// Total length.
    fn length(&self) -> u64;
    /// Fill `buffer` from `offset`.
    fn read_exact_at(&self, buffer: &mut [u8], offset: u64) -> io::Result<()>;
}

impl ReadAt for [u8] {
    fn length(&self) -> u64 {
        u64::try_from(self.len()).unwrap_or(u64::MAX)
    }

    fn read_exact_at(&self, buffer: &mut [u8], offset: u64) -> io::Result<()> {
        let start = usize::try_from(offset).map_err(|_| io::ErrorKind::UnexpectedEof)?;
        let end = start
            .checked_add(buffer.len())
            .ok_or(io::ErrorKind::UnexpectedEof)?;
        let source = self.get(start..end).ok_or(io::ErrorKind::UnexpectedEof)?;
        buffer.copy_from_slice(source);
        Ok(())
    }
}

impl ReadAt for Vec<u8> {
    fn length(&self) -> u64 {
        self.as_slice().length()
    }

    fn read_exact_at(&self, buffer: &mut [u8], offset: u64) -> io::Result<()> {
        self.as_slice().read_exact_at(buffer, offset)
    }
}

/// A file opened for reading, with its length taken once.
pub struct FileSource {
    file: std::fs::File,
    length: u64,
}

impl FileSource {
    /// Wrap `file`.
    pub fn new(file: std::fs::File) -> io::Result<Self> {
        let length = file.metadata()?.len();
        Ok(Self { file, length })
    }
}

#[cfg(unix)]
fn read_exact_at(file: &std::fs::File, buffer: &mut [u8], offset: u64) -> io::Result<()> {
    std::os::unix::fs::FileExt::read_exact_at(file, buffer, offset)
}

/// `seek_read` may return fewer bytes than asked: loop until the buffer is full.
#[cfg(windows)]
fn read_exact_at(file: &std::fs::File, mut buffer: &mut [u8], mut offset: u64) -> io::Result<()> {
    while !buffer.is_empty() {
        match std::os::windows::fs::FileExt::seek_read(file, buffer, offset) {
            Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
            Ok(read) => {
                buffer = &mut buffer[read..];
                offset += read as u64;
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

impl ReadAt for FileSource {
    fn length(&self) -> u64 {
        self.length
    }

    fn read_exact_at(&self, buffer: &mut [u8], offset: u64) -> io::Result<()> {
        read_exact_at(&self.file, buffer, offset)
    }
}

/// One entry of a checked archive.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    /// Member path.
    pub name: String,
    /// Method: stored or deflated.
    deflated: bool,
    /// CRC-32 of the uncompressed bytes.
    pub crc: u32,
    /// Compressed size.
    pub compressed: u64,
    /// Declared uncompressed size.
    pub size: u64,
    /// Offset of the member's data.
    data_at: u64,
}

impl Entry {
    /// Whether the member's data is deflated (method 8) rather than stored.
    #[must_use]
    pub fn is_deflated(&self) -> bool {
        self.deflated
    }
}

/// An archive whose structure passed every check of cli-spec.md §6.2.
#[derive(Clone, Debug)]
pub struct ZipIndex {
    /// Entries in archive order.
    pub entries: Vec<Entry>,
}

fn finding(code: &str, message: impl Into<String>) -> ZipError {
    ZipError::Finding(Finding::new(code, message))
}

/// Why an archive or member was rejected.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ZipError {
    /// A verify finding (`zip-*`).
    Finding(Finding),
    /// A limit of cli-spec.md §7.2 (`limit-exceeded`).
    Limit(Failure),
    /// The file could not be read.
    Io(String),
}

impl ZipError {
    /// The finding this error reports in `bundle verify`.
    #[must_use]
    pub fn finding(&self) -> Finding {
        match self {
            Self::Finding(finding) => finding.clone(),
            Self::Limit(failure) => failure.finding.clone(),
            Self::Io(message) => Finding::new("zip-malformed", message.clone()),
        }
    }
}

fn read_bytes<R: ReadAt + ?Sized>(
    source: &R,
    offset: u64,
    length: usize,
) -> Result<Vec<u8>, ZipError> {
    let end = offset.saturating_add(u64::try_from(length).unwrap_or(u64::MAX));
    if end > source.length() {
        return Err(finding("zip-malformed", "the archive is truncated"));
    }
    let mut buffer = vec![0; length];
    source
        .read_exact_at(&mut buffer, offset)
        .map_err(|error| ZipError::Io(format!("the archive could not be read: {error}")))?;
    Ok(buffer)
}

fn u16_at(bytes: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([bytes[at], bytes[at + 1]])
}

fn u32_at(bytes: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]])
}

struct End {
    count: u16,
    central_size: u32,
    central_offset: u32,
}

/// `Err(finding(code, message))` unless every check holds.
fn ensure(checks: &[bool], code: &str, message: &str) -> Result<(), ZipError> {
    if checks.iter().all(|holds| *holds) {
        Ok(())
    } else {
        Err(finding(code, message))
    }
}

fn read_end<R: ReadAt + ?Sized>(source: &R, limits: &Limits) -> Result<End, ZipError> {
    let length = source.length();
    if length > limits.get(LimitName::MaxArchiveBytes) {
        return Err(ZipError::Limit(limits.exceeded(
            LimitName::MaxArchiveBytes,
            Some(length),
            "the archive",
        )));
    }
    ensure(
        &[length >= END_RECORD_BYTES],
        "zip-malformed",
        "the archive is shorter than an end record",
    )?;
    let end = read_bytes(source, length - END_RECORD_BYTES, 22)?;
    ensure(
        &[u32_at(&end, 0) == END_SIGNATURE],
        "zip-malformed",
        "the end-of-central-directory record is not the last 22 bytes (a comment or trailing data follows it)",
    )?;
    if length >= END_RECORD_BYTES + 20 {
        let locator = read_bytes(source, length - END_RECORD_BYTES - 20, 4)?;
        ensure(
            &[u32_at(&locator, 0) != ZIP64_LOCATOR_SIGNATURE],
            "zip-unsupported",
            "the archive has zip64 records",
        )?;
    }
    let record = decode_end(&end)?;
    if u64::from(record.count) > limits.get(LimitName::MaxArchiveMembers) {
        return Err(ZipError::Limit(limits.exceeded(
            LimitName::MaxArchiveMembers,
            Some(u64::from(record.count)),
            "the number of archive members",
        )));
    }
    let central_end = u64::from(record.central_offset) + u64::from(record.central_size);
    ensure(
        &[central_end == length - END_RECORD_BYTES],
        "zip-malformed",
        "the central directory does not end where the end record starts",
    )?;
    Ok(record)
}

fn decode_end(end: &[u8]) -> Result<End, ZipError> {
    let total = u16_at(end, 10);
    let record = End {
        count: total,
        central_size: u32_at(end, 12),
        central_offset: u32_at(end, 16),
    };
    ensure(
        &[
            total != u16::MAX,
            record.central_size != u32::MAX,
            record.central_offset != u32::MAX,
        ],
        "zip-unsupported",
        "the end record holds zip64 markers",
    )?;
    ensure(
        &[
            u16_at(end, 4) == 0,
            u16_at(end, 6) == 0,
            u16_at(end, 8) == total,
            u16_at(end, 20) == 0,
        ],
        "zip-malformed",
        "the end record has a disk number, a comment or two different entry counts",
    )?;
    Ok(record)
}

struct Central {
    method: u16,
    crc: u32,
    compressed: u32,
    size: u32,
    local_offset: u32,
    name: Vec<u8>,
}

fn central_entry(directory: &[u8], at: usize) -> Result<(Central, usize), ZipError> {
    let header = directory
        .get(at..at + CENTRAL_HEADER_BYTES)
        .ok_or_else(|| finding("zip-malformed", "a central directory entry is truncated"))?;
    ensure(
        &[u32_at(header, 0) == CENTRAL_SIGNATURE],
        "zip-malformed",
        "a central directory entry has a bad signature",
    )?;
    let name_length = usize::from(u16_at(header, 28));
    let (compressed, size) = (u32_at(header, 20), u32_at(header, 24));
    ensure(
        &[compressed != u32::MAX, size != u32::MAX],
        "zip-unsupported",
        "a member uses zip64 sizes",
    )?;
    ensure(
        &[
            u16_at(header, 4) == VERSION,
            u16_at(header, 6) == VERSION,
            u16_at(header, 8) == UTF8_NAMES,
            matches!(u16_at(header, 10), METHOD_STORED | METHOD_DEFLATED),
            u16_at(header, 12) == FIXED_TIME,
            u16_at(header, 14) == FIXED_DATE,
            u16_at(header, 30) == 0,
            u16_at(header, 32) == 0,
            u16_at(header, 34) == 0,
            u16_at(header, 36) == 0,
            u32_at(header, 38) == 0,
        ],
        "zip-unsupported",
        "a central directory entry is not version 20, flags 0x0800, stored or deflated, \
         1980-01-01, without extra field, comment or attributes",
    )?;
    let name_at = at + CENTRAL_HEADER_BYTES;
    let name = directory
        .get(name_at..name_at + name_length)
        .ok_or_else(|| finding("zip-malformed", "a member name is truncated"))?
        .to_vec();
    let entry = Central {
        method: u16_at(header, 10),
        crc: u32_at(header, 16),
        compressed,
        size,
        local_offset: u32_at(header, 42),
        name,
    };
    Ok((entry, name_at + name_length))
}

fn check_name(raw: &[u8], limits: &Limits) -> Result<String, ZipError> {
    let name = std::str::from_utf8(raw)
        .map_err(|_| finding("zip-path-invalid", "a member name is not UTF-8"))?;
    check_path(name, limits.get_usize(LimitName::MaxPathBytes)).map_err(|problem| {
        ZipError::Finding(
            Finding::new(
                "zip-path-invalid",
                format!("member name {name:?} {}", problem.describe()),
            )
            .at(Location {
                path: crate::paths::has_path_form(name).then(|| name.to_owned()),
                ..Location::default()
            }),
        )
    })?;
    Ok(name.to_owned())
}

fn check_local<R: ReadAt + ?Sized>(
    source: &R,
    central: &Central,
    expected_at: u64,
) -> Result<u64, ZipError> {
    ensure(
        &[u64::from(central.local_offset) == expected_at],
        "zip-malformed",
        "the entries are not contiguous: a local header is not where the previous entry ends",
    )?;
    let header = read_bytes(source, expected_at, 30)?;
    let name = read_bytes(source, expected_at + LOCAL_HEADER_BYTES, central.name.len())?;
    ensure(
        &[
            u32_at(&header, 0) == LOCAL_SIGNATURE,
            u16_at(&header, 4) == VERSION,
            u16_at(&header, 6) == UTF8_NAMES,
            u16_at(&header, 8) == central.method,
            u16_at(&header, 10) == FIXED_TIME,
            u16_at(&header, 12) == FIXED_DATE,
            u32_at(&header, 14) == central.crc,
            u32_at(&header, 18) == central.compressed,
            u32_at(&header, 22) == central.size,
            usize::from(u16_at(&header, 26)) == central.name.len(),
            u16_at(&header, 28) == 0,
            name == central.name,
        ],
        "zip-malformed",
        "a local header does not repeat its central directory record",
    )?;
    Ok(expected_at + LOCAL_HEADER_BYTES + u64::try_from(name.len()).unwrap_or(0))
}

fn check_sizes(entries: &[Entry], limits: &Limits) -> Result<(), ZipError> {
    let mut total = 0u64;
    for entry in entries {
        if entry.size > limits.get(LimitName::MaxMemberBytes) {
            return Err(ZipError::Limit(
                limits
                    .exceeded(LimitName::MaxMemberBytes, Some(entry.size), &entry.name)
                    .at_path(entry.name.clone()),
            ));
        }
        if !entry.deflated && entry.compressed != entry.size {
            return Err(ZipError::Finding(
                Finding::new("zip-malformed", "a stored member's sizes differ")
                    .at_path(entry.name.clone()),
            ));
        }
        total = total.saturating_add(entry.size);
        if entry.size > RATIO_THRESHOLD {
            let ratio = entry.size.checked_div(entry.compressed).unwrap_or(u64::MAX);
            if ratio > limits.get(LimitName::MaxCompressionRatio) {
                return Err(ZipError::Finding(
                    Finding::new(
                        "zip-ratio-exceeded",
                        format!(
                            "{} inflates {ratio} times, over max-compression-ratio {}",
                            entry.name,
                            limits.get(LimitName::MaxCompressionRatio)
                        ),
                    )
                    .at_path(entry.name.clone()),
                ));
            }
        }
    }
    if total > limits.get(LimitName::MaxTotalUnpackedBytes) {
        return Err(ZipError::Limit(limits.exceeded(
            LimitName::MaxTotalUnpackedBytes,
            Some(total),
            "the sum of the declared member sizes",
        )));
    }
    Ok(())
}

fn check_unique(entries: &[Entry]) -> Result<(), ZipError> {
    let mut exact = BTreeMap::new();
    let mut folded = BTreeMap::new();
    for entry in entries {
        if exact.insert(entry.name.as_str(), ()).is_some() {
            return Err(ZipError::Finding(
                Finding::new(
                    "zip-duplicate-member",
                    format!("{} appears twice", entry.name),
                )
                .at_path(entry.name.clone()),
            ));
        }
        if let Some(previous) = folded.insert(collision_key(&entry.name), entry.name.as_str()) {
            return Err(ZipError::Finding(
                Finding::new(
                    "zip-path-collision",
                    format!(
                        "{previous} and {} are equal after NFC normalization and case folding",
                        entry.name
                    ),
                )
                .at_path(entry.name.clone()),
            ));
        }
    }
    Ok(())
}

impl ZipIndex {
    /// Check the archive's structure and list its entries.
    pub fn open<R: ReadAt + ?Sized>(source: &R, limits: &Limits) -> Result<Self, ZipError> {
        let end = read_end(source, limits)?;
        let directory = read_bytes(
            source,
            u64::from(end.central_offset),
            usize::try_from(end.central_size).unwrap_or(usize::MAX),
        )?;
        let mut at = 0usize;
        let mut expected_local = 0u64;
        let mut entries = Vec::with_capacity(usize::from(end.count));
        for _ in 0..end.count {
            let (central, next) = central_entry(&directory, at)?;
            at = next;
            let name = check_name(&central.name, limits)?;
            let data_at = check_local(source, &central, expected_local)?;
            expected_local = data_at + u64::from(central.compressed);
            entries.push(Entry {
                name,
                deflated: central.method == METHOD_DEFLATED,
                crc: central.crc,
                compressed: u64::from(central.compressed),
                size: u64::from(central.size),
                data_at,
            });
        }
        if at != directory.len() {
            return Err(finding(
                "zip-malformed",
                "the central directory holds more than its declared entries",
            ));
        }
        if expected_local != u64::from(end.central_offset) {
            return Err(finding(
                "zip-malformed",
                "the central directory does not follow the last entry's data directly",
            ));
        }
        check_unique(&entries)?;
        check_sizes(&entries, limits)?;
        Ok(Self { entries })
    }

    /// The entry named `name`.
    #[must_use]
    pub fn entry(&self, name: &str) -> Option<&Entry> {
        self.entries.iter().find(|entry| entry.name == name)
    }

    /// The compressed body of `entry` as stored in the archive, without
    /// inflating it.
    pub fn body<R: ReadAt + ?Sized>(&self, source: &R, entry: &Entry) -> Result<Vec<u8>, ZipError> {
        read_bytes(
            source,
            entry.data_at,
            usize::try_from(entry.compressed).unwrap_or(usize::MAX),
        )
    }

    /// Inflate `entry`, bounded by its declared size, and check its size and
    /// CRC-32.
    pub fn read<R: ReadAt + ?Sized>(&self, source: &R, entry: &Entry) -> Result<Vec<u8>, ZipError> {
        let at = |code: &str, message: String| {
            ZipError::Finding(Finding::new(code, message).at_path(entry.name.clone()))
        };
        let body = self.body(source, entry)?;
        let size = usize::try_from(entry.size).unwrap_or(usize::MAX);
        let data = if entry.deflated {
            miniz_oxide::inflate::decompress_to_vec_with_limit(&body, size).map_err(|_| {
                at(
                    "zip-size-mismatch",
                    format!(
                        "{} does not inflate to its declared {size} bytes",
                        entry.name
                    ),
                )
            })?
        } else {
            body
        };
        if data.len() != size {
            return Err(at(
                "zip-size-mismatch",
                format!(
                    "{} inflates to {} bytes, not its declared {size}",
                    entry.name,
                    data.len()
                ),
            ));
        }
        if crc32(&data) != entry.crc {
            return Err(at(
                "zip-crc-mismatch",
                format!("{} does not match its CRC-32", entry.name),
            ));
        }
        Ok(data)
    }
}

fn size_mismatch(entry: &Entry, message: String) -> ZipError {
    ZipError::Finding(Finding::new("zip-size-mismatch", message).at_path(entry.name.clone()))
}

/// Check an already-read compressed `body` of `entry` against its declared
/// size and CRC-32, passing the inflated bytes to `sink` in chunks instead of
/// retaining them. The bytes are identical to [`ZipIndex::read`]'s.
pub fn check_body(entry: &Entry, body: &[u8], sink: &mut dyn FnMut(&[u8])) -> Result<(), ZipError> {
    let mut crc = crc32fast::Hasher::new();
    if entry.deflated {
        inflate_stream(entry, body, sink, &mut crc)?;
    } else {
        let size = usize::try_from(entry.size).unwrap_or(usize::MAX);
        if body.len() != size {
            return Err(size_mismatch(
                entry,
                format!(
                    "{} is {} bytes, not its declared {size}",
                    entry.name,
                    body.len()
                ),
            ));
        }
        for chunk in body.chunks(STREAM_CHUNK) {
            crc.update(chunk);
            sink(chunk);
        }
    }
    if crc.finalize() != entry.crc {
        return Err(ZipError::Finding(
            Finding::new(
                "zip-crc-mismatch",
                format!("{} does not match its CRC-32", entry.name),
            )
            .at_path(entry.name.clone()),
        ));
    }
    Ok(())
}

/// Inflate a raw DEFLATE body in [`STREAM_CHUNK`] pieces, checking the
/// declared size and passing each piece to `sink`.
fn inflate_stream(
    entry: &Entry,
    body: &[u8],
    sink: &mut dyn FnMut(&[u8]),
    crc: &mut crc32fast::Hasher,
) -> Result<(), ZipError> {
    use miniz_oxide::inflate::stream::{InflateState, inflate};
    use miniz_oxide::{DataFormat, MZFlush, MZStatus};
    let size = usize::try_from(entry.size).unwrap_or(usize::MAX);
    let short = |entry: &Entry| {
        size_mismatch(
            entry,
            format!(
                "{} does not inflate to its declared {size} bytes",
                entry.name
            ),
        )
    };
    let mut state = InflateState::new_boxed(DataFormat::Raw);
    let mut buffer = vec![0u8; STREAM_CHUNK];
    let mut input = body;
    let mut produced = 0usize;
    loop {
        let result = inflate(&mut state, input, &mut buffer, MZFlush::None);
        let status = result.status.map_err(|_| short(entry))?;
        input = &input[result.bytes_consumed..];
        let chunk = &buffer[..result.bytes_written];
        produced = produced.saturating_add(chunk.len());
        if produced > size {
            return Err(size_mismatch(
                entry,
                format!(
                    "{} inflates to more than its declared {size} bytes",
                    entry.name
                ),
            ));
        }
        if !chunk.is_empty() {
            crc.update(chunk);
            sink(chunk);
        }
        match status {
            MZStatus::StreamEnd => break,
            MZStatus::Ok => {
                if result.bytes_consumed == 0 && result.bytes_written == 0 {
                    return Err(short(entry));
                }
            }
            MZStatus::NeedDict => {
                return Err(size_mismatch(
                    entry,
                    format!(
                        "{} needs a preset dictionary that a raw DEFLATE member cannot have",
                        entry.name
                    ),
                ));
            }
        }
    }
    if produced != size {
        return Err(size_mismatch(
            entry,
            format!(
                "{} inflates to {produced} bytes, not its declared {size}",
                entry.name
            ),
        ));
    }
    Ok(())
}
