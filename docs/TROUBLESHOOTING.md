# Troubleshooting

Symptom → cause → fix notes for Proxmox VE Autopilot. See the main
[README](../README.md) and [SETUP.md](SETUP.md) for the happy-path flow.

## Ubuntu

### Autoinstall never starts (subiquity shows an interactive menu)

**Cause:** The NoCloud seed ISO wasn't detected. Subiquity auto-enables
autoinstall when it finds a CD-ROM labelled `cidata`; if your Ubuntu 24.04 ISO
variant doesn't auto-detect, you need the kernel cmdline `autoinstall ds=nocloud`.

**Fix:** Regenerate the seed ISO via **Rebuild Ubuntu Seed ISO** (this re-verifies
the `cidata` label). If it still fails, use an alternative Ubuntu ISO that
supports the NoCloud datasource by default, or modify the ISO to add the kernel
cmdline (out of scope for the web UI).

### `cloud-init clean` fails on the template VM

**Symptom:** Build Ubuntu Template job fails at the sysprep step with a permission
error.

**Cause:** The guest-exec runs as the `autoinstall` user rather than root.

**Fix:** The autoinstall YAML runs first-boot commands as root by default. If
you've added a `run_late_command` step that changes user ownership, revert it
and rebuild the seed ISO. Run `mdatp health` or `intune-portal --version`
manually via the Proxmox console to confirm the VM is booting cleanly.

### Ubuntu VM clones have duplicate `/etc/machine-id`

**Cause:** The per-VM cloud-init seed wasn't attached, so cloud-init didn't
reinitialise the instance.

**Fix:** Confirm the provision job's logs show "Attach per-VM seed ISO" succeeded.
If the per-VM seed endpoint returned an error, the clone still boots but
reuses the template's machine-id. Check that the web container can reach itself
at `http://127.0.0.1:5000/api/ubuntu/per-vm-seed` from inside the Ansible
execution environment.

### `install_mde_linux` step fails at compile

**Symptom:** Rebuild Ubuntu Seed ISO returns `install_mde_linux: mde_onboarding
credential {id} not provided`.

**Cause:** You either haven't uploaded an `mde_onboarding` credential, or the
sequence still references the seed-default credential id `0`.

**Fix:** On **Credentials**, create a new credential of type **MDE Linux
Onboarding** with the `.py` script from the Defender portal. Open the sequence,
pick that credential in the `install_mde_linux` step's `mde_onboarding_credential_id`
field, save, then retry Rebuild Ubuntu Seed ISO.

### Intune chip stays red (`enroll-intune-missing`)

**Expected if the user hasn't signed in yet.** `intune-portal` is installed
during autoinstall, but enrollment completes only on the first interactive
login. This is a Microsoft limitation for Intune Linux, not a bug in this tool.

**Fix:** Sign into the VM as the local admin user, launch Intune Portal, sign
in with an Entra account. Click **Check** again — the chip should flip to
`enroll-intune-healthy`.
