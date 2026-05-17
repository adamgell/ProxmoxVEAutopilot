from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "scripts" / "init-proxmox-ve.sh"
CONTROLLER_INIT = ROOT / "scripts" / "init-controller-ubuntu.sh"
SEED_BUILD = ROOT / "scripts" / "build_seed_agent_container.sh"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"


def test_pve_init_provisions_controller_without_host_docker_runtime():
    text = INIT.read_text(encoding="utf-8")

    assert "pve-no-subscription.sources" in text
    assert "enterprise.proxmox.com" in text
    assert "disabled-by-autopilot" in text
    assert "99autopilot-force-ipv4" in text
    assert 'Acquire::ForceIPv4 "true";' in text
    assert "chronyc -a makestep" in text
    assert "installing PVE bootstrap essentials" in text
    assert "docker.io" not in text
    assert "systemctl enable --now docker" not in text
    assert "docker_compose up -d" not in text
    assert "openssl rand -hex 48" in text
    assert "download_ubuntu_cloud_image" in text
    assert "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img" in text
    assert "controller-bootstrap-ed25519" in text
    assert "pve-root-ed25519" in text
    assert "qm importdisk" in text
    assert "--ciuser \"${CONTROLLER_USER}\"" in text
    assert "--ipconfig0" in text
    assert "init-controller-ubuntu.sh" in text
    assert "create_migration_bundle" in text
    assert "stop_pve_runtime_stack" in text
    assert "controller_vm_ready" in text
    assert "controller_runtime_ready" in text
    assert "pve_host_ip" in text
    assert 'PVE_INIT_HOST="${ip}"' in text
    assert "--pve-node '${pve_node}'" in text
    assert "--pve-iso-storage '${pve_iso_storage}'" in text
    assert "--pve-disk-storage '${pve_disk_storage}'" in text
    assert "--pve-bridge '${pve_bridge}'" in text
    assert "detect_cloudosd_blank_template_vmid" in text
    assert "PVE_INIT_CLOUDOSD_TEMPLATE_VMID" in text
    assert "cloudosd_blank_template_vmid" in text
    assert "detect_osdeploy_blank_template_vmid" in text
    assert "ensure_osdeploy_blank_template" in text
    assert "autopilot-osdeploy-blank-template" in text
    assert "osdeploy_blank_template_vmid" in text
    assert "qm template \"${vmid}\"" in text
    assert "osdeploy_blank_template_ready" in text
    assert "ensure_pve_root_ssh_key" in text
    assert "pve_root_ssh_key_ready" in text
    assert "proxmox_root_ssh_key_path" in text
    assert "repair_pve_access_contract" in text
    assert "sync_controller_runtime_config" in text
    assert "controller_runtime_config_synced" in text


def test_controller_init_owns_docker_compose_and_source_builds():
    text = CONTROLLER_INIT.read_text(encoding="utf-8")

    assert "installing Ubuntu controller runtime prerequisites" in text
    assert "wait_for_apt_ready" in text
    assert "cloud-init status --wait" in text
    assert 'pgrep -x "apt|apt-get|dpkg|unattended-upgrade|unattended-upgrades"' in text
    assert "apt package locks did not clear" in text
    assert "docker.io" in text
    assert "docker-compose-plugin" in text or "docker-compose" in text
    assert "AUTOPILOT_AUTH_MODE=local" in text
    assert "AUTOPILOT_BASE_URL=http://${controller_ip}:5000" in text
    assert 'PVE_INIT_HOST="${PVE_HOST}"' in text
    assert 'PVE_INIT_NODE="${PVE_NODE}"' in text
    assert 'PVE_INIT_ISO_STORAGE="${PVE_ISO_STORAGE}"' in text
    assert 'PVE_INIT_DISK_STORAGE="${PVE_DISK_STORAGE}"' in text
    assert 'PVE_INIT_BRIDGE="${PVE_BRIDGE}"' in text
    assert '"proxmox_node": os.environ.get("PVE_INIT_NODE", "").strip()' in text
    assert '"proxmox_iso_storage": os.environ.get("PVE_INIT_ISO_STORAGE", "").strip()' in text
    assert '"proxmox_storage": os.environ.get("PVE_INIT_DISK_STORAGE", "").strip()' in text
    assert '"proxmox_bridge": os.environ.get("PVE_INIT_BRIDGE", "").strip()' in text
    assert "--pve-node) PVE_NODE=" in text
    assert "--pve-iso-storage) PVE_ISO_STORAGE=" in text
    assert "--pve-disk-storage) PVE_DISK_STORAGE=" in text
    assert "--pve-bridge) PVE_BRIDGE=" in text
    assert "--build-arg \"GIT_SHA=${git_sha}\"" in text
    assert "--build-arg \"BUILD_TIME=${build_time}\"" in text
    assert "--security-opt apparmor=unconfined" in text
    assert "--security-opt seccomp=unconfined" in text
    assert "build_seed_agent_container.sh" in text
    assert "ensure_postgres_database" in text
    assert "createdb -U autopilot autopilot" in text
    assert "ALTER ROLE autopilot WITH PASSWORD :'autopilot_password';" in text
    assert 'psql -h 127.0.0.1 -U autopilot -d autopilot' in text
    assert "docker_compose up -d --force-recreate" in text
    assert "http://${controller_ip}:5000/healthz" in text
    assert "controller_runtime_ready" in text
    assert "migration_bundle_restored" in text
    assert "postgres_volume_exists" in text
    assert 'env_file_value "${restored_env}" AUTOPILOT_POSTGRES_PASSWORD' in text
    assert 'secret_file_write "${SECRETS_DIR}/postgres-password" "${postgres_password}"' in text


