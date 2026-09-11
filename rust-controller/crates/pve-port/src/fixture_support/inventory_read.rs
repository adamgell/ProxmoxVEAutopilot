//! Historical fixture inventory, without inferred power or provisioning facts.
use super::FixtureSnapshot;
use crate::{NativeVmName, NodeName, PowerState, Vmid};
use chrono::{DateTime, Duration, Utc};
use uuid::Uuid;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FixtureFact<T> {
    Available(T),
    Unavailable,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FixtureVmRead {
    pub vmid: Vmid,
    pub name: NativeVmName,
    pub template: bool,
    pub disk_bytes: u64,
    pub power: FixtureFact<PowerState>,
}

/// Provenance is always supervisor-seeded fixture data, never live PVE evidence.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FixtureInventoryRead {
    pub fixture_id: Uuid,
    pub node: NodeName,
    pub observed_at: DateTime<Utc>,
    pub vms: Vec<FixtureVmRead>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum FixtureProjectionError {
    #[error("invalid fixture inventory")]
    Invalid,
    #[error("fixture identity mismatch")]
    IdentityMismatch,
    #[error("fixture inventory is stale or future-dated")]
    NotFresh,
}

impl FixtureSnapshot {
    /// Validates even directly constructed envelopes. Freshness is evaluated at
    /// the caller's clock, never replaced with the socket retrieval time.
    pub fn project_inventory(
        &self,
        expected_fixture: Uuid,
        expected_node: &NodeName,
        as_of: DateTime<Utc>,
        maximum_age: Duration,
    ) -> Result<FixtureFact<FixtureInventoryRead>, FixtureProjectionError> {
        use FixtureProjectionError as Error;
        if expected_fixture.is_nil() || maximum_age < Duration::zero() {
            return Err(Error::Invalid);
        }
        let bytes = serde_json::to_vec(self).map_err(|_| Error::Invalid)?;
        Self::decode(&bytes).map_err(|_| Error::Invalid)?;
        let Self::Inventory { inventory } = self else {
            return Ok(FixtureFact::Unavailable);
        };
        let node = NodeName::parse(&inventory.node).map_err(|_| Error::Invalid)?;
        if inventory.fixture_id != expected_fixture || &node != expected_node {
            return Err(Error::IdentityMismatch);
        }
        let millis = i64::try_from(inventory.observed_unix_ms).map_err(|_| Error::Invalid)?;
        let observed_at = DateTime::from_timestamp_millis(millis).ok_or(Error::Invalid)?;
        if observed_at > as_of || as_of - observed_at > maximum_age {
            return Err(Error::NotFresh);
        }
        let vms = inventory
            .vms
            .iter()
            .map(|vm| {
                Ok(FixtureVmRead {
                    vmid: Vmid::new(vm.vmid).map_err(|_| Error::Invalid)?,
                    name: NativeVmName::parse(&vm.name).map_err(|_| Error::Invalid)?,
                    template: vm.template,
                    disk_bytes: vm.disk_bytes,
                    power: FixtureFact::Unavailable,
                })
            })
            .collect::<Result<Vec<_>, Error>>()?;
        Ok(FixtureFact::Available(FixtureInventoryRead {
            fixture_id: inventory.fixture_id,
            node,
            observed_at,
            vms,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fixture_support::{FixtureInventory, FixtureVmConfig};

    #[test]
    fn projection_preserves_seed_time_and_refuses_to_invent_power() {
        let id = Uuid::now_v7();
        let node = NodeName::parse("fixture-node").unwrap();
        let snapshot = FixtureSnapshot::Inventory {
            inventory: FixtureInventory {
                version: 1,
                fixture_id: id,
                node: node.to_string(),
                observed_unix_ms: 1000,
                vms: vec![FixtureVmConfig {
                    vmid: 109,
                    name: "fixture-vm".into(),
                    template: true,
                    disk_bytes: 4096,
                }],
            },
        };
        let now = DateTime::from_timestamp_millis(2000).unwrap();
        let age = Duration::seconds(1);
        let FixtureFact::Available(read) = snapshot.project_inventory(id, &node, now, age).unwrap()
        else {
            panic!("missing inventory")
        };
        assert_eq!(read.fixture_id, id);
        assert_eq!(read.observed_at.timestamp_millis(), 1000);
        assert_eq!(read.vms[0].vmid.get(), 109);
        assert_eq!(read.vms[0].power, FixtureFact::Unavailable);
        assert_eq!(
            snapshot.project_inventory(id, &node, now + Duration::milliseconds(1), age),
            Err(FixtureProjectionError::NotFresh)
        );
        assert_eq!(
            snapshot.project_inventory(id, &node, now - Duration::seconds(2), age),
            Err(FixtureProjectionError::NotFresh)
        );
        assert_eq!(
            snapshot.project_inventory(Uuid::now_v7(), &node, now, age),
            Err(FixtureProjectionError::IdentityMismatch)
        );
        let mut invalid = snapshot;
        if let FixtureSnapshot::Inventory { inventory } = &mut invalid {
            inventory.vms[0].vmid = 1;
        }
        assert_eq!(
            invalid.project_inventory(id, &node, now, age),
            Err(FixtureProjectionError::Invalid)
        );
        assert_eq!(
            FixtureSnapshot::Unavailable {}
                .project_inventory(id, &node, now, age)
                .unwrap(),
            FixtureFact::Unavailable
        );
    }
}
