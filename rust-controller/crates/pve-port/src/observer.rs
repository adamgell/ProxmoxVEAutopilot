use std::{
    collections::BTreeMap,
    fmt,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use async_trait::async_trait;
use chrono::Utc;
use reqwest::{Client, Method, StatusCode};
use serde::Deserialize;
use thiserror::Error;

use crate::{
    BridgeInventory, ClusterVmInventory, MacAddress, NativeVmConfig, NodeName, NodeStatus,
    PveApiToken, PveBaseUrl, PvePreflightReadPort, PveReadError, PveReadPort, QgaStatus,
    StorageName, StorageStatus, TaskState, TaskStatus, Upid, VmConfig, VmPowerStatus, VmUuid, Vmid,
    Volume,
};

#[async_trait]
impl crate::PveInfrastructureVisibilityReadPort for ReqwestPveObserver {
    async fn node_visibility(
        &self,
        node: &NodeName,
    ) -> Result<crate::NodeVisibility, PveReadError> {
        let (data, observed_at) = self.infrastructure_data(node, "status").await?;
        crate::NodeVisibility::from_wire(node.clone(), data, observed_at)
    }

    async fn network_visibility(
        &self,
        node: &NodeName,
    ) -> Result<crate::NetworkVisibility, PveReadError> {
        let (data, observed_at) = self.infrastructure_data(node, "network").await?;
        crate::NetworkVisibility::from_wire(node.clone(), data, observed_at)
    }
}

#[async_trait]
impl crate::PveVisibilityReadPort for ReqwestPveObserver {
    async fn cluster_visibility(&self) -> Result<crate::ClusterVisibility, PveReadError> {
        let mut url = self
            .base_url
            .endpoint(&["api2", "json", "cluster", "resources"]);
        url.query_pairs_mut().append_pair("type", "vm");
        self.request_audit.record_request();
        let mut request = self.client.get(url);
        if let Some(token) = &self.token {
            request = request.header(reqwest::header::AUTHORIZATION, token.header());
        }
        let response = request.send().await.map_err(map_reqwest_error)?;
        match response.status() {
            StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => {
                return Err(PveReadError::Unauthorized);
            }
            StatusCode::NOT_FOUND => return Err(PveReadError::NotFound),
            StatusCode::CONFLICT => return Err(PveReadError::Conflict),
            status if !status.is_success() => return Err(PveReadError::TransportUnavailable),
            _ => {}
        }
        let body = bounded_visibility_body(response).await?;
        let observed_at = Utc::now();
        let envelope: PveEnvelope<serde_json::Value> =
            serde_json::from_slice(&body).map_err(|_| PveReadError::InvalidResponse)?;
        crate::ClusterVisibility::from_wire(envelope.data, observed_at)
    }
}

async fn bounded_visibility_body(mut response: reqwest::Response) -> Result<Vec<u8>, PveReadError> {
    const MAX_BYTES: usize = 1_048_576;
    if response
        .content_length()
        .is_some_and(|length| length > MAX_BYTES as u64)
    {
        return Err(PveReadError::InvalidResponse);
    }
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(map_reqwest_error)? {
        if chunk.len() > MAX_BYTES - body.len() {
            return Err(PveReadError::InvalidResponse);
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PveAccessMode {
    Observe,
    Adapter,
    Native,
}

#[derive(Clone, Debug, Default)]
pub struct PveRequestAudit(Arc<AtomicUsize>);

impl PveRequestAudit {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    #[must_use]
    pub fn request_count(&self) -> usize {
        self.0.load(Ordering::SeqCst)
    }

    fn record_request(&self) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}

#[derive(Clone, Debug)]
pub struct PveObserverConfig {
    token: Option<PveApiToken>,
    base_url: PveBaseUrl,
    mode: PveAccessMode,
    allow_production_reads: bool,
    timeout: Duration,
    request_audit: PveRequestAudit,
}

impl PveObserverConfig {
    #[must_use]
    pub fn new(base_url: PveBaseUrl, mode: PveAccessMode, allow_production_reads: bool) -> Self {
        Self {
            token: None,
            base_url,
            mode,
            allow_production_reads,
            timeout: Duration::from_secs(10),
            request_audit: PveRequestAudit::new(),
        }
    }

    #[must_use]
    pub const fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    #[must_use]
    pub fn with_api_token(mut self, token: PveApiToken) -> Self {
        self.token = Some(token);
        self
    }

    #[must_use]
    pub fn with_request_audit(mut self, request_audit: PveRequestAudit) -> Self {
        self.request_audit = request_audit;
        self
    }

    fn permits_target(&self) -> bool {
        self.base_url.is_loopback()
            || (self.mode == PveAccessMode::Observe && self.allow_production_reads)
    }
}

/// Once narrowed to visibility, the service cannot call QGA or mutations.
/// ```compile_fail,E0599
/// use pve_port::{PveVisibilityReadPort, PveReadPort, NodeName, Vmid};
/// async fn no_qga(port: &dyn PveVisibilityReadPort, node: &NodeName, vmid: Vmid) {
///     port.qga_ping(node, vmid).await;
/// }
/// ```
/// ```compile_fail,E0599
/// use pve_port::{PveVisibilityReadPort, PveMutationPort, CloneRequest};
/// async fn no_clone(port: &dyn PveVisibilityReadPort, request: &CloneRequest) {
///     port.clone_vm(request).await;
/// }
/// ```
/// ```compile_fail,E0599
/// use pve_port::{PveVisibilityReadPort, PveMutationPort, ConfigureRequest};
/// async fn no_configure(port: &dyn PveVisibilityReadPort, request: &ConfigureRequest) {
///     port.configure_vm(request).await;
/// }
/// ```
/// ```compile_fail,E0599
/// use pve_port::{PveVisibilityReadPort, PveMutationPort, StartRequest};
/// async fn no_start(port: &dyn PveVisibilityReadPort, request: &StartRequest) {
///     port.start_vm(request).await;
/// }
/// ```
#[derive(Clone)]
pub struct ReqwestPveObserver {
    token: Option<PveApiToken>,
    base_url: PveBaseUrl,
    client: Client,
    request_audit: PveRequestAudit,
}

impl fmt::Debug for ReqwestPveObserver {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("ReqwestPveObserver { verified_rustls: true }")
    }
}

impl ReqwestPveObserver {
    pub fn new(config: PveObserverConfig) -> Result<Self, PveObserverBuildError> {
        Self::new_with_client_builder(config, RustlsClientBuilder)
    }

    fn new_with_client_builder<B>(
        config: PveObserverConfig,
        builder: B,
    ) -> Result<Self, PveObserverBuildError>
    where
        B: VerifiedClientBuilder,
    {
        if !config.permits_target() {
            return Err(PveObserverBuildError::TargetReadDenied);
        }
        let client = builder.build(config.timeout)?;
        Ok(Self {
            token: config.token,
            base_url: config.base_url,
            client,
            request_audit: config.request_audit,
        })
    }

    async fn infrastructure_data(
        &self,
        node: &NodeName,
        leaf: &'static str,
    ) -> Result<(serde_json::Value, chrono::DateTime<Utc>), PveReadError> {
        // Keep the entire request/body/envelope collection bounded even when the
        // observer's client was configured with a longer timeout.
        tokio::time::timeout(Duration::from_secs(2), async {
            let url = self
                .base_url
                .endpoint(&["api2", "json", "nodes", node.as_str(), leaf]);
            self.request_audit.record_request();
            let mut request = self.client.get(url);
            if let Some(token) = &self.token {
                request = request.header(reqwest::header::AUTHORIZATION, token.header());
            }
            let response = request.send().await.map_err(map_reqwest_error)?;
            match response.status() {
                StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => {
                    return Err(PveReadError::Unauthorized);
                }
                StatusCode::NOT_FOUND => return Err(PveReadError::NotFound),
                StatusCode::CONFLICT => return Err(PveReadError::Conflict),
                status if !status.is_success() => return Err(PveReadError::TransportUnavailable),
                _ => {}
            }
            let body = bounded_visibility_body(response).await?;
            let observed_at = Utc::now();
            let envelope: PveEnvelope<serde_json::Value> =
                serde_json::from_slice(&body).map_err(|_| PveReadError::InvalidResponse)?;
            Ok((envelope.data, observed_at))
        })
        .await
        .unwrap_or(Err(PveReadError::TimedOut))
    }

    async fn get_data<T>(&self, path: &[&str]) -> Result<T, PveReadError>
    where
        T: for<'de> Deserialize<'de>,
    {
        self.request_data(Method::GET, self.base_url.endpoint(path))
            .await
    }

    async fn request_data<T>(&self, method: Method, url: reqwest::Url) -> Result<T, PveReadError>
    where
        T: for<'de> Deserialize<'de>,
    {
        self.request_audit.record_request();
        let mut request = self.client.request(method, url);
        if let Some(token) = &self.token {
            request = request.header(reqwest::header::AUTHORIZATION, token.header());
        }
        let response = request.send().await.map_err(map_reqwest_error)?;
        match response.status() {
            StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => {
                return Err(PveReadError::Unauthorized);
            }
            StatusCode::NOT_FOUND => return Err(PveReadError::NotFound),
            StatusCode::CONFLICT => return Err(PveReadError::Conflict),
            status if !status.is_success() => return Err(PveReadError::TransportUnavailable),
            _ => {}
        }
        response
            .json::<PveEnvelope<T>>()
            .await
            .map(|envelope| envelope.data)
            .map_err(map_reqwest_error)
    }
}

trait VerifiedClientBuilder {
    fn build(self, timeout: Duration) -> Result<Client, PveObserverBuildError>;
}

struct RustlsClientBuilder;

impl VerifiedClientBuilder for RustlsClientBuilder {
    fn build(self, timeout: Duration) -> Result<Client, PveObserverBuildError> {
        build_verified_client_from(Client::builder(), timeout)
    }
}

fn build_verified_client_from(
    builder: reqwest::ClientBuilder,
    timeout: Duration,
) -> Result<Client, PveObserverBuildError> {
    builder
        .timeout(timeout)
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .build()
        .map_err(|_| PveObserverBuildError::HttpClient)
}

#[cfg(test)]
pub(crate) fn build_verified_test_client_from(
    builder: reqwest::ClientBuilder,
    timeout: Duration,
) -> Result<Client, PveObserverBuildError> {
    build_verified_client_from(builder, timeout)
}

#[derive(Clone, Copy, Debug, Error, Eq, PartialEq)]
pub enum PveObserverBuildError {
    #[error("non-loopback PVE reads require observe mode and explicit read permission")]
    TargetReadDenied,
    #[error("failed to construct certificate-verifying rustls HTTP client")]
    HttpClient,
}

#[derive(Deserialize)]
struct PveEnvelope<T> {
    data: T,
}

#[derive(Deserialize)]
struct TaskStatusWire {
    status: String,
    exitstatus: Option<String>,
}

#[derive(Deserialize)]
struct VmConfigWire {
    smbios1: Option<String>,
    #[serde(flatten)]
    values: BTreeMap<String, serde_json::Value>,
}

#[derive(Deserialize)]
struct VolumeWire {
    volid: String,
}

fn map_reqwest_error(error: reqwest::Error) -> PveReadError {
    if error.is_timeout() {
        PveReadError::TimedOut
    } else if error.is_decode() {
        PveReadError::InvalidResponse
    } else {
        PveReadError::TransportUnavailable
    }
}

fn parse_uuid(value: Option<&str>) -> Result<Option<VmUuid>, PveReadError> {
    let Some(value) = value else {
        return Ok(None);
    };
    value
        .split(',')
        .find_map(|field| field.strip_prefix("uuid="))
        .map(VmUuid::parse)
        .transpose()
        .map_err(|_| PveReadError::InvalidResponse)
}

fn parse_macs(
    values: &BTreeMap<String, serde_json::Value>,
) -> Result<std::collections::BTreeSet<MacAddress>, PveReadError> {
    values
        .iter()
        .filter(|(key, _)| key.starts_with("net"))
        .filter_map(|(_, value)| value.as_str())
        .filter_map(|value| value.split(',').next())
        .filter_map(|adapter| adapter.split_once('=').map(|(_, mac)| mac))
        .map(MacAddress::parse)
        .collect::<Result<_, _>>()
        .map_err(|_| PveReadError::InvalidResponse)
}

#[async_trait]
impl PveReadPort for ReqwestPveObserver {
    async fn vm_config(&self, node: &NodeName, vmid: Vmid) -> Result<VmConfig, PveReadError> {
        let vmid_text = vmid.to_string();
        let wire: VmConfigWire = self
            .get_data(&[
                "api2",
                "json",
                "nodes",
                node.as_str(),
                "qemu",
                &vmid_text,
                "config",
            ])
            .await?;
        Ok(VmConfig::new(
            vmid,
            parse_uuid(wire.smbios1.as_deref())?,
            parse_macs(&wire.values)?,
            Utc::now(),
        ))
    }

    async fn task_status(&self, node: &NodeName, upid: &Upid) -> Result<TaskStatus, PveReadError> {
        if upid.node() != node {
            return Err(PveReadError::UpidNodeMismatch);
        }
        let wire: TaskStatusWire = self
            .get_data(&[
                "api2",
                "json",
                "nodes",
                node.as_str(),
                "tasks",
                upid.as_str(),
                "status",
            ])
            .await?;
        let state = match (wire.status.as_str(), wire.exitstatus.as_deref()) {
            ("running", _) => TaskState::Running,
            ("stopped", Some("OK")) => TaskState::CompleteSuccess,
            ("stopped", Some(_)) => TaskState::CompleteFailure,
            _ => return Err(PveReadError::InvalidResponse),
        };
        Ok(TaskStatus::new(upid.clone(), state, Utc::now()))
    }

    async fn storage_content(
        &self,
        node: &NodeName,
        storage: &StorageName,
    ) -> Result<Vec<Volume>, PveReadError> {
        let wires: Vec<VolumeWire> = self
            .get_data(&[
                "api2",
                "json",
                "nodes",
                node.as_str(),
                "storage",
                storage.as_str(),
                "content",
            ])
            .await?;
        let observed_at = Utc::now();
        wires
            .into_iter()
            .map(|wire| {
                Volume::new(wire.volid, observed_at).map_err(|_| PveReadError::InvalidResponse)
            })
            .collect()
    }

    async fn qga_ping(&self, node: &NodeName, vmid: Vmid) -> Result<QgaStatus, PveReadError> {
        let vmid_text = vmid.to_string();
        let _: serde_json::Value = self
            .request_data(
                Method::POST,
                self.base_url.endpoint(&[
                    "api2",
                    "json",
                    "nodes",
                    node.as_str(),
                    "qemu",
                    &vmid_text,
                    "agent",
                    "ping",
                ]),
            )
            .await?;
        Ok(QgaStatus::new(true, Utc::now()))
    }
}

#[async_trait]
impl PvePreflightReadPort for ReqwestPveObserver {
    async fn node_status(&self, node: &NodeName) -> Result<NodeStatus, PveReadError> {
        let data = self
            .get_data(&["api2", "json", "nodes", node.as_str(), "status"])
            .await?;
        NodeStatus::from_wire(node.clone(), data, Utc::now())
    }
    async fn storage_status(
        &self,
        node: &NodeName,
        storage: &StorageName,
    ) -> Result<StorageStatus, PveReadError> {
        let data = self
            .get_data(&[
                "api2",
                "json",
                "nodes",
                node.as_str(),
                "storage",
                storage.as_str(),
                "status",
            ])
            .await?;
        StorageStatus::from_wire(node.clone(), storage.clone(), data, Utc::now())
    }
    async fn bridges(&self, node: &NodeName) -> Result<BridgeInventory, PveReadError> {
        let data = self
            .get_data(&["api2", "json", "nodes", node.as_str(), "network"])
            .await?;
        BridgeInventory::from_wire(node.clone(), data, Utc::now())
    }
    async fn cluster_vms(&self) -> Result<ClusterVmInventory, PveReadError> {
        let mut url = self
            .base_url
            .endpoint(&["api2", "json", "cluster", "resources"]);
        url.query_pairs_mut().append_pair("type", "vm");
        let data = self.request_data(Method::GET, url).await?;
        ClusterVmInventory::from_wire(data, Utc::now())
    }
    async fn native_vm_config(
        &self,
        node: &NodeName,
        vmid: Vmid,
    ) -> Result<NativeVmConfig, PveReadError> {
        let vmid_text = vmid.to_string();
        let data = self
            .get_data(&[
                "api2",
                "json",
                "nodes",
                node.as_str(),
                "qemu",
                &vmid_text,
                "config",
            ])
            .await?;
        NativeVmConfig::from_wire(node.clone(), vmid, data, Utc::now())
    }
    async fn vm_status(&self, node: &NodeName, vmid: Vmid) -> Result<VmPowerStatus, PveReadError> {
        let vmid_text = vmid.to_string();
        let data = self
            .get_data(&[
                "api2",
                "json",
                "nodes",
                node.as_str(),
                "qemu",
                &vmid_text,
                "status",
                "current",
            ])
            .await?;
        VmPowerStatus::from_wire(node.clone(), vmid, data, Utc::now())
    }
}

#[cfg(test)]
mod boundary_tests {
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };

    use super::{
        Client, PveAccessMode, PveBaseUrl, PveObserverBuildError, PveObserverConfig,
        ReqwestPveObserver, VerifiedClientBuilder,
    };

    struct CountingBuilder(Arc<AtomicUsize>);

    impl VerifiedClientBuilder for CountingBuilder {
        fn build(self, _: std::time::Duration) -> Result<Client, PveObserverBuildError> {
            self.0.fetch_add(1, Ordering::SeqCst);
            Err(PveObserverBuildError::HttpClient)
        }
    }

    #[test]
    fn denied_production_target_never_reaches_client_builder() {
        for target in [
            "https://192.168.2.4:8006",
            "https://[::ffff:192.168.2.4]:8006",
            "https://pve-production.invalid:8006",
        ] {
            let builds = Arc::new(AtomicUsize::new(0));
            let config = PveObserverConfig::new(
                PveBaseUrl::parse(target).unwrap(),
                PveAccessMode::Observe,
                false,
            );

            let error = ReqwestPveObserver::new_with_client_builder(
                config,
                CountingBuilder(Arc::clone(&builds)),
            )
            .unwrap_err();

            assert_eq!(error, PveObserverBuildError::TargetReadDenied);
            assert_eq!(builds.load(Ordering::SeqCst), 0, "built for {target}");
        }
    }

    #[test]
    fn credential_denial_precedes_client_build_for_every_denied_mode() {
        // Break: authenticated construction bypasses target authorization.
        for target in [
            "https://192.0.2.1:8006",
            "https://[::ffff:192.0.2.1]:8006",
            "https://denied-pve.invalid:8006",
        ] {
            for (mode, allow) in [
                (PveAccessMode::Observe, false),
                (PveAccessMode::Adapter, false),
                (PveAccessMode::Adapter, true),
                (PveAccessMode::Native, false),
                (PveAccessMode::Native, true),
            ] {
                let builds = Arc::new(AtomicUsize::new(0));
                let config =
                    PveObserverConfig::new(PveBaseUrl::parse(target).unwrap(), mode, allow)
                        .with_api_token(
                            crate::PveApiToken::parse(
                                "observer@pve!local-proof",
                                "synthetic-secret-123",
                            )
                            .unwrap(),
                        );
                let error = ReqwestPveObserver::new_with_client_builder(
                    config,
                    CountingBuilder(Arc::clone(&builds)),
                )
                .unwrap_err();
                assert_eq!(builds.load(Ordering::SeqCst), 0);
                assert_eq!(error, PveObserverBuildError::TargetReadDenied);
            }
        }
    }
}