def test_pve_init_keeps_media_gate_but_buildhost_creation_moves_to_controller():
    text = INIT.read_text(encoding="utf-8")

    assert "--download-windows" in text
    assert "--windows-iso-language" in text
    assert "resolve_windows_iso_from_microsoft" in text
    assert "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" in text
    assert "https://www.microsoft.com/en-us/software-download/windows11" in text
    assert "vlscppe.microsoft.com/fp/tags.js" in text
    assert "vlscppe.microsoft.com/tags" in text
    assert "ov-df.microsoft.com/mdt.js" in text
    assert "GetProductDownloadLinksBySku" in text
    assert "microsoft-software-download-connector" in text
    assert "resolve_windows_eval_iso_from_microsoft" in text
    assert "https://www.microsoft.com/en-us/evalcenter/download-windows-11-enterprise" in text
    assert '"linkid=" in link["href"].lower()' in text
    assert "microsoft-evaluation-center" in text
    assert "Windows ISO software-download resolver failed; trying Microsoft Evaluation Center" in text
    assert "windows_iso_download_error" in text
    assert "Windows ISO automatic resolver failed; media gate will remain blocked" in text
    assert "--windows-iso-url must be an operator-supplied official Microsoft" in text
    assert "Windows ISO already present" in text
    assert "curl -fL -C - --retry 3 --retry-all-errors" in text
    assert "cliententerprise" in text
    assert "select_windows_iso()" in text
    assert "enterpriseeval|cliententerprise|client.*eval|evaluation|eval" in text
    assert 'existing_iso="$(select_windows_iso "${dir}")"' in text
    assert "tolower($0)" in text
    assert 'state_text windows_iso_volid "${iso_storage}:iso/${filename}"' in text
    assert "stable-virtio/virtio-win.iso" in text
    assert "windows_iso_ready" in text
    assert "virtio_iso_ready" in text
    assert "update_media_vars_yml" in text
    assert "PVE_INIT_WINDOWS_ISO" in text
    assert "PVE_INIT_VIRTIO_ISO" in text
    assert "proxmox_windows_iso" in text
    assert "proxmox_virtio_iso" in text
    assert "media_ready" in text
    assert "publish_setup_state_to_controller" in text
    assert ".[0] * .[1]" in text
    assert "observe_existing_buildhost_vm" in text
    assert "build_host_creation_owner" in text
    assert 'state_text build_host_creation_owner "controller"' in text
    foundation = text[text.index("phase_foundation()") : text.index("phase_bootstrap()")]
    assert foundation.index("repair_pve_access_contract") < foundation.index("scan_media")
    assert foundation.index("scan_media") < foundation.index("sync_controller_runtime_config")
    bootstrap = text[text.index("phase_bootstrap()") : text.index("phase_operational()")]
    assert "repair_pve_access_contract" in bootstrap
    assert "sync_controller_runtime_config" in bootstrap
    assert "qm create" not in bootstrap
    assert "--ostype win11" not in bootstrap
    assert "Autounattend.xml" not in bootstrap


