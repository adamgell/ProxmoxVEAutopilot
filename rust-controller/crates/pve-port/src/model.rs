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

validated_text!(NodeName, safe_pve_name, "invalid PVE node name");
validated_text!(StorageName, safe_pve_name, "invalid PVE storage name");
validated_text!(
    BridgeName,
    |value: &str| {
        !value.is_empty()
            && value.len() <= 15
            && value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
    },
    "invalid PVE bridge name"
);
validated_text!(
    NativeVmName,
    |value: &str| {
        !value.is_empty()
            && value.len() <= 63
            && value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    },
    "invalid native VM name"
);

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct Upid {
    raw: String,
    node: NodeName,
    process_id: u32,
    process_start: u32,
    task_start: u32,
    worker_type: String,
    worker_id: Option<String>,
    authenticated_user: String,
}

impl Upid {
    pub fn parse(value: impl Into<String>) -> Result<Self, PveValidationError> {
        let raw = value.into();
        let fields = raw.split(':').collect::<Vec<_>>();
        if raw.len() > 512
            || fields.len() != 9
            || fields[0] != "UPID"
            || !fields[8].is_empty()
            || !safe_upid_field(fields[5], false)
            || !safe_upid_field(fields[6], true)
            || !valid_authenticated_user(fields[7])
        {
            return Err(PveValidationError::InvalidIdentifier("invalid PVE UPID"));
        }

        let node = NodeName::parse(fields[1])
            .map_err(|_| PveValidationError::InvalidIdentifier("invalid PVE UPID"))?;
        let process_id = parse_upid_hex(fields[2])?;
        let process_start = parse_upid_hex(fields[3])?;
        let task_start = parse_upid_hex(fields[4])?;
        let worker_type = fields[5].to_owned();
        let worker_id = (!fields[6].is_empty()).then(|| fields[6].to_owned());
        let authenticated_user = fields[7].to_owned();

        Ok(Self {
            raw,
            node,
            process_id,
            process_start,
            task_start,
            worker_type,
            worker_id,
            authenticated_user,
        })
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.raw
    }

    #[must_use]
    pub const fn node(&self) -> &NodeName {
        &self.node
    }

    #[must_use]
    pub const fn process_id(&self) -> u32 {
        self.process_id
    }

    #[must_use]
    pub const fn process_start(&self) -> u32 {
        self.process_start
    }

    #[must_use]
    pub const fn task_start(&self) -> u32 {
        self.task_start
    }

    #[must_use]
    pub fn worker_type(&self) -> &str {
        &self.worker_type
    }

    #[must_use]
    pub fn worker_id(&self) -> Option<&str> {
        self.worker_id.as_deref()
    }

    #[must_use]
    pub fn authenticated_user(&self) -> &str {
        &self.authenticated_user
    }
}

fn parse_upid_hex(value: &str) -> Result<u32, PveValidationError> {
    if value.len() != 8 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(PveValidationError::InvalidIdentifier("invalid PVE UPID"));
    }
    u32::from_str_radix(value, 16)
        .map_err(|_| PveValidationError::InvalidIdentifier("invalid PVE UPID"))
}

fn safe_upid_field(value: &str, allow_empty: bool) -> bool {
    (allow_empty || !value.is_empty())
        && value
            .chars()
            .all(|character| character.is_ascii_graphic() && character != ':')
}

fn valid_authenticated_user(value: &str) -> bool {
    safe_upid_field(value, false)
        && value.split_once('@').is_some_and(|(user, realm)| {
            !user.is_empty() && !realm.is_empty() && !user.contains('@') && !realm.contains('@')
        })
}

impl fmt::Display for Upid {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.raw)
    }
}

impl Serialize for Upid {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&self.raw)
    }
}

impl<'de> Deserialize<'de> for Upid {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        Self::parse(String::deserialize(deserializer)?).map_err(de::Error::custom)
    }
}

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
        let normalized_host = host
            .strip_prefix('[')
            .and_then(|host| host.strip_suffix(']'))
            .unwrap_or(host);
        let loopback = normalized_host
            .parse::<std::net::IpAddr>()
            .is_ok_and(is_normalized_loopback);
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
    pub fn is_loopback(&self) -> bool {
        self.0.host_str().is_some_and(|host| {
            host.strip_prefix('[')
                .and_then(|host| host.strip_suffix(']'))
                .unwrap_or(host)
                .parse::<std::net::IpAddr>()
                .is_ok_and(is_normalized_loopback)
        })
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

fn is_normalized_loopback(address: std::net::IpAddr) -> bool {
    match address {
        std::net::IpAddr::V4(address) => address.is_loopback(),
        std::net::IpAddr::V6(address) => {
            address.is_loopback()
                || address
                    .to_ipv4_mapped()
                    .is_some_and(|mapped| mapped.is_loopback())
        }
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
    #[error("UPID node must match the requested PVE node")]
    UpidNodeMismatch,
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
        maximum_age: Duration,
    ) -> Result<Self, PveValidationError> {
        if upid.node() != &node {
            return Err(PveValidationError::UpidNodeMismatch);
        }
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
    TaskState { state: TaskState },
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
    pub task_state: Option<TaskState>,
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
    #[error("UPID node does not match the requested PVE node")]
    UpidNodeMismatch,
    #[error("PVE transport was unavailable")]
    TransportUnavailable,
}
