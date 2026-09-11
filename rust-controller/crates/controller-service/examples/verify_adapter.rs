//! Read-only installed-runtime preflight using the actual accepted registry.
fn main() -> anyhow::Result<()> {
    let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let registry = ansible_adapter::AdapterRegistry::local(&root)?;
    let job = api_compat::JobEnvelope::from_json_str(include_str!(
        "../../../fixtures/jobs/synthetic-long-sleep.json"
    ))?;
    registry.validate(&api_compat::normalize_job(&job)?)?;
    println!("trusted synthetic adapter runtime verified; no process launched");
    Ok(())
}