def test_pve_init_has_explicit_disposable_dev_lab_reset_phase():
    text = INIT.read_text(encoding="utf-8")

    assert "reset-dev-lab" in text
    assert "--reset-media" in text
    assert "phase_reset_dev_lab()" in text
    assert "reset_dev_lab_vms" in text
    assert "reset_dev_lab_media" in text
    assert "reset_dev_lab_runtime_state" in text
    assert "stop_pve_runtime_stack" in text
    assert "is_dev_lab_vm_name" in text
    assert "autopilot-controller-01" in text
    assert "autopilot-buildhost-01" in text
    assert "autopilot-osdeploy-blank-template" in text
    assert "OSDEPLOY-E2E-*" in text
    assert "qm destroy \"${vmid}\" --purge 1 --destroy-unreferenced-disks 1" in text
    assert "virtio-win*.iso" in text
    assert "osdeploy-server-*.iso" in text
    assert "cloudosd-autopilot-*.iso" in text
    assert "autopilot-buildhost-seed-*.iso" in text
    assert 'rm -rf "${SETUP_DIR}" "${SECRETS_DIR}" "${PVE_RUNTIME_DIR}"' in text
    assert 'rm -f "${ENV_FILE}" "${VARS_FILE}" "${VAULT_FILE}"' in text
    assert "dev_lab_reset_ready" in text


def test_pve_operational_repairs_token_and_syncs_controller_config_before_publish():
    text = INIT.read_text(encoding="utf-8")
    operational = text[text.index("phase_operational()") :]

    assert "require_command pveum" in operational
    assert "require_command pvesm" in operational
    assert "require_command qm" in operational
    assert "repair_pve_access_contract" in operational
    assert "sync_controller_runtime_config" in operational
    assert "promote_controller_setup_artifacts" in operational
    assert operational.index("repair_pve_access_contract") < operational.index(
        "verify_controller_health"
    )
    assert operational.index("scan_media") < operational.index(
        "sync_controller_runtime_config"
    )
    assert operational.index("sync_controller_runtime_config") < operational.index(
        "promote_controller_setup_artifacts"
    )


def test_pve_runtime_config_sync_copies_vault_without_logging_secret():
    text = INIT.read_text(encoding="utf-8")
    sync = text[text.index("sync_controller_runtime_config()") : text.index("controller_artifact_host_path()")]
    source_sync = text[text.index("sync_source_to_controller()") : text.index("copy_migration_bundle_to_controller()")]

    assert '"${VAULT_FILE}"' in sync
    assert '"${VARS_FILE}"' in sync
    assert '"${ENV_FILE}"' not in sync
    assert '"${SECRETS_DIR}/"' not in sync
    assert "pve-root-ed25519" in sync
    assert "postgres-password" not in sync
    assert "--exclude 'autopilot-proxmox/.env'" in source_sync
    assert "--exclude 'autopilot-proxmox/inventory/group_vars/all/vars.yml'" in source_sync
    assert "--exclude 'autopilot-proxmox/inventory/group_vars/all/vault.yml'" in source_sync
    assert "--exclude 'autopilot-proxmox/secrets/'" in source_sync
    assert "--exclude 'autopilot-proxmox/cache/'" in source_sync
    assert "vault.yml" in sync
    assert "chmod 600" in sync
    assert "vault_proxmox_api_token_secret" not in sync


def test_pve_init_has_runtime_config_only_repair_phase():
    text = INIT.read_text(encoding="utf-8")
    phase = text[text.index("phase_runtime_config()") : text.index("phase_reset_dev_lab()")]

    assert "--phase foundation|bootstrap|operational|runtime-config|reset-dev-lab|all" in text
    assert "runtime-config) phase_runtime_config" in text
    assert "repair_pve_access_contract" in phase
    assert "scan_media" in phase
    assert "sync_controller_runtime_config" in phase
    assert "run_controller_init" not in phase
    assert "start_compose" not in phase
    assert "sync_source_to_controller" not in phase


def test_pve_operational_pulls_large_setup_iso_artifacts_from_controller():
    text = INIT.read_text(encoding="utf-8")
    operational = text[text.index("phase_operational()") :]

    assert "promote_controller_setup_artifacts" in text
    assert "controller_artifact_host_path" in text
    assert "/app/cache/osdeploy/*" in text
    assert "/app/cache/cloudosd/*" in text
    assert "artifact_registry.json" in text
    assert "rsync -a --partial --inplace" in text
    assert '"already_copied": True' in text
    assert "/api/setup/v1/artifacts/promote" in text
    assert "promoted_artifacts_last_pull_at" in text
    assert operational.index("verify_controller_health") < operational.index(
        "promote_controller_setup_artifacts"
    )


