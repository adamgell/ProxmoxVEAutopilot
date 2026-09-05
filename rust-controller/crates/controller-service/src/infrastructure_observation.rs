//! Bounded, sanitized visibility for one explicitly selected node.

use crate::{
    config::ValidatedObservationConfig,
    pve_observation::{ObservationSetupFailure, ObservationStatus},
};
use chrono::{DateTime, Utc};
use pve_port::{
    InterfaceKind, NetworkVisibility, NodeName, NodeVisibility, PveAccessMode, PveApiToken,
    PveInfrastructureVisibilityReadPort, PveObserverConfig, PveReadError, ReqwestPveObserver,
};
use serde::{Serialize, Serializer, ser::SerializeStruct};
use std::time::{Duration, Instant};

#[derive(Clone, Serialize)]
pub(crate) struct NodeObservation {
    status: ObservationStatus,
    observed_at: Option<DateTime<Utc>>,
    uptime_known: Option<bool>,
    #[serde(skip)]
    completed_at: Option<Instant>,
}
impl NodeObservation {
    fn from_read(result: Result<NodeVisibility, PveReadError>) -> Self {
        let completed_at = Instant::now();
        match result {
            Ok(report) => Self {
                status: ObservationStatus::Fresh,
                observed_at: Some(report.observed_at()),
                uptime_known: Some(report.uptime().is_some()),
                completed_at: Some(completed_at),
            },
            Err(error) => Self {
                status: error_status(error),
                observed_at: None,
                uptime_known: None,
                completed_at: None,
            },
        }
    }
    pub(crate) fn status(&self) -> ObservationStatus {
        self.status
    }
    pub(crate) fn observed_at(&self) -> Option<DateTime<Utc>> {
        self.observed_at
    }
    pub(crate) fn uptime_known(&self) -> Option<bool> {
        self.uptime_known
    }
    pub(crate) fn completed_at(&self) -> Option<Instant> {
        self.completed_at
    }
}

#[derive(Clone, Serialize)]
pub(crate) struct NetworkObservationCounts {
    linux_bridges: usize,
    ovs_bridges: usize,
    other_interfaces: usize,
    active: usize,
    inactive: usize,
    activity_unknown: usize,
    rejected_rows: usize,
}

#[derive(Clone, Serialize)]
pub(crate) struct NetworkObservation {
    status: ObservationStatus,
    observed_at: Option<DateTime<Utc>>,
    counts: Option<NetworkObservationCounts>,
    #[serde(skip)]
    completed_at: Option<Instant>,
}
impl NetworkObservation {
    fn from_read(result: Result<NetworkVisibility, PveReadError>) -> Self {
        let completed_at = Instant::now();
        let report = match result {
            Ok(report) => report,
            Err(error) => {
                return Self {
                    status: error_status(error),
                    observed_at: None,
                    counts: None,
                    completed_at: None,
                };
            }
        };
        let mut counts = NetworkObservationCounts {
            linux_bridges: 0,
            ovs_bridges: 0,
            other_interfaces: 0,
            active: 0,
            inactive: 0,
            activity_unknown: 0,
            rejected_rows: report.rejected_rows(),
        };
        for interface in report.records() {
            match interface.kind() {
                InterfaceKind::LinuxBridge => counts.linux_bridges += 1,
                InterfaceKind::OvsBridge => counts.ovs_bridges += 1,
                InterfaceKind::Other => counts.other_interfaces += 1,
            }
            match interface.active() {
                Some(true) => counts.active += 1,
                Some(false) => counts.inactive += 1,
                None => counts.activity_unknown += 1,
            }
        }
        Self {
            status: if counts.rejected_rows == 0 {
                ObservationStatus::Fresh
            } else {
                ObservationStatus::Degraded
            },
            observed_at: Some(report.observed_at()),
            counts: Some(counts),
            completed_at: Some(completed_at),
        }
    }
    pub(crate) fn status(&self) -> ObservationStatus {
        self.status
    }
    pub(crate) fn observed_at(&self) -> Option<DateTime<Utc>> {
        self.observed_at
    }
    pub(crate) fn counts(&self) -> Option<&NetworkObservationCounts> {
        self.counts.as_ref()
    }
    pub(crate) fn completed_at(&self) -> Option<Instant> {
        self.completed_at
    }
}

