//! Bounded OS inspection for Ansible workers which detach into new sessions.
//! Never read argv/environment; compare process birth identity before signaling.
use std::collections::BTreeMap;

const MAX_PROCESSES: usize = 128;
#[derive(Clone, Copy, Eq, PartialEq)]
struct Identity {
    pid: i32,
    group: i32,
    birth: (u64, u64),
}
impl Identity {
    fn same_process(self, other: Self) -> bool {
        self.pid == other.pid && self.birth == other.birth
    }
}

// Private inspection/signal seam permits deterministic detached-worker races
// without executing a second playbook or an arbitrary test helper process.
trait ProcessAccess {
    fn identity(&self, pid: i32) -> Option<Identity>;
    fn children(&self, pid: i32) -> Vec<i32>;
    fn signal(&self, target: i32, signal: i32);
}
struct NativeAccess;
impl ProcessAccess for NativeAccess {
    fn identity(&self, pid: i32) -> Option<Identity> {
        identity(pid)
    }
    fn children(&self, pid: i32) -> Vec<i32> {
        children(pid)
    }
    fn signal(&self, target: i32, signal: i32) {
        unsafe {
            libc::kill(target, signal);
        }
    }
}

pub(crate) struct ProcessTree {
    root: i32,
    known: BTreeMap<i32, Identity>,
}
impl ProcessTree {
    pub(crate) fn new(root: i32) -> Self {
        let mut tree = Self {
            root,
            known: BTreeMap::new(),
        };
        tree.refresh(false);
        tree
    }
    pub(crate) fn refresh(&mut self, freeze: bool) {
        self.refresh_using(freeze, &NativeAccess);
    }
    fn refresh_using(&mut self, freeze: bool, access: &impl ProcessAccess) {
        let mut queue: Vec<_> = self
            .known
            .values()
            .filter_map(|known| {
                access
                    .identity(known.pid)
                    .filter(|current| current.same_process(*known))
            })
            .collect();
        if let Some(root) = access.identity(self.root) {
            // Once recorded, a reused root PID can never add another tree.
            if self
                .known
                .get(&self.root)
                .is_none_or(|known| known.same_process(root))
                && !queue.iter().any(|known| known.same_process(root))
            {
                queue.push(root);
            }
        }
        let mut index = 0;
        while index < queue.len() && index < MAX_PROCESSES {
            let current = queue[index];
            self.known.insert(current.pid, current);
            if freeze {
                signal_identity(current, libc::SIGSTOP, access);
            }
            for child in access.children(current.pid) {
                if queue.len() >= MAX_PROCESSES {
                    break;
                }
                if let Some(id) = access.identity(child)
                    && !queue.iter().any(|known| known.same_process(id))
                {
                    queue.push(id);
                }
            }
            index += 1;
        }
    }
    pub(crate) fn signal(&self, signal: i32) {
        self.signal_using(signal, &NativeAccess);
    }
    fn signal_using(&self, signal: i32, access: &impl ProcessAccess) {
        for id in self.known.values() {
            signal_identity(*id, signal, access);
        }
    }
    pub(crate) fn any_live(&self) -> bool {
        self.any_live_using(&NativeAccess)
    }
    fn any_live_using(&self, access: &impl ProcessAccess) -> bool {
        self.known.values().any(|known| {
            access
                .identity(known.pid)
                .is_some_and(|current| current.same_process(*known))
        })
    }
}

fn signal_identity(id: Identity, signal: i32, access: &impl ProcessAccess) {
    let Some(current) = access
        .identity(id.pid)
        .filter(|current| current.same_process(id))
    else {
        return;
    };
    // Group leaders cover their still-existing descendants even if reparented.
    // Individual PID checks cover workers which changed group after discovery.
    if current.group == current.pid {
        access.signal(-current.group, signal);
    }
    access.signal(current.pid, signal);
}

#[cfg(target_os = "macos")]
fn identity(pid: i32) -> Option<Identity> {
    let mut info = std::mem::MaybeUninit::<libc::proc_bsdinfo>::zeroed();
    let size = std::mem::size_of::<libc::proc_bsdinfo>() as i32;
    let read = unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTBSDINFO,
            0,
            info.as_mut_ptr().cast(),
            size,
        )
    };
    if read != size {
        return None;
    }
    let info = unsafe { info.assume_init() };
    if info.pbi_status == 5 {
        return None;
    } // SZOMB
    Some(Identity {
        pid,
        group: info.pbi_pgid as i32,
        birth: (info.pbi_start_tvsec, info.pbi_start_tvusec),
    })
}

