use std::{collections::BTreeSet, fmt, str::FromStr, time::Duration};

use chrono::{DateTime, Utc};
use controller_domain::ObservationHealth;
use serde::{Deserialize, Deserializer, Serialize, Serializer, de};
use thiserror::Error;
use uuid::Uuid;

macro_rules! validated_text {
    ($name:ident, $validator:expr, $message:literal) => {
        #[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
        pub struct $name(String);

        impl $name {
            pub fn parse(value: impl Into<String>) -> Result<Self, PveValidationError> {
                let value = value.into();
                if !($validator)(&value) {
                    return Err(PveValidationError::InvalidIdentifier($message));
                }
                Ok(Self(value))
            }

            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str(&self.0)
            }
        }

        impl Serialize for $name {
            fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
            where
                S: Serializer,
            {
                serializer.serialize_str(&self.0)
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
            where
                D: Deserializer<'de>,
            {
                Self::parse(String::deserialize(deserializer)?).map_err(de::Error::custom)
            }
        }
    };
}

fn safe_pve_name(value: &str) -> bool {
    let mut chars = value.chars();
    matches!(chars.next(), Some(first) if first.is_ascii_alphanumeric())
        && value.len() <= 64
        && chars.all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.')
        })
}

fn safe_upid(value: &str) -> bool {
    value.starts_with("UPID:")
        && value.len() <= 512
        && value.chars().all(|character| {
            character.is_ascii_graphic() && !matches!(character, '/' | '\\' | '?' | '#')
        })
}

validated_text!(NodeName, safe_pve_name, "invalid PVE node name");
validated_text!(StorageName, safe_pve_name, "invalid PVE storage name");
validated_text!(Upid, safe_upid, "invalid PVE UPID");

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct Vmid(u32);

impl Vmid {
    pub fn new(value: u32) -> Result<Self, PveValidationError> {
        if !(100..=999_999_999).contains(&value) {
            return Err(PveValidationError::InvalidVmid);
        }
        Ok(Self(value))
    }

    #[must_use]
    pub const fn get(self) -> u32 {
        self.0
    }
}

impl fmt::Display for Vmid {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

impl<'de> Deserialize<'de> for Vmid {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        Self::new(u32::deserialize(deserializer)?).map_err(de::Error::custom)
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct VmUuid(Uuid);

impl VmUuid {
    pub fn parse(value: &str) -> Result<Self, PveValidationError> {
        let uuid = Uuid::from_str(value).map_err(|_| PveValidationError::InvalidVmUuid)?;
        if uuid.is_nil() {
            return Err(PveValidationError::InvalidVmUuid);
        }
        Ok(Self(uuid))
    }