#[derive(Clone)]
pub(crate) struct InfrastructureResult {
    node: NodeObservation,
    network: NetworkObservation,
}
impl InfrastructureResult {
    pub(crate) fn node(&self) -> &NodeObservation {
        &self.node
    }
    pub(crate) fn network(&self) -> &NetworkObservation {
        &self.network
    }
    pub(crate) fn coverage(&self) -> &'static str {
        "unverified"
    }
    fn timed_out() -> Self {
        Self {
            node: NodeObservation::from_read(Err(PveReadError::TimedOut)),
            network: NetworkObservation::from_read(Err(PveReadError::TimedOut)),
        }
    }
}
impl Serialize for InfrastructureResult {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut summary = serializer.serialize_struct("InfrastructureResult", 3)?;
        summary.serialize_field("node", &self.node)?;
        summary.serialize_field("network", &self.network)?;
        summary.serialize_field("coverage", self.coverage())?;
        summary.end()
    }
}

pub(crate) struct InfrastructureObservation {
    port: Box<dyn PveInfrastructureVisibilityReadPort>,
    node: NodeName,
}
impl InfrastructureObservation {
    pub(crate) fn new(
        config: &ValidatedObservationConfig,
        token: PveApiToken,
    ) -> Result<Self, ObservationSetupFailure> {
        let node = config.node_target().ok_or(ObservationSetupFailure)?.clone();
        let observer = ReqwestPveObserver::new(
            PveObserverConfig::new(
                config.base_url().clone(),
                PveAccessMode::Observe,
                config.allow_production_reads(),
            )
            .with_api_token(token)
            .with_timeout(Duration::from_secs(2)),
        )
        .map_err(|_| ObservationSetupFailure)?;
        Ok(Self {
            port: Box::new(observer),
            node,
        })
    }
    #[cfg(test)]
    pub(crate) fn from_port(
        node: NodeName,
        port: Box<dyn PveInfrastructureVisibilityReadPort>,
    ) -> Self {
        Self { port, node }
    }
    pub(crate) async fn collect_once(&self) -> InfrastructureResult {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        tokio::time::timeout_at(deadline, async {
            let node = NodeObservation::from_read(
                bounded_read(self.port.node_visibility(&self.node)).await,
            );
            // Timeout polls its inner future first. A delayed repoll must not start
            // another read or return current observations after the pair expired.
            if tokio::time::Instant::now() >= deadline {
                return InfrastructureResult::timed_out();
            }
            let network = NetworkObservation::from_read(
                bounded_read(self.port.network_visibility(&self.node)).await,
            );
            if tokio::time::Instant::now() >= deadline {
                return InfrastructureResult::timed_out();
            }
            InfrastructureResult { node, network }
        })
        .await
        .unwrap_or_else(|_| InfrastructureResult::timed_out())
    }
}

async fn bounded_read<T>(
    read: impl std::future::Future<Output = Result<T, PveReadError>>,
) -> Result<T, PveReadError> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    let result = tokio::time::timeout_at(deadline, read)
        .await
        .unwrap_or(Err(PveReadError::TimedOut));
    if tokio::time::Instant::now() >= deadline {
        Err(PveReadError::TimedOut)
    } else {
        result
    }
}

fn error_status(error: PveReadError) -> ObservationStatus {
    match error {
        PveReadError::TimedOut => ObservationStatus::TimedOut,
        PveReadError::Unauthorized => ObservationStatus::Unauthorized,
        PveReadError::InvalidResponse | PveReadError::UpidNodeMismatch => {
            ObservationStatus::Invalid
        }
        PveReadError::NotFound | PveReadError::Conflict | PveReadError::TransportUnavailable => {
            ObservationStatus::Unavailable
        }
    }
}

