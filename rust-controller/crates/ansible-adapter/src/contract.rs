use api_compat::NormalizedPlan;
use controller_domain::OperationKind;
use sha2::{Digest, Sha256};
use std::{
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
};
use thiserror::Error;

pub(crate) const PLAYBOOK_BYTES: &[u8] =
    include_bytes!("../../../../autopilot-proxmox/playbooks/_test_long_sleep.yml");
#[cfg(target_os = "macos")]
const EXECUTABLE: &str = "/opt/homebrew/bin/ansible-playbook";
#[cfg(not(target_os = "macos"))]
const EXECUTABLE: &str = "/usr/bin/ansible-playbook";

#[derive(Clone, Copy, Debug)]
pub struct AdapterContract {
    pub operation_kind: OperationKind,
    pub contract_version: u16,
    pub adapter_identity: &'static str,
    pub playbook_relative_path: &'static str,
    pub allowed_extra_vars: &'static [&'static str],
    pub timeout_seconds: u64,
}

pub const SYNTHETIC_LONG_SLEEP_V1: AdapterContract = AdapterContract {
    operation_kind: OperationKind::SyntheticLongSleep,
    contract_version: 1,
    adapter_identity: "ansible:/app/playbooks/_test_long_sleep.yml@1",
    playbook_relative_path: "autopilot-proxmox/playbooks/_test_long_sleep.yml",
    allowed_extra_vars: &["duration"],
    timeout_seconds: 30,
};

#[derive(Debug, Error)]
pub enum AdapterError {
    #[error("adapter contract rejected")]
    Contract,
    #[error("adapter trusted path or content rejected")]
    TrustedPath,
    #[error("adapter local I/O failed")]
    Io,
}

impl From<std::io::Error> for AdapterError {
    fn from(_: std::io::Error) -> Self {
        Self::Io
    }
}

/// The registry has exactly one entry. Callers cannot add commands, paths,
/// environment variables, extra vars, or alternative contract implementations.
#[derive(Clone)]
pub struct AdapterRegistry {
    root: PathBuf,
    executable: PathBuf,
    interpreter: PathBuf,
    executable_digest: [u8; 32],
}

/// Constructible only through the registry. In particular, serialized legacy
/// argv cannot be used as a process capability.
/// ```compile_fail
/// use ansible_adapter::ValidatedInvocation;
/// let _ = ValidatedInvocation { executable: "/bin/sh".into() };
/// ```
pub struct ValidatedInvocation {
    pub(crate) registry: AdapterRegistry,
    pub(crate) duration: i64,
    pub(crate) fingerprint: String,
}

impl AdapterRegistry {
    pub fn local(worktree: &Path) -> Result<Self, AdapterError> {
        let root = fs::canonicalize(worktree)?;
        verify_playbook(&root)?;
        let executable = trusted_executable(Path::new(EXECUTABLE))?;
        let bytes = fs::read(&executable)?;
        // The installed Ansible entrypoint must use a concrete trusted Python,
        // not /usr/bin/env (whose resolution could depend on caller state).
        let shebang = bytes
            .split(|byte| *byte == b'\n')
            .next()
            .ok_or(AdapterError::TrustedPath)?;
        let interpreter = std::str::from_utf8(shebang)
            .ok()
            .and_then(|line| line.strip_prefix("#!"))
            .filter(|path| path.starts_with('/') && !path.contains(char::is_whitespace))
            .ok_or(AdapterError::TrustedPath)?;
        if !Path::new(interpreter)
            .file_name()
            .is_some_and(|name| name.to_string_lossy().starts_with("python3"))
        {
            return Err(AdapterError::TrustedPath);
        }
        let interpreter = trusted_executable(Path::new(interpreter))?;
        Ok(Self {
            root,
            executable,
            interpreter,
            executable_digest: Sha256::digest(bytes).into(),
        })
    }

    pub fn validate(&self, plan: &NormalizedPlan) -> Result<ValidatedInvocation, AdapterError> {
        let contract = SYNTHETIC_LONG_SLEEP_V1;
        let duration = plan
            .parameter_i64("duration")
            .ok_or(AdapterError::Contract)?;
        if plan.operation_kind() != contract.operation_kind
            || plan.contract_version() != contract.contract_version
            || plan.adapter_identity() != contract.adapter_identity
            || plan.parameters().len() != 1
            || !(0..=20).contains(&duration)
            || plan.required_capabilities().len() != 1
            || !plan.required_capabilities().contains("ansible_local")
        {
            return Err(AdapterError::Contract);
        }
        self.verify()?;
        Ok(ValidatedInvocation {
            registry: self.clone(),
            duration,
            fingerprint: plan
                .fingerprint()
                .map_err(|_| AdapterError::Contract)?
                .as_hex()
                .to_owned(),
        })
    }

    pub(crate) fn verify(&self) -> Result<(), AdapterError> {
        verify_playbook(&self.root)?;
        if trusted_executable(Path::new(EXECUTABLE))? != self.executable
            || trusted_executable(&self.interpreter)? != self.interpreter
            || <[u8; 32]>::from(Sha256::digest(fs::read(&self.executable)?))
                != self.executable_digest
        {
            return Err(AdapterError::TrustedPath);
        }
        Ok(())
    }

    pub(crate) fn executable(&self) -> &Path {
        &self.executable
    }
    pub(crate) fn interpreter(&self) -> &Path {
        &self.interpreter
    }
}

fn verify_playbook(root: &Path) -> Result<(), AdapterError> {
    let expected = root.join(SYNTHETIC_LONG_SLEEP_V1.playbook_relative_path);
    if fs::canonicalize(&expected)? != expected || fs::read(expected)? != PLAYBOOK_BYTES {
        return Err(AdapterError::TrustedPath);
    }
    Ok(())
}

fn trusted_executable(path: &Path) -> Result<PathBuf, AdapterError> {
    let canonical = fs::canonicalize(path)?;
    let metadata = fs::metadata(&canonical)?;
    if !metadata.is_file() || metadata.mode() & 0o111 == 0 {
        return Err(AdapterError::TrustedPath);
    }
    // Code and its containing directories may be owned by root or this process
    // owner. Homebrew's admin-owned directories intentionally allow group write;
    // trust that installed administrator boundary, never world-writable paths.
    let uid = unsafe { libc::geteuid() };
    for ancestor in canonical.ancestors() {
        let metadata = fs::metadata(ancestor)?;
        let trusted_admin_directory =
            cfg!(target_os = "macos") && metadata.is_dir() && metadata.gid() == 80;
        if ![0, uid].contains(&metadata.uid())
            || metadata.mode() & 0o002 != 0
            || (metadata.mode() & 0o020 != 0 && !trusted_admin_directory)
        {
            return Err(AdapterError::TrustedPath);
        }
    }
    Ok(canonical)
}
