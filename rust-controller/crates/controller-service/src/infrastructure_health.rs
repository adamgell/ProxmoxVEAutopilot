//! Component health uses each response's completion clock; visibility grants no execution authority.
use crate::{
    infrastructure_observation::{InfrastructureResult, NetworkObservation, NodeObservation},
    pve_observation::ObservationStatus,
};
use chrono::{DateTime, Utc};
use serde::Serialize;
use std::time::{Duration, Instant};

#[derive(Default)]
pub(crate) struct InfrastructureProgress {
    result: Option<InfrastructureResult>,
    node_last_success: Option<DateTime<Utc>>,
    network_last_success: Option<DateTime<Utc>>,
}
impl InfrastructureProgress {
    pub(crate) fn record(&mut self, result: InfrastructureResult) {
        if node_complete(result.node()) {
            self.node_last_success = result.node().observed_at();
        }
        if network_complete(result.network()) {
            self.network_last_success = result.network().observed_at();
        }
        self.result = Some(result);
    }
    pub(crate) fn snapshot(&self, now: Instant) -> Option<InfrastructureHealth> {
        let result = self.result.as_ref()?;
        Some(InfrastructureHealth {
            node: NodeHealth {
                result: result.node().clone(),
                fresh: node_complete(result.node()) && recent(result.node().completed_at(), now),
                last_success: self.node_last_success,
            },
            network: NetworkHealth {
                result: result.network().clone(),
                fresh: network_complete(result.network())
                    && recent(result.network().completed_at(), now),
                last_success: self.network_last_success,
            },
            coverage: result.coverage(),
        })
    }
}
fn node_complete(node: &NodeObservation) -> bool {
    node.status() == ObservationStatus::Fresh
        && node.observed_at().is_some()
        && node.uptime_known().is_some()
        && node.completed_at().is_some()
}
fn network_complete(network: &NetworkObservation) -> bool {
    network.status() == ObservationStatus::Fresh
        && network.observed_at().is_some()
        && network.counts().is_some()
        && network.completed_at().is_some()
}
fn recent(completed: Option<Instant>, now: Instant) -> bool {
    completed.is_some_and(|at| now.saturating_duration_since(at) <= Duration::from_secs(30))
}
#[derive(Serialize)]
pub(crate) struct InfrastructureHealth {
    node: NodeHealth,
    network: NetworkHealth,
    coverage: &'static str,
}
impl InfrastructureHealth {
    pub(crate) fn ready(&self) -> bool {
        self.node.fresh && self.network.fresh
    }
}
#[derive(Serialize)]
struct NodeHealth {
    #[serde(flatten)]
    result: NodeObservation,
    fresh: bool,
    last_success: Option<DateTime<Utc>>,
}
#[derive(Serialize)]
struct NetworkHealth {
    #[serde(flatten)]
    result: NetworkObservation,
    fresh: bool,
    last_success: Option<DateTime<Utc>>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::infrastructure_observation::test_support::{network, node, result};
    use serde_json::{Value, json};

    fn utc() -> DateTime<Utc> {
        DateTime::from_timestamp(1_700_000_000, 0).unwrap()
    }
    fn complete(node_at: Instant, network_at: Instant) -> InfrastructureResult {
        result(
            node(
                ObservationStatus::Fresh,
                Some(utc()),
                Some(false),
                Some(node_at),
            ),
            network(
                ObservationStatus::Fresh,
                Some(utc()),
                true,
                Some(network_at),
            ),
        )
    }

    // Break: receipt time substituted for component completion, or snapshots refreshing age.
    #[test]
    fn component_freshness_has_exact_boundary_and_snapshots_do_not_refresh_it() {
        let at = Instant::now();
        let mut progress = InfrastructureProgress::default();
        assert!(progress.snapshot(at).is_none());
        progress.record(complete(at, at));
        assert!(
            progress
                .snapshot(at + Duration::from_secs(30))
                .unwrap()
                .ready()
        );
        assert!(
            !progress
                .snapshot(at + Duration::from_millis(30_001))
                .unwrap()
                .ready()
        );
        assert!(
            !progress
                .snapshot(at + Duration::from_secs(60))
                .unwrap()
                .ready()
        );
        assert!(
            progress
                .snapshot(at - Duration::from_secs(1))
                .unwrap()
                .ready()
        );
    }

