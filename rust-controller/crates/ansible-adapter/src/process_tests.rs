use crate::{
    AdapterRegistry,
    runner::{ManagedProcess, NativeProcess, Prepared},
};
use std::{path::PathBuf, time::Duration};

fn invocation() -> crate::ValidatedInvocation {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let value = serde_json::json!({"id":"20260904-1234abcd","job_type":"test_long_sleep", "playbook":"/app/playbooks/_test_long_sleep.yml", "status":"pending", "cmd":["ansible-playbook","/app/playbooks/_test_long_sleep.yml","-e","duration=20"], "args":{"duration":"20"}});
    let plan = api_compat::normalize_job(
        &api_compat::JobEnvelope::from_json_str(&value.to_string()).unwrap(),
    )
    .unwrap();
    AdapterRegistry::local(&root)
        .unwrap()
        .validate(&plan)
        .unwrap()
}

#[test]
fn prepared_environment_inventory_home_and_playbook_are_isolated() {
    // Break caught: inheriting project/user config, inventory, plugins or secrets.
    let invocation = invocation();
    let prepared = Prepared::new(&invocation).unwrap();
    let expected_base = if cfg!(target_os = "macos") {
        "/private/tmp"
    } else {
        "/tmp"
    };
    assert_eq!(
        prepared.directory.path().parent().unwrap(),
        std::path::Path::new(expected_base)
    );
    let command = prepared.command(&invocation).unwrap();
    let env: std::collections::BTreeMap<_, _> = command
        .as_std()
        .get_envs()
        .map(|(k, v)| {
            (
                k.to_string_lossy().into_owned(),
                v.unwrap().to_string_lossy().into_owned(),
            )
        })
        .collect();
    assert_eq!(env["PATH"], "/usr/bin:/bin");
    assert_eq!(env["PYTHONNOUSERSITE"], "1");
    assert!(env["HOME"].starts_with(prepared.directory.path().to_str().unwrap()));
    assert!(env["ANSIBLE_CONFIG"].starts_with(prepared.directory.path().to_str().unwrap()));
    assert!(env["TMPDIR"].starts_with(prepared.directory.path().to_str().unwrap()));
    assert!(!env.contains_key("PYTHONPATH"));
    assert!(!env.contains_key("SECRET_CANARY"));
    assert_eq!(
        std::fs::read(prepared.directory.path().join("playbook.yml")).unwrap(),
        crate::contract::PLAYBOOK_BYTES
    );
    assert_eq!(
        command.as_std().get_current_dir().unwrap(),
        prepared.directory.path()
    );
}

async fn process_rows() -> Vec<(i32, i32, String)> {
    let output = tokio::process::Command::new("/bin/ps")
        .args(["-axo", "pid=,ppid=,stat=,comm="])
        .output()
        .await
        .unwrap();
    String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .filter_map(|line| {
            let fields: Vec<_> = line.split_whitespace().collect();
            if fields.len() < 4 || fields[2].starts_with('Z') {
                return None;
            }
            Some((
                fields[0].parse().ok()?,
                fields[1].parse().ok()?,
                fields[3].to_owned(),
            ))
        })
        .collect()
}

async fn children(parent: i32) -> Vec<i32> {
    let rows = process_rows().await;
    let mut pids = vec![parent];
    loop {
        let next: Vec<_> = rows
            .iter()
            .filter(|(pid, ppid, _)| pids.contains(ppid) && !pids.contains(pid))
            .map(|(pid, _, _)| *pid)
            .collect();
        if next.is_empty() {
            break;
        }
        pids.extend(next);
    }
    pids
}

#[tokio::test]
async fn native_group_cleanup_and_drop_kill_descendants() {
    // Break caught: killing only ansible's leader leaves its Python/sleep child.
    for dropped in [false, true] {
        let invocation = invocation();
        let prepared = Prepared::new(&invocation).unwrap();
        let (mut process, _output) = NativeProcess::spawn(&invocation, prepared).unwrap();
        let group = process.group_id();
        let descendants = tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                let pids = children(group).await;
                if process_rows()
                    .await
                    .iter()
                    .any(|(pid, _, command)| pids.contains(pid) && command.ends_with("sleep"))
                {
                    break pids;
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        })
        .await
        .expect("real approved playbook must create descendants");
        assert!(descendants.contains(&group));
        if !dropped {
            process.terminate().await;
        }
        drop(process);
        tokio::time::timeout(Duration::from_secs(5), async {
            while process_rows()
                .await
                .iter()
                .any(|(pid, _, _)| descendants.contains(pid))
            {
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .expect("no live descendants may remain");
    }
}
