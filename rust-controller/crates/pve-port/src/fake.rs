use std::{
    collections::{HashMap, VecDeque},
    sync::{Arc, Mutex},
};

use async_trait::async_trait;

use crate::{
    NodeName, PveReadError, PveReadPort, QgaStatus, StorageName, TaskStatus, Upid, VmConfig, Vmid,
    Volume,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PveRequest {
    VmConfig {
        node: NodeName,
        vmid: Vmid,
    },
    TaskStatus {
        node: NodeName,
        upid: Upid,
    },
    StorageContent {
        node: NodeName,
        storage: StorageName,
    },
    QgaPing {
        node: NodeName,
        vmid: Vmid,
    },
}

#[derive(Default)]
struct FakeState {
    vm_configs: ResponseQueues<(NodeName, Vmid), VmConfig>,
    task_statuses: ResponseQueues<(NodeName, Upid), TaskStatus>,
    storage_contents: ResponseQueues<(NodeName, StorageName), Vec<Volume>>,
    qga_statuses: ResponseQueues<(NodeName, Vmid), QgaStatus>,
    requests: Vec<PveRequest>,
}

type ResponseQueues<K, T> = HashMap<K, VecDeque<Result<T, PveReadError>>>;

#[derive(Clone, Default)]
pub struct FakePve {
    state: Arc<Mutex<FakeState>>,
}

impl FakePve {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn enqueue_vm_config(
        &self,
        node: NodeName,
        vmid: Vmid,
        response: Result<VmConfig, PveReadError>,
    ) {
        self.state
            .lock()
            .expect("fake PVE state lock poisoned")
            .vm_configs
            .entry((node, vmid))
            .or_default()
            .push_back(response);
    }

    pub fn enqueue_task_status(
        &self,
        node: NodeName,
        upid: Upid,
        response: Result<TaskStatus, PveReadError>,
    ) {
        self.state
            .lock()
            .expect("fake PVE state lock poisoned")
            .task_statuses
            .entry((node, upid))
            .or_default()
            .push_back(response);
    }

    pub fn enqueue_storage_content(
        &self,
        node: NodeName,
        storage: StorageName,
        response: Result<Vec<Volume>, PveReadError>,
    ) {
        self.state
            .lock()
            .expect("fake PVE state lock poisoned")
            .storage_contents
            .entry((node, storage))
            .or_default()
            .push_back(response);
    }

    pub fn enqueue_qga_ping(
        &self,
        node: NodeName,
        vmid: Vmid,
        response: Result<QgaStatus, PveReadError>,
    ) {
        self.state
            .lock()
            .expect("fake PVE state lock poisoned")
            .qga_statuses
            .entry((node, vmid))
            .or_default()
            .push_back(response);
    }

    #[must_use]
    pub fn recorded_requests(&self) -> Vec<PveRequest> {
        self.state
            .lock()
            .expect("fake PVE state lock poisoned")
            .requests
            .clone()
    }
}

#[async_trait]
impl PveReadPort for FakePve {
    async fn vm_config(&self, node: &NodeName, vmid: Vmid) -> Result<VmConfig, PveReadError> {
        let mut state = self.state.lock().expect("fake PVE state lock poisoned");
        state.requests.push(PveRequest::VmConfig {
            node: node.clone(),
            vmid,
        });
        state
            .vm_configs
            .get_mut(&(node.clone(), vmid))
            .and_then(VecDeque::pop_front)
            .unwrap_or(Err(PveReadError::NotFound))
    }

    async fn task_status(&self, node: &NodeName, upid: &Upid) -> Result<TaskStatus, PveReadError> {
        if upid.node() != node {
            return Err(PveReadError::UpidNodeMismatch);
        }
        let mut state = self.state.lock().expect("fake PVE state lock poisoned");
        state.requests.push(PveRequest::TaskStatus {
            node: node.clone(),
            upid: upid.clone(),
        });
        state
            .task_statuses
            .get_mut(&(node.clone(), upid.clone()))
            .and_then(VecDeque::pop_front)
            .unwrap_or(Err(PveReadError::NotFound))
    }

    async fn storage_content(
        &self,
        node: &NodeName,
        storage: &StorageName,
    ) -> Result<Vec<Volume>, PveReadError> {
        let mut state = self.state.lock().expect("fake PVE state lock poisoned");
        state.requests.push(PveRequest::StorageContent {
            node: node.clone(),
            storage: storage.clone(),
        });
        state
            .storage_contents
            .get_mut(&(node.clone(), storage.clone()))
            .and_then(VecDeque::pop_front)
            .unwrap_or(Err(PveReadError::NotFound))
    }

    async fn qga_ping(&self, node: &NodeName, vmid: Vmid) -> Result<QgaStatus, PveReadError> {
        let mut state = self.state.lock().expect("fake PVE state lock poisoned");
        state.requests.push(PveRequest::QgaPing {
            node: node.clone(),
            vmid,
        });
        state
            .qga_statuses
            .get_mut(&(node.clone(), vmid))
            .and_then(VecDeque::pop_front)
            .unwrap_or(Err(PveReadError::NotFound))
    }
}