#[cfg(target_os = "macos")]
fn children(pid: i32) -> Vec<i32> {
    let mut pids = [0i32; MAX_PROCESSES];
    // proc_listpids(PROC_PPID_ONLY) returns bytes, unlike convenience wrappers.
    let bytes = unsafe {
        libc::proc_listpids(
            6,
            pid as u32,
            pids.as_mut_ptr().cast(),
            std::mem::size_of_val(&pids) as i32,
        )
    };
    let count = (bytes.max(0) as usize / std::mem::size_of::<i32>()).min(MAX_PROCESSES);
    pids[..count]
        .iter()
        .copied()
        .filter(|pid| *pid > 0)
        .collect()
}

#[cfg(target_os = "linux")]
fn proc_text(path: &str) -> Option<String> {
    use std::io::Read;
    let mut text = String::new();
    std::fs::File::open(path)
        .ok()?
        .take(4096)
        .read_to_string(&mut text)
        .ok()?;
    Some(text)
}

#[cfg(target_os = "linux")]
fn identity(pid: i32) -> Option<Identity> {
    let stat = proc_text(&format!("/proc/{pid}/stat"))?;
    let (_, fields) = stat.rsplit_once(") ")?;
    let values: Vec<_> = fields.split_whitespace().take(20).collect();
    if values.len() < 20 || values[0] == "Z" {
        return None;
    }
    Some(Identity {
        pid,
        group: values[2].parse().ok()?,
        birth: (values[19].parse().ok()?, 0),
    })
}

#[cfg(target_os = "linux")]
fn children(pid: i32) -> Vec<i32> {
    proc_text(&format!("/proc/{pid}/task/{pid}/children"))
        .unwrap_or_default()
        .split_whitespace()
        .take(MAX_PROCESSES)
        .filter_map(|pid| pid.parse().ok())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    #[derive(Default)]
    struct Snapshot {
        processes: BTreeMap<i32, Identity>,
        children: BTreeMap<i32, Vec<i32>>,
        signals: RefCell<Vec<(i32, i32)>>,
    }
    impl ProcessAccess for Snapshot {
        fn identity(&self, pid: i32) -> Option<Identity> {
            self.processes.get(&pid).copied()
        }
        fn children(&self, pid: i32) -> Vec<i32> {
            self.children.get(&pid).cloned().unwrap_or_default()
        }
        fn signal(&self, target: i32, signal: i32) {
            self.signals.borrow_mut().push((target, signal));
        }
    }
    fn process(pid: i32, group: i32, birth: u64) -> Identity {
        Identity {
            pid,
            group,
            birth: (birth, 0),
        }
    }

    #[test]
    fn tracked_worker_changes_group_and_loses_parent_but_remains_owned() {
        // Break caught: mutable group equality discards a reparented worker,
        // so its new descendants and detached group survive cleanup.
        let mut snapshot = Snapshot::default();
        snapshot.processes.insert(100, process(100, 100, 1));
        snapshot.processes.insert(101, process(101, 100, 2));
        snapshot.children.insert(100, vec![101]);
        let mut tree = ProcessTree {
            root: 100,
            known: BTreeMap::new(),
        };
        tree.refresh_using(false, &snapshot);
        assert_eq!(tree.known.len(), 2);

        snapshot.processes.remove(&100);
        snapshot.children.remove(&100);
        snapshot.processes.insert(101, process(101, 101, 2));
        snapshot.processes.insert(102, process(102, 101, 3));
        snapshot.children.insert(101, vec![102]);
        assert!(tree.any_live_using(&snapshot));
        tree.refresh_using(false, &snapshot);
        assert_eq!(tree.known[&101].group, 101);
        assert!(tree.known.contains_key(&102));
        tree.signal_using(libc::SIGKILL, &snapshot);
        assert_eq!(
            *snapshot.signals.borrow(),
            vec![
                (-101, libc::SIGKILL),
                (101, libc::SIGKILL),
                (102, libc::SIGKILL)
            ]
        );
    }

    #[test]
    fn signaling_uses_current_group_and_rejects_reused_pid() {
        // Break caught: signaling the cached obsolete group or a recycled PID.
        let mut snapshot = Snapshot::default();
        let captured = process(101, 101, 2);
        snapshot.processes.insert(101, process(101, 200, 2));
        signal_identity(captured, libc::SIGKILL, &snapshot);
        assert_eq!(*snapshot.signals.borrow(), vec![(101, libc::SIGKILL)]);
        snapshot.signals.borrow_mut().clear();
        snapshot.processes.insert(101, process(101, 101, 9));
        signal_identity(captured, libc::SIGKILL, &snapshot);
        assert!(snapshot.signals.borrow().is_empty());
    }
}
