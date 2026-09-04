use std::collections::{BTreeMap, BTreeSet};

use controller_domain::OperationKind;
use event_journal::{CanonicalizationError, payload_digest};
use serde::Serialize;
use serde_json::Value;
use thiserror::Error;

use crate::{JobEnvelope, JobValidationError};

const JOB_TYPE: &str = "test_long_sleep";
const EXECUTABLE: &str = "ansible-playbook";
const PLAYBOOK: &str = "/app/playbooks/_test_long_sleep.yml";
const ARGUMENT_KEY: &str = "duration";
const CONTRACT_VERSION: u16 = 1;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(untagged)]
enum SanitizedScalar {
    Integer(i64),
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(transparent)]
pub struct SanitizedValue(SanitizedScalar);

impl SanitizedValue {
    fn from_duration(value: &Value) -> Result<Self, NormalizationError> {
        let raw = value
            .as_str()
            .ok_or(NormalizationError::UnsafeParameterValue)?;
        let parsed = raw
            .parse::<i64>()
            .map_err(|_| NormalizationError::UnsafeParameterValue)?;
        if raw != parsed.to_string() {
            return Err(NormalizationError::UnsafeParameterValue);
        }
        Ok(Self(SanitizedScalar::Integer(parsed)))
    }

    #[must_use]
    pub const fn as_i64(&self) -> Option<i64> {
        match &self.0 {
            SanitizedScalar::Integer(value) => Some(*value),
        }
    }
}

/// A plan can only be constructed through [`normalize_job`]. Its fields remain
/// private so callers cannot bypass the compatibility allowlist.
///
/// ```compile_fail
/// use api_compat::NormalizedPlan;
///
/// let _ = NormalizedPlan {
///     job_id: String::new(),
/// };
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct NormalizedPlan {
    job_id: String,
    operation_kind: OperationKind,
    contract_version: u16,
    adapter_identity: String,
    parameters: BTreeMap<String, SanitizedValue>,
    required_capabilities: BTreeSet<String>,
    expected_events: Vec<String>,
    postconditions: Vec<String>,
}

impl NormalizedPlan {
    #[must_use]
    pub fn job_id(&self) -> &str {
        &self.job_id
    }

    #[must_use]
    pub const fn operation_kind(&self) -> OperationKind {
        self.operation_kind
    }

    #[must_use]
    pub const fn contract_version(&self) -> u16 {
        self.contract_version
    }

    #[must_use]
    pub fn adapter_identity(&self) -> &str {
        &self.adapter_identity
    }

    #[must_use]
    pub fn parameter_i64(&self, key: &str) -> Option<i64> {
        self.parameters.get(key).and_then(SanitizedValue::as_i64)
    }

    #[must_use]
    pub const fn parameters(&self) -> &BTreeMap<String, SanitizedValue> {
        &self.parameters
    }

    #[must_use]
    pub const fn required_capabilities(&self) -> &BTreeSet<String> {
        &self.required_capabilities
    }

    #[must_use]
    pub fn expected_events(&self) -> &[String] {
        &self.expected_events
    }

    #[must_use]
    pub fn postconditions(&self) -> &[String] {
        &self.postconditions
    }

    pub fn fingerprint(&self) -> Result<PlanFingerprint, NormalizationError> {
        let value = serde_json::to_value(self).map_err(NormalizationError::Serialization)?;
        Ok(PlanFingerprint(payload_digest(&value)?))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlanFingerprint(String);

impl PlanFingerprint {
    #[must_use]
    pub fn as_hex(&self) -> &str {
        &self.0
    }

    #[must_use]
    pub fn redacted(&self) -> String {
        format!("sha256:{}…", &self.0[..12])
    }
}

#[derive(Debug, Error)]
pub enum NormalizationError {
    #[error(transparent)]
    UnsafeJob(#[from] JobValidationError),
    #[error("legacy job id is invalid")]
    InvalidJobId,
    #[error("legacy job type is not allowlisted")]
    UnknownJobType,
    #[error("legacy executable is not allowlisted")]
    UnknownExecutable,
    #[error("legacy playbook is not allowlisted")]
    UnknownPlaybook,
    #[error("legacy argument is not allowlisted")]
    UnknownArgument,
    #[error("legacy command and structured arguments disagree")]
    ArgumentMismatch,
    #[error("legacy job contains path traversal")]
    PathTraversal,
    #[error("legacy parameter is not a safe scalar")]
    UnsafeParameterValue,
    #[error("legacy synthetic sleep duration is outside 0 through 20 seconds")]
    SleepDurationOutOfRange,
    #[error("legacy job status cannot be normalized")]
    InvalidStatus,
    #[error(transparent)]
    Canonicalization(#[from] CanonicalizationError),
    #[error(transparent)]
    Serialization(serde_json::Error),
}

pub fn normalize_job(job: &JobEnvelope) -> Result<NormalizedPlan, NormalizationError> {
    job.validate_sanitized()?;
    validate_job_id(job.id())?;
    if job.job_type() != JOB_TYPE {
        return Err(NormalizationError::UnknownJobType);
    }
    if job.status() != "pending" {
        return Err(NormalizationError::InvalidStatus);
    }
    reject_traversal(job.playbook())?;
    for argument in job.command() {
        reject_traversal(argument)?;
    }
    if job.playbook() != PLAYBOOK {
        return Err(NormalizationError::UnknownPlaybook);
    }

    let [executable, playbook, extra_flag, extra_value] = job.command() else {
        return Err(NormalizationError::UnknownArgument);
    };
    if executable != EXECUTABLE {
        return Err(NormalizationError::UnknownExecutable);
    }
    if playbook != PLAYBOOK {
        return Err(NormalizationError::UnknownPlaybook);
    }
    if extra_flag != "-e" {
        return Err(NormalizationError::UnknownArgument);
    }
    let Some(command_duration) = extra_value.strip_prefix("duration=") else {
        return Err(NormalizationError::UnknownArgument);
    };

    if job.args().len() != 1 || !job.args().contains_key(ARGUMENT_KEY) {
        return Err(NormalizationError::UnknownArgument);
    }
    let duration = SanitizedValue::from_duration(&job.args()[ARGUMENT_KEY])?;
    let Some(duration_i64) = duration.as_i64() else {
        return Err(NormalizationError::UnsafeParameterValue);
    };
    if !(0..=20).contains(&duration_i64) {
        return Err(NormalizationError::SleepDurationOutOfRange);
    }
    if command_duration != duration_i64.to_string() {
        return Err(NormalizationError::ArgumentMismatch);
    }

    Ok(NormalizedPlan {
        job_id: job.id().to_owned(),
        operation_kind: OperationKind::SyntheticLongSleep,
        contract_version: CONTRACT_VERSION,
        adapter_identity: format!("ansible:{PLAYBOOK}@{CONTRACT_VERSION}"),
        parameters: BTreeMap::from([(ARGUMENT_KEY.to_owned(), duration)]),
        required_capabilities: BTreeSet::from(["ansible_local".to_owned()]),
        expected_events: vec!["adapter_started".to_owned(), "adapter_completed".to_owned()],
        postconditions: vec!["synthetic_sleep_completed".to_owned()],
    })
}

fn validate_job_id(job_id: &str) -> Result<(), NormalizationError> {
    if job_id.is_empty()
        || !job_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(NormalizationError::InvalidJobId);
    }
    Ok(())
}

fn reject_traversal(value: &str) -> Result<(), NormalizationError> {
    if value.split(['/', '\\']).any(|component| component == "..") {
        return Err(NormalizationError::PathTraversal);
    }
    Ok(())
}