#[cfg(test)]
pub(crate) mod test_support {
    use super::*;
    pub(crate) fn node(
        status: ObservationStatus,
        observed_at: Option<DateTime<Utc>>,
        uptime_known: Option<bool>,
        completed_at: Option<Instant>,
    ) -> NodeObservation {
        NodeObservation {
            status,
            observed_at,
            uptime_known,
            completed_at,
        }
    }
    pub(crate) fn network(
        status: ObservationStatus,
        observed_at: Option<DateTime<Utc>>,
        has_counts: bool,
        completed_at: Option<Instant>,
    ) -> NetworkObservation {
        NetworkObservation {
            status,
            observed_at,
            completed_at,
            counts: has_counts.then_some(NetworkObservationCounts {
                linux_bridges: 0,
                ovs_bridges: 0,
                other_interfaces: 0,
                active: 0,
                inactive: 0,
                activity_unknown: 0,
                rejected_rows: usize::from(status == ObservationStatus::Degraded),
            }),
        }
    }
    pub(crate) fn result(
        node: NodeObservation,
        network: NetworkObservation,
    ) -> InfrastructureResult {
        InfrastructureResult { node, network }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{observation_test_config, observation_test_config_with_node};
    use pve_port::{NetworkVisibility, NodeVisibility};
    use serde_json::{Value, json};
    use std::{
        future::Future,
        path::Path,
        pin::Pin,
        sync::{
            Arc, Mutex,
            atomic::{AtomicUsize, Ordering},
        },
        task::Poll,
    };

    fn node() -> NodeName {
        NodeName::parse("private-selected-node").unwrap()
    }
    fn observed_at() -> DateTime<Utc> {
        "2026-09-05T12:34:56Z".parse().unwrap()
    }
    fn node_report(value: Value) -> NodeVisibility {
        NodeVisibility::from_wire(node(), value, observed_at()).unwrap()
    }
    fn network_report(value: Value) -> NetworkVisibility {
        NetworkVisibility::from_wire(node(), value, observed_at() + chrono::Duration::seconds(1))
            .unwrap()
    }

    enum Reply<T> {
        Ready(Result<T, PveReadError>),
        ReadyAfter(Result<T, PveReadError>, Duration),
        Pending,
    }
    struct Guard(Arc<AtomicUsize>);
    impl Drop for Guard {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }
    #[derive(Default)]
    struct Trace {
        calls: Mutex<Vec<(&'static str, String)>>,
        node_dropped: Arc<AtomicUsize>,
        network_dropped: Arc<AtomicUsize>,
        network_started: Mutex<Option<Instant>>,
    }
    struct Port {
        node: Mutex<Option<Reply<NodeVisibility>>>,
        network: Mutex<Option<Reply<NetworkVisibility>>>,
        trace: Arc<Trace>,
    }
    type ReadFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, PveReadError>> + Send + 'a>>;
    async fn reply<T>(value: Reply<T>) -> Result<T, PveReadError> {
        match value {
            Reply::Ready(result) => result,
            Reply::ReadyAfter(result, delay) => {
                tokio::time::sleep(delay).await;
                result
            }
            Reply::Pending => std::future::pending().await,
        }
    }
    impl PveInfrastructureVisibilityReadPort for Port {
        fn node_visibility<'life0, 'life1, 'async_trait>(
            &'life0 self,
            target: &'life1 NodeName,
        ) -> ReadFuture<'async_trait, NodeVisibility>
        where
            'life0: 'async_trait,
            'life1: 'async_trait,
            Self: 'async_trait,
        {
            Box::pin(async move {
                self.trace
                    .calls
                    .lock()
                    .unwrap()
                    .push(("node", target.as_str().into()));
                let _guard = Guard(self.trace.node_dropped.clone());
                let value = self.node.lock().unwrap().take().expect("node called twice");
                reply(value).await
            })
        }
        fn network_visibility<'life0, 'life1, 'async_trait>(
            &'life0 self,
            target: &'life1 NodeName,
        ) -> ReadFuture<'async_trait, NetworkVisibility>
        where
            'life0: 'async_trait,
            'life1: 'async_trait,
            Self: 'async_trait,
        {
            Box::pin(async move {
                *self.trace.network_started.lock().unwrap() = Some(Instant::now());
                self.trace
                    .calls
                    .lock()
                    .unwrap()
                    .push(("network", target.as_str().into()));
                let _guard = Guard(self.trace.network_dropped.clone());
                let value = self
                    .network
                    .lock()
                    .unwrap()
                    .take()
                    .expect("network called twice");
                reply(value).await
            })
        }
    }
    fn collector(
        node_reply: Reply<NodeVisibility>,
        network_reply: Reply<NetworkVisibility>,
    ) -> (InfrastructureObservation, Arc<Trace>) {
        let trace = Arc::new(Trace::default());
        let port = Port {
            node: Mutex::new(Some(node_reply)),
            network: Mutex::new(Some(network_reply)),
            trace: trace.clone(),
        };
        (
            InfrastructureObservation::from_port(node(), Box::new(port)),
            trace,
        )
    }
    fn success() -> (Reply<NodeVisibility>, Reply<NetworkVisibility>) {
        (
            Reply::Ready(Ok(node_report(json!({"uptime":0})))),
            Reply::Ready(Ok(network_report(json!([])))),
        )
    }
    fn assert_calls(trace: &Trace, methods: &[&str]) {
        let expected: Vec<_> = methods
            .iter()
            .map(|method| (*method, "private-selected-node".to_owned()))
            .collect();
        assert_eq!(*trace.calls.lock().unwrap(), expected);
    }
    fn assert_node_failure(result: &NodeObservation, status: ObservationStatus) {
        assert_eq!(result.status(), status);
        assert!(result.observed_at().is_none());
        assert!(result.uptime_known().is_none());
        assert!(result.completed_at().is_none());
    }
    fn assert_network_failure(result: &NetworkObservation, status: ObservationStatus) {
        assert_eq!(result.status(), status);
        assert!(result.observed_at().is_none());
        assert!(result.counts().is_none());
        assert!(result.completed_at().is_none());
    }

