# Troubleshooting

Symptoms, causes, and fixes for the most common failures. If your issue isn't here, check the **Jobs** page for the full log of whatever last ran.

## Settings dropdowns are empty

**Symptom:** On the Settings page, the Node / Storage / ISO dropdowns show nothing or spin forever.

**Cause:** The UI couldn't reach Proxmox or the API token is rejected.

**Fix:**

1. Save your Proxmox host, port, and node manually first, then refresh — the dropdowns only populate after the token has a working target.
2. On the Proxmox host, confirm the token exists and the role is attached:

   ```bash
   pveum user token list autopilot@pve
   pveum acl list | grep autopilot
   ```

3. Verify from the Docker host that you can reach the Proxmox API:

   ```bash
   curl -k https://<PROXMOX_IP>:8006/api2/json/version \
     -H "Authorization: PVEAPIToken=autopilot@pve!ansible=<SECRET>"
   ```

   A 200 response with version info means the token works. 401 means the secret is wrong; 403 means the role isn't attached.

## 403 Forbidden when rebuilding the answer ISO

**Symptom:** Clicking **Rebuild Answer ISO** on the Build Template page fails with a 403.

**Cause:** The token has the role on `/` but not on the ISO storage, so `Datastore.AllocateSpace` isn't granted where the upload actually lands.

**Fix:**

```bash
pveum acl modify /storage/<iso-storage> -user autopilot@pve -role AutopilotProvisioner
```

Replace `<iso-storage>` with the storage name you configured as `proxmox_iso_storage` (default `isos`). After this, retry the rebuild.

## Template build hangs at "waiting for boot"

**Symptom:** The `build_template` job sits at the "waiting for VM to boot from CD" or "guest agent not responding" step and eventually times out.

**Possible causes and fixes:**