    #[must_use]
    pub const fn as_uuid(self) -> Uuid {
        self.0
    }
}

impl fmt::Display for VmUuid {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

impl<'de> Deserialize<'de> for VmUuid {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        Self::parse(&String::deserialize(deserializer)?).map_err(de::Error::custom)
    }
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct MacAddress(String);

impl MacAddress {
    pub fn parse(value: &str) -> Result<Self, PveValidationError> {
        let octets = value.split(':').collect::<Vec<_>>();
        if octets.len() != 6
            || octets
                .iter()
                .any(|octet| octet.len() != 2 || !octet.chars().all(|c| c.is_ascii_hexdigit()))
        {
            return Err(PveValidationError::InvalidMacAddress);
        }
        Ok(Self(value.to_ascii_uppercase()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for MacAddress {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl<'de> Deserialize<'de> for MacAddress {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        Self::parse(&String::deserialize(deserializer)?).map_err(de::Error::custom)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PveBaseUrl(reqwest::Url);

impl PveBaseUrl {
    pub fn parse(value: &str) -> Result<Self, PveValidationError> {
        let url = reqwest::Url::parse(value).map_err(|_| PveValidationError::InvalidBaseUrl)?;
        let host = url.host_str().ok_or(PveValidationError::InvalidBaseUrl)?;
        let loopback = host
            .parse::<std::net::IpAddr>()
            .is_ok_and(|address| address.is_loopback());
        if !matches!(url.scheme(), "http" | "https")
            || (url.scheme() == "http" && !loopback)
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || !matches!(url.path(), "" | "/")
        {
            return Err(PveValidationError::InvalidBaseUrl);
        }
        Ok(Self(url))
    }

    #[must_use]
    pub fn is_builtin_production(&self) -> bool {
        self.0.host_str() == Some("192.168.2.4")
    }

    pub(crate) fn endpoint(&self, segments: &[&str]) -> reqwest::Url {
        let mut url = self.0.clone();
        url.path_segments_mut()
            .expect("validated HTTP URLs support path segments")
            .clear()
            .extend(segments);
        url
    }
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum PveValidationError {
    #[error("{0}")]
    InvalidIdentifier(&'static str),
    #[error("VMID must be between 100 and 999999999")]
    InvalidVmid,
    #[error("VM UUID must be a non-nil UUID")]
    InvalidVmUuid,
    #[error("MAC address must contain six hexadecimal octets")]
    InvalidMacAddress,
    #[error("PVE base URL must be credential-free HTTPS, or loopback HTTP, with no path or query")]
    InvalidBaseUrl,
    #[error("clone intent requires at least one expected MAC address")]
    MissingExpectedMac,
    #[error("observation maximum age must be greater than zero")]
    ZeroMaximumAge,
    #[error("volume identifier must not be blank")]
    EmptyVolumeId,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct VmConfig {
    vmid: Vmid,
    smbios_uuid: Option<VmUuid>,
    mac_addresses: BTreeSet<MacAddress>,
    observed_at: DateTime<Utc>,
}

impl VmConfig {
    #[must_use]
    pub const fn new(
        vmid: Vmid,
        smbios_uuid: Option<VmUuid>,
        mac_addresses: BTreeSet<MacAddress>,
        observed_at: DateTime<Utc>,
    ) -> Self {
        Self {
            vmid,
            smbios_uuid,
            mac_addresses,
            observed_at,
        }
    }

    #[must_use]
    pub const fn vmid(&self) -> Vmid {
        self.vmid
    }

    #[must_use]
    pub const fn smbios_uuid(&self) -> Option<VmUuid> {
        self.smbios_uuid
    }

    #[must_use]
    pub fn mac_addresses(&self) -> &BTreeSet<MacAddress> {
        &self.mac_addresses
    }

    #[must_use]
    pub const fn observed_at(&self) -> DateTime<Utc> {
        self.observed_at
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskState {
    Running,
    CompleteSuccess,
    CompleteFailure,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct TaskStatus {
    upid: Upid,
    state: TaskState,
    observed_at: DateTime<Utc>,
}

impl TaskStatus {
    #[must_use]
    pub const fn new(upid: Upid, state: TaskState, observed_at: DateTime<Utc>) -> Self {
        Self {
            upid,
            state,
            observed_at,
        }
    }

    #[must_use]
    pub fn running(upid: Upid, observed_at: DateTime<Utc>) -> Self {
        Self::new(upid, TaskState::Running, observed_at)
    }

    #[must_use]
    pub fn complete(upid: Upid, observed_at: DateTime<Utc>) -> Self {
        Self::new(upid, TaskState::CompleteSuccess, observed_at)
    }

    #[must_use]
    pub fn failed(upid: Upid, observed_at: DateTime<Utc>) -> Self {
        Self::new(upid, TaskState::CompleteFailure, observed_at)
    }

    #[must_use]
    pub fn upid(&self) -> &Upid {
        &self.upid
    }

    #[must_use]
    pub const fn state(&self) -> TaskState {
        self.state
    }

    #[must_use]
    pub const fn succeeded(&self) -> bool {
        matches!(self.state, TaskState::CompleteSuccess)
    }

    #[must_use]
    pub const fn observed_at(&self) -> DateTime<Utc> {
        self.observed_at
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct Volume {
    id: String,
    observed_at: DateTime<Utc>,
}

impl Volume {
    pub fn new(
        id: impl Into<String>,
        observed_at: DateTime<Utc>,
    ) -> Result<Self, PveValidationError> {
        let id = id.into();
        if id.trim().is_empty() {
            return Err(PveValidationError::EmptyVolumeId);
        }
        Ok(Self { id, observed_at })
    }

    #[must_use]
    pub fn id(&self) -> &str {
        &self.id
    }

    #[must_use]
    pub const fn observed_at(&self) -> DateTime<Utc> {
        self.observed_at
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct QgaStatus {
    reachable: bool,
    observed_at: DateTime<Utc>,
}

impl QgaStatus {
    #[must_use]
    pub const fn new(reachable: bool, observed_at: DateTime<Utc>) -> Self {
        Self {
            reachable,
            observed_at,
        }
    }

    #[must_use]
    pub const fn reachable(self) -> bool {
        self.reachable
    }

    #[must_use]
    pub const fn observed_at(self) -> DateTime<Utc> {
        self.observed_at
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CloneIntent {
    pub(crate) node: NodeName,
    pub(crate) vmid: Vmid,
    pub(crate) upid: Upid,
    pub(crate) expected_uuid: VmUuid,
    pub(crate) expected_macs: BTreeSet<MacAddress>,
    pub(crate) as_of: DateTime<Utc>,
    pub(crate) maximum_age: Duration,
}

impl CloneIntent {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        node: NodeName,
        vmid: Vmid,
        upid: Upid,
        expected_uuid: VmUuid,
        expected_macs: BTreeSet<MacAddress>,
        as_of: DateTime<Utc>,
        maximum_age: Duration,
    ) -> Result<Self, PveValidationError> {
        if expected_macs.is_empty() {
            return Err(PveValidationError::MissingExpectedMac);
        }
        if maximum_age.is_zero() {
            return Err(PveValidationError::ZeroMaximumAge);
        }
        Ok(Self {
            node,
            vmid,
            upid,
            expected_uuid,
            expected_macs,
            as_of,
            maximum_age,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EvidenceSource {
    PveApi,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(tag = "fact", rename_all = "snake_case")]
pub enum PveFactKind {
    TaskCompletion { complete: bool },
    VmIdentity { satisfied: bool },
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct PveFact {
    pub kind: PveFactKind,
    pub observed_at: DateTime<Utc>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct PveEvidence {
    pub task_complete: Option<bool>,
    pub vm_identity_satisfied: Option<bool>,
    pub health: ObservationHealth,
    pub observed_at: DateTime<Utc>,
    pub source: EvidenceSource,
    pub facts: Vec<PveFact>,
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum PveReadError {
    #[error("PVE read was unauthorized")]
    Unauthorized,
    #[error("PVE resource was not found")]
    NotFound,
    #[error("PVE resource is locked or conflicted")]
    Conflict,
    #[error("PVE read timed out")]
    TimedOut,
    #[error("PVE response did not match the typed contract")]
    InvalidResponse,
    #[error("PVE transport was unavailable")]
    TransportUnavailable,
}
