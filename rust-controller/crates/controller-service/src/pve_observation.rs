use crate::config::ValidatedObservationConfig;
use chrono::{DateTime, Utc};
use pve_port::{
    GuestKind, PveAccessMode, PveApiToken, PveObserverConfig, PveReadError, PveVisibilityReadPort,
    ReqwestPveObserver,
};
use serde::{Serialize, Serializer, ser::SerializeStruct};
use std::time::Duration;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ObservationStatus {
    Fresh,
    Degraded,
    Unauthorized,
    Unavailable,
    Invalid,
    TimedOut,
}

#[derive(Clone, Serialize)]
pub(crate) struct ObservationCounts {
    visible_qemu: usize,
    visible_lxc: usize,
    visible_unsupported: usize,
    rejected_rows: usize,
}

#[derive(Clone)]
pub(crate) struct ObservationResult {
    status: ObservationStatus,
    observed_at: Option<DateTime<Utc>>,
    counts: Option<ObservationCounts>,
}

impl ObservationResult {
    pub(crate) fn status(&self) -> ObservationStatus {
        self.status
    }
    pub(crate) fn observed_at(&self) -> Option<DateTime<Utc>> {
        self.observed_at
    }
    pub(crate) fn counts(&self) -> Option<&ObservationCounts> {
        self.counts.as_ref()
    }
    pub(crate) fn coverage(&self) -> &'static str {
        "unverified"
    }
}

impl Serialize for ObservationResult {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut summary = serializer.serialize_struct("ObservationResult", 4)?;
        summary.serialize_field("status", &self.status)?;
        summary.serialize_field("observed_at", &self.observed_at)?;
        summary.serialize_field("counts", &self.counts)?;
        summary.serialize_field("coverage", self.coverage())?;
        summary.end()
    }
}

#[derive(Debug)]
pub(crate) struct ObservationSetupFailure;