def test_pve_init_clears_recycled_controller_known_host_before_ssh():
    text = INIT.read_text(encoding="utf-8")

    assert "CONTROLLER_KNOWN_HOSTS" in text
    assert "reset_controller_known_host" in text
    assert 'ssh-keygen -f "${CONTROLLER_KNOWN_HOSTS}" -R "${ip}"' in text
    assert 'reset_controller_known_host "${ip}"' in text

    wait_for_ssh = text.index("wait_for_ssh()")
    reset_call = text.index('reset_controller_known_host "${ip}"', wait_for_ssh)
    ssh_loop = text.index('if ssh "${ssh_args[@]}"', wait_for_ssh)
    assert reset_call < ssh_loop


def test_foundation_publishes_final_state_after_controller_health():
    text = INIT.read_text(encoding="utf-8")
    phase = text[text.index("phase_foundation()") : text.index("phase_bootstrap()")]

    assert phase.index('verify_controller_health "${controller_ip}"') < phase.index("stop_pve_runtime_stack")
    assert phase.index("stop_pve_runtime_stack") < phase.index("publish_setup_state_to_controller")


def test_controller_bootstrap_key_lives_outside_synced_repo_tree():
    text = INIT.read_text(encoding="utf-8")

    assert 'PVE_RUNTIME_DIR="${PVE_RUNTIME_DIR:-/root/.local/share/proxmoxveautopilot}"' in text
    assert 'CONTROLLER_SSH_KEY="${CONTROLLER_SSH_KEY:-${PVE_RUNTIME_DIR}/controller-bootstrap-ed25519}"' in text
    assert 'mkdir -p "${SECRETS_DIR}" "$(dirname "${CONTROLLER_SSH_KEY}")"' in text
    assert 'chmod 700 "${SECRETS_DIR}" "$(dirname "${CONTROLLER_SSH_KEY}")"' in text
    assert 'CONTROLLER_SSH_KEY="${SECRETS_DIR}/controller-bootstrap-ed25519"' not in text


def test_seed_agent_build_uses_dotnet_sdk_container_without_repo_outputs():
    text = SEED_BUILD.read_text(encoding="utf-8")

    assert "mcr.microsoft.com/dotnet/sdk:8.0" in text
    assert "--mount \"type=bind,src=${AGENT_ROOT},dst=/src,readonly\"" in text
    assert "BaseIntermediateOutputPath=\"${work}/obj/\"" in text
    assert "BaseOutputPath=\"${work}/bin/\"" in text
    assert "DefaultItemExcludes" in text
    assert "obj/**%3Bbin/**" in text
    assert "manifest.json" in text
    assert "SHA256SUMS" in text
    assert "dotnet publish" in text
    assert "/src/src/AutopilotAgent/AutopilotAgent.csproj" in text


def test_compose_exposes_lan_reachable_base_url_to_services():
    text = COMPOSE.read_text(encoding="utf-8")

    assert "AUTOPILOT_BASE_URL: ${AUTOPILOT_BASE_URL:-http://127.0.0.1:5000}" in text
    assert "AUTOPILOT_AUTH_MODE: ${AUTOPILOT_AUTH_MODE:-auto}" in text
    assert "AUTOPILOT_TS_ENGINE_DATABASE_URL: host=127.0.0.1 port=5432" in text


def test_dockerfile_avoids_curl_for_microsoft_key_fetch_on_pve_buildkit():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "urlopen('https://packages.microsoft.com/keys/microsoft.asc'" in text
    assert "curl -fsSL https://packages.microsoft.com/keys/microsoft.asc" not in text


def test_dockerfile_creates_osdeploy_cache_runtime_directory():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "/app/cache/osdeploy" in text


def test_mcp_source_is_packaged_and_compose_uses_persistent_entrypoint():
    compose = COMPOSE.read_text(encoding="utf-8")
    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8")

    assert 'command: ["mcp"]' in compose
    assert "web/mcp/server.py" not in dockerignore
    assert "!web/mcp/__pycache__/" not in dockerignore
    assert "!web/mcp/__pycache__/*.pyc" not in dockerignore