    // Break: constructor silently selects a node or ignores validated selection.
    #[test]
    fn constructor_requires_explicit_selection() {
        let path = Path::new("synthetic-unopened-token");
        let token = || PveApiToken::parse("observer@pve!fixture", "synthetic-secret").unwrap();
        assert!(InfrastructureObservation::new(&observation_test_config(path), token()).is_err());
        let config = observation_test_config_with_node(path, "http://127.0.0.1:5000", "pve-test");
        assert!(InfrastructureObservation::new(&config, token()).is_ok());
    }

    // Break: production construction loses the selected node, authentication, or narrow routes.
    #[tokio::test]
    async fn production_constructor_uses_only_authenticated_selected_node_gets() {
        use tokio::{
            io::{AsyncReadExt, AsyncWriteExt},
            net::TcpListener,
        };
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let config = observation_test_config_with_node(
            Path::new("synthetic-unopened-token"),
            &format!("http://{}", listener.local_addr().unwrap()),
            "pve-test",
        );
        let collector = InfrastructureObservation::new(
            &config,
            PveApiToken::parse("observer@pve!fixture", "synthetic-secret").unwrap(),
        )
        .unwrap();
        let server = async {
            for (route, body) in [
                ("status", r#"{"data":{"uptime":0}}"#),
                (
                    "network",
                    r#"{"data":[{"iface":"private-ovs","type":"OVSBridge","active":1}]}"#,
                ),
            ] {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut request = Vec::new();
                while !request.windows(4).any(|part| part == b"\r\n\r\n") {
                    let mut chunk = [0; 1024];
                    let count = stream.read(&mut chunk).await.unwrap();
                    assert_ne!(count, 0);
                    request.extend_from_slice(&chunk[..count]);
                    assert!(request.len() <= 16_384);
                }
                let request = String::from_utf8(request).unwrap();
                assert!(request.starts_with(&format!(
                    "GET /api2/json/nodes/pve-test/{route} HTTP/1.1\r\n"
                )));
                assert!(request.contains(
                    "authorization: PVEAPIToken=observer@pve!fixture=synthetic-secret\r\n"
                ));
                stream.write_all(format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).as_bytes()).await.unwrap();
            }
        };
        // No background fixture task: failure or timeout drops both futures and owned sockets.
        let (result, ()) = tokio::time::timeout(Duration::from_secs(3), async {
            tokio::join!(collector.collect_once(), server)
        })
        .await
        .expect("owned loopback pair exceeded budget");
        assert_eq!(result.node().status(), ObservationStatus::Fresh);
        assert_eq!(result.node().uptime_known(), Some(true));
        assert_eq!(result.network().status(), ObservationStatus::Fresh);
        assert_eq!(
            serde_json::to_value(result.network().counts()).unwrap()["ovs_bridges"],
            1
        );
    }

