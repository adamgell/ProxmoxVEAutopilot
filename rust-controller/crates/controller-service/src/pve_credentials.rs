use crate::config::ValidatedObservationConfig;
use pve_port::PveApiToken;
use std::{
    fs::{Metadata, OpenOptions},
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
};

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct TokenFile {
    token_id: String,
    secret: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct CredentialFailure;

impl std::fmt::Display for CredentialFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("observation credentials rejected")
    }
}
impl std::error::Error for CredentialFailure {}

pub(crate) fn load_token(
    config: &ValidatedObservationConfig,
) -> Result<PveApiToken, CredentialFailure> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(config.token_file())
        .map_err(|_| CredentialFailure)?;
    let metadata = file.metadata().map_err(|_| CredentialFailure)?;
    // SAFETY: geteuid takes no arguments and returns the current effective UID.
    validate_metadata(&metadata, unsafe { libc::geteuid() })?;
    read_token(file)
}

fn validate_metadata(metadata: &Metadata, effective_uid: u32) -> Result<(), CredentialFailure> {
    if !metadata.is_file()
        || metadata.uid() != effective_uid
        || metadata.mode() & 0o077 != 0
        || metadata.len() > 8192
    {
        return Err(CredentialFailure);
    }
    Ok(())
}

fn read_token(reader: impl Read) -> Result<PveApiToken, CredentialFailure> {
    // A second bound closes growth after the descriptor metadata check.
    let mut bytes = Vec::new();
    reader
        .take(8193)
        .read_to_end(&mut bytes)
        .map_err(|_| CredentialFailure)?;
    if bytes.len() > 8192 {
        return Err(CredentialFailure);
    }
    let wire: TokenFile = serde_json::from_slice(&bytes).map_err(|_| CredentialFailure)?;
    PveApiToken::parse(&wire.token_id, &wire.secret).map_err(|_| CredentialFailure)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::observation_test_config;
    use std::{
        fs,
        io::{Read, Write},
        os::unix::fs::{MetadataExt, PermissionsExt, symlink},
        path::Path,
        process::{Child, Command, Stdio},
        time::{Duration, Instant},
    };

    const CANARY: &str = "synthetic-secret-canary-task2";
    const VALID: &str =
        r#"{"token_id":"observer@pve!local-proof","secret":"synthetic-secret-canary-task2"}"#;

    fn write_file(dir: &Path, body: &[u8], mode: u32) -> std::path::PathBuf {
        let path = dir.join("synthetic-token.json");
        let mut file = fs::File::create(&path).unwrap();
        file.set_permissions(fs::Permissions::from_mode(mode))
            .unwrap();
        file.write_all(body).unwrap();
        path
    }

    fn assert_rejected(path: &Path) {
        let result = load_token(&observation_test_config(path));
        let error = result.expect_err("unsafe credential input accepted");
        assert_eq!(error, CredentialFailure);
        let rendered = format!("{error} {error:?}");
        assert!(!rendered.contains(CANARY));
        assert!(!rendered.contains(path.to_str().unwrap()));
        assert!(std::error::Error::source(&error).is_none());
    }

    #[test]
    fn credential_valid_0600_redacts_token_and_accepts_exact_size_limit() {
        let dir = tempfile::tempdir().unwrap();
        let path = write_file(dir.path(), VALID.as_bytes(), 0o600);
        let token = load_token(&observation_test_config(&path)).expect("valid protected token");
        assert_eq!(format!("{token:?}"), "PveApiToken(<redacted>)");
        assert!(!format!("{token:?}").contains(CANARY));
        let mut exact = VALID.as_bytes().to_vec();
        exact.resize(8192, b' ');
        write_file(dir.path(), &exact, 0o600);
        assert!(load_token(&observation_test_config(&path)).is_ok());
    }

    #[test]
    fn credential_rejects_oversize_and_strict_json_or_token_failures() {
        let dir = tempfile::tempdir().unwrap();
        let bodies = [
            format!("{VALID}{}", " ".repeat(8193 - VALID.len())),
            r#"{"token_id":"observer@pve!proof","secret":"synthetic-secret-canary-task2","extra":true}"#.into(),
            r#"{"token_id":"observer@pve!proof","secret":"synthetic-secret-canary-task2","secret":"second"}"#.into(),
            r#"{"token_id":"observer@pve!proof","token_id":"second@pve!proof","secret":"synthetic-secret-canary-task2"}"#.into(),
            "{synthetic-secret-canary-task2".into(),
            r#"{"secret":"synthetic-secret-canary-task2"}"#.into(),
            r#"{"token_id":"observer@pve!proof","secret":123}"#.into(),
            r#"{"token_id":"invalid","secret":"synthetic-secret-canary-task2"}"#.into(),
            r#"{"token_id":"observer@pve!proof","secret":"invalid\nsecret"}"#.into(),
            format!("{VALID}{VALID}"),
        ];
        for body in bodies {
            assert_rejected(&write_file(dir.path(), body.as_bytes(), 0o600));
        }
    }

    #[test]
    fn credential_rejects_missing_symlink_directory_and_exposed_permissions() {
        let dir = tempfile::tempdir().unwrap();
        assert_rejected(&dir.path().join("missing-synthetic-secret-canary-task2"));
        assert_rejected(dir.path());
        let path = write_file(dir.path(), VALID.as_bytes(), 0o600);
        let link = dir.path().join("symlink");
        symlink(&path, &link).unwrap();
        assert_rejected(&link);
        for mode in [0o640, 0o604, 0o620, 0o601] {
            assert_rejected(&write_file(dir.path(), VALID.as_bytes(), mode));
        }
    }

    #[test]
    fn credential_metadata_rejects_effective_owner_mismatch_without_chown() {
        let dir = tempfile::tempdir().unwrap();
        let path = write_file(dir.path(), VALID.as_bytes(), 0o600);
        let metadata = fs::metadata(path).unwrap();
        assert!(validate_metadata(&metadata, metadata.uid()).is_ok());
        assert_eq!(
            validate_metadata(&metadata, metadata.uid().wrapping_add(1)),
            Err(CredentialFailure)
        );
    }

    // Break: relying only on pre-read length admits a file grown after descriptor validation.
    #[test]
    fn credential_read_cap_rejects_growth_after_metadata_check() {
        let dir = tempfile::tempdir().unwrap();
        let path = write_file(dir.path(), VALID.as_bytes(), 0o600);
        let mut file = fs::File::open(&path).unwrap();
        let metadata = file.metadata().unwrap();
        validate_metadata(&metadata, metadata.uid()).unwrap();
        fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(&vec![b' '; 16384])
            .unwrap();
        assert!(read_token(&mut file).is_err());
        use std::io::Seek;
        assert_eq!(file.stream_position().unwrap(), 8193);
    }

    // Same bounded ownership guard as tests/service.rs: only our child can be killed/reaped.
    struct ServiceChild(Child);
    impl Drop for ServiceChild {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let deadline = Instant::now() + Duration::from_millis(200);
            while Instant::now() < deadline {
                if matches!(self.0.try_wait(), Ok(Some(_))) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(5));
            }
            panic!("owned credential child cleanup unconfirmed");
        }
    }

    #[test]
    fn credential_fifo_is_nonblocking_and_errors_do_not_leak_to_stderr() {
        const CHILD: &str = "CREDENTIAL_FIFO_TEST_CHILD";
        if let Some(path) = std::env::var_os(CHILD) {
            // Exercise the actual environment entry point only in this owned child.
            let controller = crate::config::ControllerConfig::from_env().unwrap();
            let config = crate::config::observation_config(&controller)
                .ok()
                .flatten()
                .expect("validated environment");
            assert_eq!(config.token_file(), Path::new(&path));
            let error = load_token(&config).expect_err("FIFO accepted");
            eprintln!("{error}");
            assert_eq!(error, CredentialFailure);
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let fifo = dir.path().join(CANARY);
        let c_path = std::ffi::CString::new(fifo.as_os_str().as_encoded_bytes()).unwrap();
        // SAFETY: valid NUL-terminated path in an owned temporary directory.
        assert_eq!(unsafe { libc::mkfifo(c_path.as_ptr(), 0o600) }, 0);
        let mut child = ServiceChild(Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "pve_credentials::tests::credential_fifo_is_nonblocking_and_errors_do_not_leak_to_stderr", "--nocapture"])
            .env_clear().env(CHILD, &fifo)
            .env("RUST_CONTROLLER_MODE", "observe")
            .env("RUST_CONTROLLER_DATABASE_URL", "postgresql://127.0.0.1/proof")
            .env("RUST_CONTROLLER_PVE_BASE_URL", "http://127.0.0.1:8006")
            .env("RUST_CONTROLLER_PVE_TRANSPORT", "http-observe")
            .env("RUST_CONTROLLER_PVE_TOKEN_FILE", &fifo)
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap());
        let deadline = Instant::now() + Duration::from_secs(2);
        let status = loop {
            if let Some(status) = child.0.try_wait().unwrap() {
                break status;
            }
            assert!(Instant::now() < deadline, "FIFO credential loading blocked");
            std::thread::sleep(Duration::from_millis(5));
        };
        assert!(status.success(), "credential child failed");
        let mut stderr = String::new();
        child
            .0
            .stderr
            .take()
            .unwrap()
            .take(8192)
            .read_to_string(&mut stderr)
            .unwrap();
        assert!(!stderr.contains(CANARY));
        assert!(!stderr.contains(fifo.to_str().unwrap()));
        assert_eq!(stderr, "observation credentials rejected\n");
    }
}
