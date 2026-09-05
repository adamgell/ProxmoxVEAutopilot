use pve_port::NativeVmName;
use serde::Serialize;

use crate::ContractError;

/// Grow-only binary-GiB capacity, independent of free-storage sufficiency.
/// Template bytes are a pinned input, not a fresh observation.
/// Deserialization cannot bypass admission.
/// ```compile_fail,E0277
/// use osdeploy_adapter::DiskCapacity;
/// let _: DiskCapacity = serde_json::from_str("{}").unwrap();
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct DiskCapacity {
    requested_gib: u64,
    template_bytes: u64,
    effective_bytes: u64,
    growth_required: bool,
}

impl DiskCapacity {
    pub fn new(requested_gib: u64, template_bytes: u64) -> Result<Self, ContractError> {
        if requested_gib < 80 {
            return Err(ContractError::InvalidRequestedDiskCapacity);
        }
        if template_bytes == 0 {
            return Err(ContractError::InvalidTemplateCapacity);
        }
        let requested_bytes = requested_gib
            .checked_mul(1_073_741_824)
            .ok_or(ContractError::DiskCapacityOverflow)?;
        Ok(Self {
            requested_gib,
            template_bytes,
            effective_bytes: template_bytes.max(requested_bytes),
            growth_required: requested_bytes > template_bytes,
        })
    }
    pub fn requested_gib(&self) -> u64 {
        self.requested_gib
    }
    pub fn template_bytes(&self) -> u64 {
        self.template_bytes
    }
    pub fn effective_bytes(&self) -> u64 {
        self.effective_bytes
    }
    pub fn growth_required(&self) -> bool {
        self.growth_required
    }
}

/// Exact legacy normalization, including truncation after stripping edge hyphens.
/// This helper can return names that native admission rejects.
pub fn normalize_legacy_windows_name(input: &str) -> String {
    let ascii: String = input
        .trim()
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || *character == '-')
        .collect();
    ascii.trim_matches('-').chars().take(15).collect()
}

/// Separate requested/PVE identities and deterministic Windows/agent names.
/// Agent-ID collisions require a later database reservation; no uniqueness is proven.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct DeploymentNames {
    requested_name: String,
    pve_name: NativeVmName,
    windows_name: String,
    expected_agent_id: String,
}

impl DeploymentNames {
    pub fn new(requested_name: &str, pve_name: NativeVmName) -> Result<Self, ContractError> {
        if requested_name.is_empty()
            || requested_name.len() > 256
            || requested_name.chars().any(char::is_control)
        {
            return Err(ContractError::InvalidRequestedName);
        }
        let windows_name = normalize_legacy_windows_name(requested_name);
        if windows_name.is_empty()
            || windows_name.ends_with('-')
            || windows_name.bytes().all(|byte| byte.is_ascii_digit())
        {
            return Err(ContractError::InvalidWindowsName);
        }
        let expected_agent_id = format!("agent-{}", windows_name.to_ascii_lowercase());
        Ok(Self {
            requested_name: requested_name.to_owned(),
            pve_name,
            windows_name,
            expected_agent_id,
        })
    }
    pub fn requested_name(&self) -> &str {
        &self.requested_name
    }
    pub fn pve_name(&self) -> &NativeVmName {
        &self.pve_name
    }
    pub fn windows_name(&self) -> &str {
        &self.windows_name
    }
    pub fn expected_agent_id(&self) -> &str {
        &self.expected_agent_id
    }
}

/// Constructor input only; not a durable or wire DTO.
pub struct PhasePolicyInput {
    pub registration_seconds: u32,
    pub pe_seconds: u32,
    pub shutdown_grace_seconds: u32,
    pub full_os_seconds: u32,
    pub mutation_seconds: u32,
    pub evidence_freshness_seconds: u32,
    pub allow_force_stop: bool,
}

/// Pinned budgets without wall-clock anchors or execution authority.
/// Full-OS actions later share one absolute deadline; lease renewal cannot reset it.
/// ```compile_fail,E0277
/// use osdeploy_adapter::PhasePolicy;
/// let _: PhasePolicy = serde_json::from_str("{}").unwrap();
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct PhasePolicy {
    registration_seconds: u32,
    pe_seconds: u32,
    shutdown_grace_seconds: u32,
    full_os_seconds: u32,
    mutation_seconds: u32,
    evidence_freshness_seconds: u32,
    allow_force_stop: bool,
}

impl PhasePolicy {
    pub fn new(input: PhasePolicyInput) -> Result<Self, ContractError> {
        if [
            input.registration_seconds,
            input.pe_seconds,
            input.shutdown_grace_seconds,
            input.full_os_seconds,
            input.mutation_seconds,
        ]
        .into_iter()
        .any(|seconds| !(1..=86_400).contains(&seconds))
        {
            return Err(ContractError::InvalidPhaseDuration);
        }
        if !(1..=300).contains(&input.evidence_freshness_seconds)
            || input.evidence_freshness_seconds > input.mutation_seconds
        {
            return Err(ContractError::InvalidEvidenceFreshness);
        }
        Ok(Self {
            registration_seconds: input.registration_seconds,
            pe_seconds: input.pe_seconds,
            shutdown_grace_seconds: input.shutdown_grace_seconds,
            full_os_seconds: input.full_os_seconds,
            mutation_seconds: input.mutation_seconds,
            evidence_freshness_seconds: input.evidence_freshness_seconds,
            allow_force_stop: input.allow_force_stop,
        })
    }
    pub fn production_defaults(allow_force_stop: bool) -> Self {
        Self::new(PhasePolicyInput {
            registration_seconds: 2400,
            pe_seconds: 7200,
            shutdown_grace_seconds: 300,
            full_os_seconds: 7200,
            mutation_seconds: 300,
            evidence_freshness_seconds: 30,
            allow_force_stop,
        })
        .expect("fixed production policy is valid")
    }
    pub fn registration_seconds(&self) -> u32 {
        self.registration_seconds
    }
    pub fn pe_seconds(&self) -> u32 {
        self.pe_seconds
    }
    pub fn shutdown_grace_seconds(&self) -> u32 {
        self.shutdown_grace_seconds
    }
    pub fn full_os_seconds(&self) -> u32 {
        self.full_os_seconds
    }
    pub fn mutation_seconds(&self) -> u32 {
        self.mutation_seconds
    }
    pub fn evidence_freshness_seconds(&self) -> u32 {
        self.evidence_freshness_seconds
    }
    pub fn allow_force_stop(&self) -> bool {
        self.allow_force_stop
    }
}