    #[test]
    fn node_ages_independently_of_later_network_completion() {
        let at = Instant::now();
        let mut progress = InfrastructureProgress::default();
        progress.record(complete(at, at + Duration::from_secs(2)));
        let body =
            serde_json::to_value(progress.snapshot(at + Duration::from_secs(31)).unwrap()).unwrap();
        assert_eq!(body["node"]["fresh"], false);
        assert_eq!(body["network"]["fresh"], true);
    }

    // Break: retained successful timestamps healing current failures or degraded rows.
    #[test]
    fn each_failed_component_retains_last_success_without_granting_readiness() {
        let at = Instant::now();
        for (node_status, network_status) in [
            (ObservationStatus::Unauthorized, ObservationStatus::Fresh),
            (ObservationStatus::Fresh, ObservationStatus::Unauthorized),
            (ObservationStatus::Fresh, ObservationStatus::Degraded),
        ] {
            let mut progress = InfrastructureProgress::default();
            progress.record(complete(at, at));
            let later = utc() + chrono::Duration::seconds(10);
            progress.record(result(
                node(node_status, Some(later), Some(true), Some(at)),
                network(network_status, Some(later), true, Some(at)),
            ));
            let snapshot = progress.snapshot(at).unwrap();
            assert!(!snapshot.ready());
            let body = serde_json::to_value(snapshot).unwrap();
            assert_eq!(
                body["node"]["fresh"],
                node_status == ObservationStatus::Fresh
            );
            assert_eq!(
                body["network"]["fresh"],
                network_status == ObservationStatus::Fresh
            );
            assert_eq!(
                body["node"]["last_success"],
                json!(if node_status == ObservationStatus::Fresh {
                    later
                } else {
                    utc()
                })
            );
            assert_eq!(
                body["network"]["last_success"],
                json!(if network_status == ObservationStatus::Fresh {
                    later
                } else {
                    utc()
                })
            );
        }
    }

    #[test]
    fn missing_component_fields_cannot_set_success_or_readiness() {
        let at = Instant::now();
        for missing in 0..6 {
            let mut progress = InfrastructureProgress::default();
            progress.record(result(
                node(
                    ObservationStatus::Fresh,
                    (missing != 0).then(utc),
                    (missing != 1).then_some(false),
                    (missing != 2).then_some(at),
                ),
                network(
                    ObservationStatus::Fresh,
                    (missing != 3).then(utc),
                    missing != 4,
                    (missing != 5).then_some(at),
                ),
            ));
            let snapshot = progress.snapshot(at).unwrap();
            assert!(!snapshot.ready());
            let body = serde_json::to_value(snapshot).unwrap();
            let (failed, fresh) = if missing < 3 {
                ("node", "network")
            } else {
                ("network", "node")
            };
            assert_eq!(body[failed]["fresh"], false);
            assert!(body[failed]["last_success"].is_null());
            assert_eq!(body[fresh]["fresh"], true);
        }
    }

    #[test]
    fn serialized_health_contains_only_sanitized_components_and_fixed_coverage() {
        let at = Instant::now();
        let mut progress = InfrastructureProgress::default();
        progress.record(complete(at, at));
        let body: Value = serde_json::to_value(progress.snapshot(at).unwrap()).unwrap();
        assert_eq!(body.as_object().unwrap().len(), 3);
        assert_eq!(body["coverage"], "unverified");
        assert_eq!(
            body["node"],
            json!({"status":"fresh","observed_at":utc(),"uptime_known":false,"fresh":true,"last_success":utc()})
        );
        assert_eq!(body["network"].as_object().unwrap().len(), 5);
        assert!(body["network"].get("completed_at").is_none());
        assert_eq!(
            body["network"]["counts"],
            json!({"linux_bridges":0,"ovs_bridges":0,"other_interfaces":0,"active":0,"inactive":0,"activity_unknown":0,"rejected_rows":0})
        );
    }
}