    // Break: wrong node/order, metadata leakage, or completion stamped after the pair.
    #[tokio::test]
    async fn successful_pair_sanitizes_counts_and_captures_each_completion() {
        let (collector, trace) = collector(
            Reply::Ready(Ok(node_report(
                json!({"node":"private-selected-node","uptime":987654321,"secret":"node-secret-canary"}),
            ))),
            Reply::Ready(Ok(network_report(json!([
                {"iface":"private-linux","type":"bridge","active":1,"address":"address-canary","secret":"network-secret-canary"},
                {"iface":"private-ovs","type":"OVSBridge","active":0},
                {"iface":"private-other","type":"unknown-type-canary"}
            ])))),
        );
        let before = Instant::now();
        let result = collector.collect_once().await;
        assert_calls(&trace, &["node", "network"]);
        let between = trace.network_started.lock().unwrap().unwrap();
        assert!(result.node().completed_at().unwrap() >= before);
        assert!(result.node().completed_at().unwrap() <= between);
        assert!(result.network().completed_at().unwrap() >= between);
        assert!(result.network().completed_at().unwrap() <= Instant::now());
        assert_eq!(result.node().observed_at(), Some(observed_at()));
        assert_eq!(
            result.network().observed_at(),
            Some(observed_at() + chrono::Duration::seconds(1))
        );
        assert_eq!(
            serde_json::to_value(&result).unwrap(),
            json!({
                "node":{"status":"fresh","observed_at":observed_at(),"uptime_known":true},
                "network":{"status":"fresh","observed_at":observed_at()+chrono::Duration::seconds(1),"counts":{
                    "linux_bridges":1,"ovs_bridges":1,"other_interfaces":1,"active":1,"inactive":1,"activity_unknown":1,"rejected_rows":0}},
                "coverage":"unverified"
            })
        );
        let encoded = serde_json::to_string(&result).unwrap();
        for canary in [
            "private-",
            "987654321",
            "canary",
            "completed_at",
            "synthetic-secret",
        ] {
            assert!(!encoded.contains(canary));
        }
    }

    // Break: zero uptime or missing uptime is mistaken for a failed node observation.
    #[tokio::test]
    async fn zero_and_missing_uptime_are_fresh_with_distinct_knowledge() {
        for (value, known) in [(json!({"uptime":0}), true), (json!({}), false)] {
            let (_, network) = success();
            let (collector, trace) = collector(Reply::Ready(Ok(node_report(value))), network);
            let result = collector.collect_once().await;
            assert_eq!(result.node().status(), ObservationStatus::Fresh);
            assert_eq!(result.node().uptime_known(), Some(known));
            assert_eq!(result.network().status(), ObservationStatus::Fresh);
            assert_calls(&trace, &["node", "network"]);
        }
    }

    // Break: rejected rows remain fresh, or unknown metadata/empty reports degrade.
    #[tokio::test]
    async fn network_degrades_only_for_rejected_rows() {
        for (rows, status, rejected, other, unknown) in [
            (json!([]), ObservationStatus::Fresh, 0, 0, 0),
            (
                json!([{"iface":"future0","type":"future"}]),
                ObservationStatus::Fresh,
                0,
                1,
                1,
            ),
            (
                json!([{}, {"iface":"future0","type":"future"}]),
                ObservationStatus::Degraded,
                1,
                1,
                1,
            ),
            (json!([{}]), ObservationStatus::Degraded, 1, 0, 0),
        ] {
            let (node, _) = success();
            let (collector, _) = collector(node, Reply::Ready(Ok(network_report(rows))));
            let result = collector.collect_once().await;
            assert_eq!(result.network().status(), status);
            assert!(result.network().observed_at().is_some());
            assert!(result.network().completed_at().is_some());
            assert_eq!(
                serde_json::to_value(result.network().counts()).unwrap(),
                json!({
                    "linux_bridges":0,"ovs_bridges":0,"other_interfaces":other,"active":0,"inactive":0,"activity_unknown":unknown,"rejected_rows":rejected
                })
            );
        }
    }

