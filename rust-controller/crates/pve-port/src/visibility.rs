//! Sanitized cluster visibility with no absence, ownership, or execution authority.

use std::collections::BTreeSet;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use serde::{Serialize, Serializer, ser::SerializeStruct};
use serde_json::{Map, Value};

use crate::{NodeName, PveReadError, Vmid};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GuestKind {
    Qemu,
    Lxc,
    Unsupported,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum VisiblePower {
    Running,
    Stopped,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct VisibleGuest {
    vmid: Vmid,
    node: NodeName,
    kind: GuestKind,
    template: Option<bool>,
    power: VisiblePower,
}

impl VisibleGuest {
    #[must_use]
    pub const fn vmid(&self) -> Vmid {
        self.vmid
    }

    #[must_use]
    pub const fn node(&self) -> &NodeName {
        &self.node
    }

    #[must_use]
    pub const fn kind(&self) -> GuestKind {
        self.kind
    }

    #[must_use]
    pub const fn template(&self) -> Option<bool> {
        self.template
    }

    #[must_use]
    pub const fn power(&self) -> VisiblePower {
        self.power
    }
}

/// Permission-filtered visibility never proves that an unlisted guest is absent.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ClusterVisibility {
    observed_at: DateTime<Utc>,
    records: Vec<VisibleGuest>,
    rejected_rows: usize,
}

impl ClusterVisibility {
    pub fn from_wire(value: Value, observed_at: DateTime<Utc>) -> Result<Self, PveReadError> {
        let rows = value.as_array().ok_or(PveReadError::InvalidResponse)?;
        if rows.len() > 1024 {
            return Err(PveReadError::InvalidResponse);
        }
        let mut seen = BTreeSet::new();
        let mut records = Vec::new();
        let mut rejected_rows = 0;
        for row in rows {
            let Ok((data, vmid, node)) = parse_identity(row) else {
                rejected_rows += 1;
                continue;
            };
            // Retain valid identities even when metadata subsequently rejects a row.
            // Duplicate detection is defensive validation, never a uniqueness proof.
            if !seen.insert(vmid) {
                return Err(PveReadError::InvalidResponse);
            }
            match parse_guest(data, vmid, node) {
                Ok(guest) => records.push(guest),
                Err(_) => rejected_rows += 1,
            }
        }
        Ok(Self {
            observed_at,
            records,
            rejected_rows,
        })
    }

    #[must_use]
    pub fn records(&self) -> &[VisibleGuest] {
        &self.records
    }

    #[must_use]
    pub const fn rejected_rows(&self) -> usize {
        self.rejected_rows
    }

    #[must_use]
    pub const fn observed_at(&self) -> DateTime<Utc> {
        self.observed_at
    }

    #[must_use]
    pub const fn coverage(&self) -> &'static str {
        "unverified"
    }
}

fn parse_identity(row: &Value) -> Result<(&Map<String, Value>, Vmid, NodeName), PveReadError> {
    let data = row.as_object().ok_or(PveReadError::InvalidResponse)?;
    let vmid = data
        .get("vmid")
        .and_then(Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .ok_or(PveReadError::InvalidResponse)?;
    let vmid = Vmid::new(vmid).map_err(|_| PveReadError::InvalidResponse)?;
    let node = NodeName::parse(
        data.get("node")
            .and_then(Value::as_str)
            .ok_or(PveReadError::InvalidResponse)?,
    )
    .map_err(|_| PveReadError::InvalidResponse)?;
    Ok((data, vmid, node))
}

fn parse_guest(
    data: &Map<String, Value>,
    vmid: Vmid,
    node: NodeName,
) -> Result<VisibleGuest, PveReadError> {
    let kind = match data.get("type").and_then(Value::as_str) {
        Some("qemu") => GuestKind::Qemu,
        Some("lxc") => GuestKind::Lxc,
        Some(_) => GuestKind::Unsupported,
        None => return Err(PveReadError::InvalidResponse),
    };
    let template = match data.get("template") {
        None => None,
        Some(value) => match value.as_u64() {
            Some(0) => Some(false),
            Some(1) => Some(true),
            _ => return Err(PveReadError::InvalidResponse),
        },
    };
    let power = match data.get("status") {
        None => VisiblePower::Unknown,
        Some(value) => match value.as_str() {
            Some("running") => VisiblePower::Running,
            Some("stopped") => VisiblePower::Stopped,
            Some(_) => VisiblePower::Unknown,
            None => return Err(PveReadError::InvalidResponse),
        },
    };
    Ok(VisibleGuest {
        vmid,
        node,
        kind,
        template,
        power,
    })
}

impl Serialize for ClusterVisibility {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut snapshot = serializer.serialize_struct("ClusterVisibility", 4)?;
        snapshot.serialize_field("observed_at", &self.observed_at)?;
        snapshot.serialize_field("records", &self.records)?;
        snapshot.serialize_field("rejected_rows", &self.rejected_rows)?;
        snapshot.serialize_field("coverage", self.coverage())?;
        snapshot.end()
    }
}

/// This capability exposes only cluster visibility, with no execution reads or writes.
#[async_trait]
pub trait PveVisibilityReadPort: Send + Sync {
    async fn cluster_visibility(&self) -> Result<ClusterVisibility, PveReadError>;
}