pub(crate) struct PveObservation {
    port: Box<dyn PveVisibilityReadPort>,
}
impl PveObservation {
    pub(crate) fn new(
        config: &ValidatedObservationConfig,
        token: PveApiToken,
    ) -> Result<Self, ObservationSetupFailure> {
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
        })
    }
    #[cfg(test)]
    pub(crate) fn from_port(port: Box<dyn PveVisibilityReadPort>) -> Self {
        Self { port }
    }
    pub(crate) async fn collect_once(&self) -> ObservationResult {
        let report =
            match tokio::time::timeout(Duration::from_secs(3), self.port.cluster_visibility())
                .await
                .unwrap_or(Err(PveReadError::TimedOut))
            {
                Ok(report) => report,
                Err(error) => {
                    let status = match error {
                        PveReadError::TimedOut => ObservationStatus::TimedOut,
                        PveReadError::Unauthorized => ObservationStatus::Unauthorized,
                        PveReadError::InvalidResponse | PveReadError::UpidNodeMismatch => {
                            ObservationStatus::Invalid
                        }
                        PveReadError::NotFound
                        | PveReadError::Conflict
                        | PveReadError::TransportUnavailable => ObservationStatus::Unavailable,
                    };
                    return ObservationResult {
                        status,
                        observed_at: None,
                        counts: None,
                    };
                }
            };
        let mut counts = ObservationCounts {
            visible_qemu: 0,
            visible_lxc: 0,
            visible_unsupported: 0,
            rejected_rows: report.rejected_rows(),
        };
        for guest in report.records() {
            match guest.kind() {
                GuestKind::Qemu => counts.visible_qemu += 1,
                GuestKind::Lxc => counts.visible_lxc += 1,
                GuestKind::Unsupported => counts.visible_unsupported += 1,
            }
        }
        ObservationResult {
            status: if counts.rejected_rows == 0 {
                ObservationStatus::Fresh
            } else {
                ObservationStatus::Degraded
            },
            observed_at: Some(report.observed_at()),
            counts: Some(counts),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::observation_test_config_at;
    use pve_port::{ClusterVisibility, PveReadError};
    use serde_json::json;
    use std::{
        future::Future,
        path::Path,
        pin::Pin,
        sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        },
        time::Duration,
    };
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
        task::JoinHandle,
    };

    struct Fixture {
        observation: PveObservation,
        task: Option<JoinHandle<()>>,
        requests: Arc<AtomicUsize>,
    }
    impl Fixture {
        async fn new(status: u16, body: String, delay: Duration) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let config = observation_test_config_at(
                Path::new("synthetic-unused-token-path"),
                &format!("http://{}", listener.local_addr().unwrap()),
            );
            let requests = Arc::new(AtomicUsize::new(0));
            let captured = requests.clone();
            let task = tokio::spawn(async move {
                loop {
                    let (mut stream, _) = listener.accept().await.unwrap();
                    let mut request = Vec::new();
                    while !request.windows(4).any(|v| v == b"\r\n\r\n") {
                        let mut chunk = [0; 1024];
                        let n = stream.read(&mut chunk).await.unwrap();
                        if n == 0 {
                            return;
                        }
                        request.extend_from_slice(&chunk[..n]);
                        assert!(request.len() < 16_384);
                    }
                    let request = String::from_utf8(request).unwrap();
                    assert!(
                        request
                            .starts_with("GET /api2/json/cluster/resources?type=vm HTTP/1.1\r\n")
                    );
                    assert!(request.contains(
                        "authorization: PVEAPIToken=observer@pve!fixture=synthetic-secret\r\n"
                    ));
                    captured.fetch_add(1, Ordering::SeqCst);
                    tokio::time::sleep(delay).await;
                    let response = format!(
                        "HTTP/1.1 {status} Fixture\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = stream.write_all(response.as_bytes()).await;
                }
            });
            let observation = PveObservation::new(
                &config,
                PveApiToken::parse("observer@pve!fixture", "synthetic-secret").unwrap(),
            )
            .unwrap();
            Self {
                observation,
                task: Some(task),
                requests,
            }
        }
        async fn finish(mut self) {
            assert_eq!(self.requests.load(Ordering::SeqCst), 1);
            let task = self.task.take().unwrap();
            task.abort();
            let result = task.await;
            assert!(result.is_ok() || result.unwrap_err().is_cancelled());
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            if let Some(task) = &self.task {
                task.abort();
            }
        }
    }

    #[tokio::test]
    async fn summaries_serialize_only_classified_counts_and_unverified_coverage() {
        for (data, status, status_label, counts) in [
            (
                json!([]),
                ObservationStatus::Fresh,
                "fresh",
                json!({"visible_qemu":0,"visible_lxc":0,"visible_unsupported":0,"rejected_rows":0}),
            ),
            (
                json!([{"vmid":101,"node":"private-node","type":"qemu","name":"private-name"},{"vmid":102,"node":"private-node","type":"lxc"},{"vmid":103,"node":"private-node","type":"future"}]),
                ObservationStatus::Fresh,
                "fresh",
                json!({"visible_qemu":1,"visible_lxc":1,"visible_unsupported":1,"rejected_rows":0}),
            ),
            (
                json!([{}, {"vmid":101,"node":"private-node","type":"qemu"}]),
                ObservationStatus::Degraded,
                "degraded",
                json!({"visible_qemu":1,"visible_lxc":0,"visible_unsupported":0,"rejected_rows":1}),
            ),
            (
                json!([{}]),
                ObservationStatus::Degraded,
                "degraded",
                json!({"visible_qemu":0,"visible_lxc":0,"visible_unsupported":0,"rejected_rows":1}),
            ),
        ] {
            let fixture = Fixture::new(200, json!({"data":data}).to_string(), Duration::ZERO).await;
            let before = Utc::now();
            let result = fixture.observation.collect_once().await;
            assert_eq!(result.status(), status);
            let timestamp = result.observed_at().unwrap();
            assert!(timestamp >= before && timestamp <= Utc::now());
            assert!(result.counts().is_some());
            assert_eq!(result.coverage(), "unverified");
            assert_eq!(
                serde_json::to_value(&result).unwrap(),
                json!({"status":status_label,"observed_at":timestamp,"counts":counts,"coverage":"unverified"})
            );
            fixture.finish().await;
        }
    }

    #[tokio::test]
    async fn errors_have_fixed_status_with_no_timestamp_or_counts() {
        for (code, body, expected) in [
            (401, "sensitive body", "unauthorized"),
            (403, "sensitive body", "unauthorized"),
            (404, "sensitive body", "unavailable"),
            (409, "sensitive body", "unavailable"),
            (500, "sensitive body", "unavailable"),
            (200, "bad-json", "invalid"),
            (200, r#"{"data":{}}"#, "invalid"),
        ] {
            let fixture = Fixture::new(code, body.into(), Duration::ZERO).await;
            let result = fixture.observation.collect_once().await;
            assert!(result.observed_at().is_none());
            assert!(result.counts().is_none());
            assert_eq!(
                serde_json::to_value(&result).unwrap(),
                json!({"status":expected,"observed_at":null,"counts":null,"coverage":"unverified"})
            );
            fixture.finish().await;
        }
    }

    #[tokio::test]
    async fn production_constructor_enforces_two_second_request_timeout() {
        let fixture = Fixture::new(200, r#"{"data":[]}"#.into(), Duration::from_secs(10)).await;
        let start = std::time::Instant::now();
        let result = fixture.observation.collect_once().await;
        assert_eq!(result.status(), ObservationStatus::TimedOut);
        assert!(
            start.elapsed() >= Duration::from_millis(1800)
                && start.elapsed() < Duration::from_secs(3)
        );
        assert_eq!(
            serde_json::to_value(&result).unwrap(),
            json!({"status":"timed_out","observed_at":null,"counts":null,"coverage":"unverified"})
        );
        fixture.finish().await;
    }

    struct PendingPort {
        dropped: Arc<AtomicUsize>,
    }
    impl PveVisibilityReadPort for PendingPort {
        fn cluster_visibility<'life0, 'async_trait>(
            &'life0 self,
        ) -> Pin<
            Box<dyn Future<Output = Result<ClusterVisibility, PveReadError>> + Send + 'async_trait>,
        >
        where
            'life0: 'async_trait,
            Self: 'async_trait,
        {
            struct Guard(Arc<AtomicUsize>);
            impl Drop for Guard {
                fn drop(&mut self) {
                    self.0.fetch_add(1, Ordering::SeqCst);
                }
            }
            Box::pin(async move {
                let _guard = Guard(self.dropped.clone());
                std::future::pending().await
            })
        }
    }

    #[tokio::test]
    async fn collection_deadline_drops_pending_read_at_three_seconds() {
        let dropped = Arc::new(AtomicUsize::new(0));
        let observation = PveObservation::from_port(Box::new(PendingPort {
            dropped: dropped.clone(),
        }));
        let start = std::time::Instant::now();
        let result = tokio::time::timeout(Duration::from_secs(4), observation.collect_once())
            .await
            .expect("collection exceeded budget");
        assert_eq!(result.status(), ObservationStatus::TimedOut);
        assert!(
            start.elapsed() >= Duration::from_millis(2800)
                && start.elapsed() < Duration::from_secs(4)
        );
        assert_eq!(dropped.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn caller_cancellation_drops_the_only_inflight_read() {
        let dropped = Arc::new(AtomicUsize::new(0));
        let observation = PveObservation::from_port(Box::new(PendingPort {
            dropped: dropped.clone(),
        }));
        assert!(
            tokio::time::timeout(Duration::from_millis(20), observation.collect_once())
                .await
                .is_err()
        );
        assert_eq!(dropped.load(Ordering::SeqCst), 1);
    }
}