    // Break: a failed component contaminates its peer, retries, or leaks stale fields.
    #[tokio::test]
    async fn each_error_is_fixed_and_independent_of_the_other_component() {
        for (error, status) in [
            (PveReadError::TimedOut, ObservationStatus::TimedOut),
            (PveReadError::Unauthorized, ObservationStatus::Unauthorized),
            (PveReadError::InvalidResponse, ObservationStatus::Invalid),
            (PveReadError::UpidNodeMismatch, ObservationStatus::Invalid),
            (PveReadError::NotFound, ObservationStatus::Unavailable),
            (PveReadError::Conflict, ObservationStatus::Unavailable),
            (
                PveReadError::TransportUnavailable,
                ObservationStatus::Unavailable,
            ),
        ] {
            for fail_node in [true, false] {
                let (mut node, mut network) = success();
                if fail_node {
                    node = Reply::Ready(Err(error.clone()));
                } else {
                    network = Reply::Ready(Err(error.clone()));
                }
                let (collector, trace) = collector(node, network);
                let result = collector.collect_once().await;
                if fail_node {
                    assert_node_failure(result.node(), status);
                    assert_eq!(result.network().status(), ObservationStatus::Fresh);
                } else {
                    assert_eq!(result.node().status(), ObservationStatus::Fresh);
                    assert_network_failure(result.network(), status);
                }
                assert_calls(&trace, &["node", "network"]);
            }
        }
    }

    // Break: port calls can stall indefinitely or node timeout suppresses network.
    #[tokio::test(start_paused = true)]
    async fn each_pending_call_is_dropped_at_two_seconds() {
        for pending_node in [true, false] {
            let (mut node, mut network) = success();
            if pending_node {
                node = Reply::Pending;
            } else {
                network = Reply::Pending;
            }
            let (collector, trace) = collector(node, network);
            let start = tokio::time::Instant::now();
            let result = collector.collect_once().await;
            assert_eq!(start.elapsed(), Duration::from_secs(2));
            if pending_node {
                assert_node_failure(result.node(), ObservationStatus::TimedOut);
                assert_eq!(result.network().status(), ObservationStatus::Fresh);
            } else {
                assert_eq!(result.node().status(), ObservationStatus::Fresh);
                assert_network_failure(result.network(), ObservationStatus::TimedOut);
            }
            assert_calls(&trace, &["node", "network"]);
            assert_eq!(trace.node_dropped.load(Ordering::SeqCst), 1);
            assert_eq!(trace.network_dropped.load(Ordering::SeqCst), 1);
        }
    }

    #[tokio::test(start_paused = true)]
    async fn two_pending_reads_finish_in_four_seconds_without_retry() {
        let (collector, trace) = collector(Reply::Pending, Reply::Pending);
        let start = tokio::time::Instant::now();
        let result = collector.collect_once().await;
        assert_eq!(start.elapsed(), Duration::from_secs(4));
        assert_node_failure(result.node(), ObservationStatus::TimedOut);
        assert_network_failure(result.network(), ObservationStatus::TimedOut);
        assert_calls(&trace, &["node", "network"]);
    }

