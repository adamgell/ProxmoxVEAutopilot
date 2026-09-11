use ansible_adapter::AdapterRegistry;
use api_compat::{JobEnvelope, NormalizedPlan, normalize_job};
use serde_json::{Value, json};
use std::path::PathBuf;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

fn job(duration: &str) -> Value {
    json!({"id":"20260904-1234abcd","job_type":"test_long_sleep",
        "playbook":"/app/playbooks/_test_long_sleep.yml", "status":"pending",
        "cmd":["ansible-playbook","/app/playbooks/_test_long_sleep.yml","-e",format!("duration={duration}")],
        "args":{"duration":duration}})
}

fn plan(value: &Value) -> Result<NormalizedPlan, String> {
    let job = JobEnvelope::from_json_str(&value.to_string()).map_err(|_| "unsafe job")?;
    normalize_job(&job).map_err(|_| "unsafe plan".to_owned())
}

#[test]
fn only_fixed_local_contract_accepts_boundary_durations() {
    // Break caught: stale sleep_seconds schema or range widening.
    let registry = AdapterRegistry::local(&root()).unwrap();
    for duration in ["0", "20"] {
        assert!(registry.validate(&plan(&job(duration)).unwrap()).is_ok());
    }
    for duration in [
        "-1",
        "21",
        "0;id",
        "$(id)",
        "{{ lookup('env','SECRET') }}",
        "Bearer abc",
    ] {
        assert!(plan(&job(duration)).is_err());
    }
}

#[test]
fn input_cannot_select_executable_playbook_flags_environment_or_scripts() {
    // Break caught: user controlled execution routes escaping the typed registry.
    for (pointer, value) in [
        ("/cmd/0", json!("/bin/sh")),
        ("/cmd/0", json!("/tmp/ansible-playbook")),
        ("/cmd/1", json!("/tmp/generated.sh")),
        ("/playbook", json!("/app/playbooks/../other.yml")),
        ("/cmd/2", json!("--vault-password-file")),
        ("/args", json!({"duration":"0", "ansible_connection":"ssh"})),
        ("/args", json!({"duration":"0", "password":"secret-canary"})),
    ] {
        let mut input = job("0");
        *input.pointer_mut(pointer).unwrap() = value;
        assert!(plan(&input).is_err(), "accepted {pointer}");
    }
    let mut input = job("0");
    input["env"] = json!({"ANSIBLE_CONFIG":"/tmp/evil.cfg"});
    assert!(plan(&input).is_err());
}

#[test]
fn registry_rejects_playbook_symlink_outside_trusted_root() {
    // Break caught: lexical starts_with path checks following an outside symlink.
    let outside = tempfile::tempdir().unwrap();
    let checkout = tempfile::tempdir().unwrap();
    let relative = "autopilot-proxmox/playbooks/_test_long_sleep.yml";
    std::fs::write(
        outside.path().join("playbook.yml"),
        std::fs::read(root().join(relative)).unwrap(),
    )
    .unwrap();
    std::fs::create_dir_all(checkout.path().join("autopilot-proxmox/playbooks")).unwrap();
    std::os::unix::fs::symlink(
        outside.path().join("playbook.yml"),
        checkout.path().join(relative),
    )
    .unwrap();
    assert!(AdapterRegistry::local(checkout.path()).is_err());
}

#[test]
fn registry_rejects_replaced_playbook_even_at_approved_path() {
    // Break caught: approved pathname allowing replacement code.
    let checkout = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(checkout.path().join("autopilot-proxmox/playbooks")).unwrap();
    std::fs::write(
        checkout
            .path()
            .join("autopilot-proxmox/playbooks/_test_long_sleep.yml"),
        "- hosts: all\n",
    )
    .unwrap();
    assert!(AdapterRegistry::local(checkout.path()).is_err());
}
