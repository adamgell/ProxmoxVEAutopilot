//! Sanitized node and network observations with no execution authority.

use std::collections::BTreeSet;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use serde::{Serialize, Serializer, ser::SerializeStruct};
use serde_json::{Map, Value};

use crate::{NodeName, PveReadError};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InterfaceKind {
    LinuxBridge,
    OvsBridge,
    Other,
}

/// A bounded opaque interface identity is not a validated execution bridge.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct VisibleInterface {
    name: String,
    kind: InterfaceKind,
    active: Option<bool>,
}

impl VisibleInterface {
    #[must_use]
    pub fn name(&self) -> &str {
        &self.name
    }
    #[must_use]
    pub const fn kind(&self) -> InterfaceKind {
        self.kind
    }
    #[must_use]
    pub const fn active(&self) -> Option<bool> {
        self.active
    }
}

/// Uptime visibility does not establish online or execution-ready status.
///
/// ```compile_fail,E0277
/// use pve_port::{NodeStatus, NodeVisibility};
/// fn promote(report: NodeVisibility) -> NodeStatus {
///     report.into()
/// }
/// ```
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NodeVisibility {
    node: NodeName,
    uptime: Option<u64>,
    observed_at: DateTime<Utc>,
}

impl NodeVisibility {
    pub fn from_wire(
        node: NodeName,
        value: Value,
        observed_at: DateTime<Utc>,
    ) -> Result<Self, PveReadError> {
        let data = value.as_object().ok_or(PveReadError::InvalidResponse)?;
        validate_node_binding(data, &node)?;
        let uptime = data
            .get("uptime")
            .map(|value| value.as_u64().ok_or(PveReadError::InvalidResponse))
            .transpose()?;
        Ok(Self {
            node,
            uptime,
            observed_at,
        })
    }
    #[must_use]
    pub const fn node(&self) -> &NodeName {
        &self.node
    }
    #[must_use]
    pub const fn uptime(&self) -> Option<u64> {
        self.uptime
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

/// Permission-filtered network visibility never proves absence or readiness.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NetworkVisibility {
    node: NodeName,
    records: Vec<VisibleInterface>,
    rejected_rows: usize,
    observed_at: DateTime<Utc>,
}

impl NetworkVisibility {
    pub fn from_wire(
        node: NodeName,
        value: Value,
        observed_at: DateTime<Utc>,
    ) -> Result<Self, PveReadError> {
        let rows = value.as_array().ok_or(PveReadError::InvalidResponse)?;
        if rows.len() > 1024 {
            return Err(PveReadError::InvalidResponse);
        }
        let mut seen = BTreeSet::new();
        let mut records = Vec::new();
        let mut rejected_rows = 0;
        for row in rows {
            let Some(data) = row.as_object() else {
                rejected_rows += 1;
                continue;
            };
            // Node binding is report-wide validation, even for a malformed row.
            validate_node_binding(data, &node)?;
            let Ok(name) = parse_interface_name(data) else {
                rejected_rows += 1;
                continue;
            };
            // Keep identities before metadata rejection so duplicates cannot hide.
            if !seen.insert(name) {
                return Err(PveReadError::InvalidResponse);
            }
            match parse_interface(data, name) {
                Ok(interface) => records.push(interface),
                Err(_) => rejected_rows += 1,
            }
        }
        Ok(Self {
            node,
            records,
            rejected_rows,
            observed_at,
        })
    }
    #[must_use]
    pub const fn node(&self) -> &NodeName {
        &self.node
    }
    #[must_use]
    pub fn records(&self) -> &[VisibleInterface] {
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

fn validate_node_binding(
    data: &Map<String, Value>,
    expected: &NodeName,
) -> Result<(), PveReadError> {
    if let Some(value) = data.get("node") {
        let name = value.as_str().ok_or(PveReadError::InvalidResponse)?;
        let node = NodeName::parse(name).map_err(|_| PveReadError::InvalidResponse)?;
        if &node != expected {
            return Err(PveReadError::InvalidResponse);
        }
    }
    Ok(())
}

fn parse_interface_name(data: &Map<String, Value>) -> Result<&str, PveReadError> {
    let name = data
        .get("iface")
        .and_then(Value::as_str)
        .ok_or(PveReadError::InvalidResponse)?;
    if !(1..=64).contains(&name.len())
        || !name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte))
    {
        return Err(PveReadError::InvalidResponse);
    }
    Ok(name)
}

fn parse_interface(
    data: &Map<String, Value>,
    name: &str,
) -> Result<VisibleInterface, PveReadError> {
    let kind = match data.get("type").and_then(Value::as_str) {
        Some("bridge") => InterfaceKind::LinuxBridge,
        Some("OVSBridge") => InterfaceKind::OvsBridge,
        Some(_) => InterfaceKind::Other,
        None => return Err(PveReadError::InvalidResponse),
    };
    let active = match data.get("active") {
        None => None,
        Some(value) => match value.as_u64() {
            Some(0) => Some(false),
            Some(1) => Some(true),
            _ => return Err(PveReadError::InvalidResponse),
        },
    };
    Ok(VisibleInterface {
        name: name.to_owned(),
        kind,
        active,
    })
}

impl Serialize for NodeVisibility {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut report = serializer.serialize_struct("NodeVisibility", 4)?;
        report.serialize_field("node", &self.node)?;
        report.serialize_field("uptime", &self.uptime)?;
        report.serialize_field("observed_at", &self.observed_at)?;
        report.serialize_field("coverage", self.coverage())?;
        report.end()
    }
}

impl Serialize for NetworkVisibility {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut report = serializer.serialize_struct("NetworkVisibility", 5)?;
        report.serialize_field("node", &self.node)?;
        report.serialize_field("records", &self.records)?;
        report.serialize_field("rejected_rows", &self.rejected_rows)?;
        report.serialize_field("observed_at", &self.observed_at)?;
        report.serialize_field("coverage", self.coverage())?;
        report.end()
    }
}

/// Only node and network GET visibility is available through this capability.
///
/// ```compile_fail,E0599
/// use pve_port::{NodeName, PveInfrastructureVisibilityReadPort, PveReadPort, Vmid};
/// async fn qga(port: &dyn PveInfrastructureVisibilityReadPort, node: &NodeName, vmid: Vmid) {
///     port.qga_ping(node, vmid).await;
/// }
/// ```
///
/// ```compile_fail,E0599
/// use pve_port::{CloneRequest, PveInfrastructureVisibilityReadPort, PveMutationPort};
/// async fn mutate(port: &dyn PveInfrastructureVisibilityReadPort, request: &CloneRequest) {
///     port.clone_vm(request).await;
/// }
/// ```
///
/// ```compile_fail,E0599
/// use pve_port::{NodeName, PveInfrastructureVisibilityReadPort, PvePreflightReadPort};
/// async fn native(port: &dyn PveInfrastructureVisibilityReadPort, node: &NodeName) {
///     port.node_status(node).await;
/// }
/// ```
#[async_trait]
pub trait PveInfrastructureVisibilityReadPort: Send + Sync {
    async fn node_visibility(&self, node: &NodeName) -> Result<NodeVisibility, PveReadError>;
    async fn network_visibility(&self, node: &NodeName) -> Result<NetworkVisibility, PveReadError>;
}