- **Answer ISO missing or mislabelled.** Windows Setup looks for a volume labelled `OEMDRV`. If you built the ISO manually without the `-V "OEMDRV"` flag, Windows will ignore it and sit on the "Press any key to boot from CD" prompt. Rebuild via the UI button, or re-run the command in [SETUP.md Appendix B](SETUP.md#appendix-b--air-gapped--manual-answer-iso-build).
- **VirtIO ISO version mismatch.** Windows 11 24H2+ needs a recent `virtio-win.iso`. Grab the latest from [fedorapeople.org](https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/).
- **Keypress didn't land.** OVMF's "press any key" window is short. If the template build doesn't get past it, open the Console from the running VM and hit a key yourself — once OVMF boots from the CD, Setup continues unattended.
- **Storage is full.** Check `pvesm status` — the VM disk storage needs room for a 64 GB disk plus snapshots.

## Guest agent never comes online after OOBE

**Symptom:** The VM finished installing and reaches the desktop, but Ansible reports "guest agent timeout" and hash capture never runs.

**Cause:** The QEMU guest agent wasn't installed by FirstLogonCommands. Usually this means the VirtIO ISO wasn't attached at the right drive letter, or the MSI path in `autounattend.xml` doesn't match the VirtIO ISO layout.

**Fix:**

1. Open the Console for the VM. If there's no guest agent, you'll see Windows running but Ansible idle.
2. Manually run the guest agent MSI from the VirtIO ISO (it's mounted as a CD drive): `E:\guest-agent\qemu-ga-x86_64.msi` (path varies by VirtIO version).
3. Once `qemu-ga` is running, Ansible picks up and continues.
4. For a long-term fix, check that `proxmox_virtio_iso` in Settings matches the ISO filename actually present on Proxmox.

## Hash upload to Intune fails

**Symptom:** The Upload to Intune job exits with "authentication failed" or "insufficient privileges".

**Cause:** Missing or wrong Entra app credentials, or the app registration doesn't have the right Microsoft Graph permissions.

**Fix:**

1. Check `vault.yml`:

   ```yaml
   vault_entra_app_id: "..."        # Application (client) ID
   vault_entra_tenant_id: "..."     # Directory (tenant) ID
   vault_entra_app_secret: "..."    # Client secret value (not the ID)
   ```

2. In Entra admin center, confirm the app registration has **application** permission `DeviceManagementServiceConfig.ReadWrite.All` on Microsoft Graph, and that admin consent has been granted.
3. Make sure the client secret hasn't expired.

## Lost my API token secret

Proxmox only shows token secrets once. If you lost it, regenerate:

```bash
pveum user token remove autopilot@pve ansible
pveum user token add autopilot@pve ansible --privsep=0 --comment "Automation"
```

Paste the new secret into `vault.yml` and `docker compose restart autopilot`.

## "No module named …" when running Ansible directly

**Symptom:** `ansible-playbook` from the host (not inside the container) fails with a missing Python module.

**Fix:** Install into a venv as shown in [SETUP.md Appendix C](SETUP.md#appendix-c--ansible-cli). The container already has all dependencies; only the CLI path needs a venv.

## Settings changed in the UI don't seem to take effect

**Symptom:** You edited something on the Settings page, but the next job still uses the old value.

**Cause:** Two likely candidates:

1. You edited `vars.yml` on disk at the same time. The container reads the file on each playbook run, so whichever save happened last wins — and if you edited on disk *after* a UI save, the UI thinks its value is live but the next run uses your on-disk value (or vice-versa).
2. The job that's "using the old value" was actually queued before you saved.

**Fix:** Pick one place to edit — either the UI or `vars.yml` directly — and stick with it. Re-queue any jobs that started before your change.

## Domain-join "Test connection" fails

The Test connection button on the `domain_join` credential form reports each stage (DNS → connect → bind → rootDSE → OU) green or red with elapsed ms. Most common failures:

- **DNS stage red** — no `_ldap._tcp.<domain>` SRV records reachable from the container. Check the Docker host's resolver; with `network_mode: host` the container uses the host's `/etc/resolv.conf`. Fix by ensuring the host can resolve the domain.
- **Connect stage red with TLS error** — the DC's certificate isn't trusted by the container. Flip the new `ad_validate_certs` Settings flag to `false` for lab use, or add the issuing CA to the host trust store and restart the container.
- **Bind stage red, error `invalidCredentials`** — wrong username/password. Username may be `user@domain.fqdn` or `DOMAIN\user`; try the other form.
- **Bind stage red, error `LDAP_STRONGER_AUTH_REQUIRED`** — your domain forbids LDAP simple bind on 389 without TLS. Use an LDAPS-capable DC, or configure StartTLS certificates correctly.
- **OU stage red, error `noSuchObject`** — the OU DN typed on the form doesn't exist, or the test account can't see it. Check the DN syntax (`OU=Workstations,DC=example,DC=local`).

The test proves *bind + OU visibility*. It does **not** prove the account has "join computer to domain" rights on that OU — that can only be verified by a real join attempt.

## "autopilot_hybrid step is not yet implemented"

**Symptom:** Provision fails immediately with this error.

**Cause:** The selected task sequence contains an `autopilot_hybrid` step. The builder accepts it so sequences stay stable across versions, but the compiler refuses at provision time in v1.

**Fix:** Edit the sequence and either remove the `autopilot_hybrid` step or swap it for `autopilot_entra`. Pure Hybrid Autopilot isn't supported yet.

## Lost my credential encryption key

**Symptom:** After recreating the container, the Credentials page shows rows but decrypting (editing, or provisioning a sequence that references them) fails.

**Cause:** The Fernet key at `/app/secrets/credential_key` is new. The rows were encrypted with the old key.

**Fix:** Restore the old key file if you have a backup, replace the file, and restart the container. If the old key is gone for good, delete the affected credential rows from the UI and re-create them. Prevent this next time by persisting `./secrets/` on the host — see the volume-mount table in [SETUP.md](SETUP.md#2c-what-the-compose-file-mounts).

## "Capture Hash" is disabled on a device

**Symptom:** A VM on the Devices page shows the Capture Hash action greyed out with a tooltip about the sequence.

**Cause:** The VM was provisioned with a sequence that has *produces Autopilot hash* turned off (typically the AD Domain Join sequence). No hash is expected — domain-joined machines don't go through Autopilot.

**Fix:** Expected behavior. If you want a hash for this VM, re-provision it with a sequence whose *produces hash* is on (for example the default Entra Join sequence).

## Can't delete a credential or sequence

**Symptom:** Delete returns 409 Conflict with a list of references.

**Cause:** Deletion is blocked while anything references the row — a credential that a step uses, or a sequence linked to an existing VM in `vm_provisioning`.

**Fix:** Either edit the referencing sequence/step to stop pointing at the credential, or delete the VMs first. Then retry the delete.

## Can't find my storage or SDN zone name

```bash
pvesm status                     # storages
pvesh get /cluster/sdn/zones     # SDN zones
pvesh get /nodes                 # node names
```

Use those values in your `pveum acl modify` commands and in the Settings page dropdowns.