    // Break: a ready inner future wins timeout polling despite an expired call budget.
    #[tokio::test(start_paused = true)]
    async fn delayed_repoll_respects_each_call_deadline() {
        for delay_node in [true, false] {
            for (elapsed, expected) in [
                (Duration::from_millis(1999), ObservationStatus::Fresh),
                (Duration::from_secs(2), ObservationStatus::TimedOut),
                (Duration::from_secs(3), ObservationStatus::TimedOut),
            ] {
                let (mut node, mut network) = success();
                if delay_node {
                    node = Reply::ReadyAfter(
                        Ok(node_report(json!({"uptime":0}))),
                        Duration::from_secs(1),
                    );
                } else {
                    network =
                        Reply::ReadyAfter(Ok(network_report(json!([]))), Duration::from_secs(1));
                }
                let (collector, trace) = collector(node, network);
                let mut future = Box::pin(collector.collect_once());
                std::future::poll_fn(|cx| {
                    assert!(future.as_mut().poll(cx).is_pending());
                    Poll::Ready(())
                })
                .await;
                tokio::time::advance(elapsed).await;
                let result = future.await;
                if delay_node {
                    if expected == ObservationStatus::TimedOut {
                        assert_node_failure(result.node(), expected);
                    } else {
                        assert_eq!(result.node().status(), expected);
                    }
                    assert_eq!(result.network().status(), ObservationStatus::Fresh);
                } else {
                    assert_eq!(result.node().status(), ObservationStatus::Fresh);
                    if expected == ObservationStatus::TimedOut {
                        assert_network_failure(result.network(), expected);
                    } else {
                        assert_eq!(result.network().status(), expected);
                    }
                }
                assert_calls(&trace, &["node", "network"]);
            }
        }
    }

    // Break: cancellation detaches work or starts the second read after the first is dropped.
    #[tokio::test(start_paused = true)]
    async fn cancellation_drops_active_read_and_starts_no_later_read() {
        for during_node in [true, false] {
            let (ready_node, _) = success();
            let (collector, trace) = collector(
                if during_node {
                    Reply::Pending
                } else {
                    ready_node
                },
                Reply::Pending,
            );
            assert!(
                tokio::time::timeout(Duration::from_secs(1), collector.collect_once())
                    .await
                    .is_err()
            );
            tokio::time::advance(Duration::from_secs(10)).await;
            assert_calls(
                &trace,
                if during_node {
                    &["node"]
                } else {
                    &["node", "network"]
                },
            );
            assert_eq!(trace.node_dropped.load(Ordering::SeqCst), 1);
            assert_eq!(
                trace.network_dropped.load(Ordering::SeqCst),
                usize::from(!during_node)
            );
        }
    }

    // Break: delayed scheduling beyond the total budget still starts an unstarted read.
    #[tokio::test(start_paused = true)]
    async fn expired_whole_deadline_cancels_without_starting_network() {
        for elapsed in [Duration::from_secs(5), Duration::from_secs(6)] {
            let (_, network) = success();
            let (collector, trace) = collector(Reply::Pending, network);
            let mut future = Box::pin(collector.collect_once());
            std::future::poll_fn(|cx| {
                assert!(future.as_mut().poll(cx).is_pending());
                Poll::Ready(())
            })
            .await;
            tokio::time::advance(elapsed).await;
            let result = future.await;
            assert_node_failure(result.node(), ObservationStatus::TimedOut);
            assert_network_failure(result.network(), ObservationStatus::TimedOut);
            assert_calls(&trace, &["node"]);
            assert_eq!(trace.node_dropped.load(Ordering::SeqCst), 1);
            assert_eq!(trace.network_dropped.load(Ordering::SeqCst), 0);
        }
    }

    #[tokio::test(start_paused = true)]
    async fn expired_whole_deadline_discards_an_earlier_success() {
        for elapsed in [Duration::from_secs(5), Duration::from_secs(6)] {
            let (node, _) = success();
            let (collector, trace) = collector(node, Reply::Pending);
            let mut future = Box::pin(collector.collect_once());
            std::future::poll_fn(|cx| {
                assert!(future.as_mut().poll(cx).is_pending());
                Poll::Ready(())
            })
            .await;
            tokio::time::advance(elapsed).await;
            let result = future.await;
            assert_node_failure(result.node(), ObservationStatus::TimedOut);
            assert_network_failure(result.network(), ObservationStatus::TimedOut);
            assert_calls(&trace, &["node", "network"]);
            assert_eq!(trace.network_dropped.load(Ordering::SeqCst), 1);
        }
    }
}
