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
        let mut queue: Vec<_> = self
            .known
            .values()
            .copied()
            .filter(|id| identity(id.pid) == Some(*id))
            .collect();
        if let Some(root) = identity(self.root) {
            // Once recorded, a reused root PID can never add another tree.
            if self
                .known
                .get(&self.root)
                .is_none_or(|known| *known == root)
                && !queue.contains(&root)
            {
                queue.push(root);
            }
        }
        let mut index = 0;
        while index < queue.len() && index < MAX_PROCESSES {
            let current = queue[index];
            self.known.insert(current.pid, current);
            if freeze {
                signal_identity(current, libc::SIGSTOP);
            }
            for child in children(current.pid) {
                if queue.len() >= MAX_PROCESSES {
                    break;
                }
                if let Some(id) = identity(child)
                    && !queue.contains(&id)
                {
                    queue.push(id);
                }
            }
            index += 1;
        }
    }
    pub(crate) fn signal(&self, signal: i32) {
        for id in self.known.values() {
            signal_identity(*id, signal);
        }
    }
    pub(crate) fn any_live(&self) -> bool {
        self.known.values().any(|id| identity(id.pid) == Some(*id))
    }
}

fn signal_identity(id: Identity, signal: i32) {
    if identity(id.pid) != Some(id) {
        return;
    }
    // Group leaders cover their still-existing descendants even if reparented.
    // Individual PID checks cover workers which changed group after discovery.
    unsafe {
        if id.group == id.pid {
            libc::kill(-id.group, signal);
        }
        libc::kill(id.pid, signal);
    }
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
