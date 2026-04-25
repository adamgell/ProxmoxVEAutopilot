# utm_answer_iso

Generates a Windows 11 ARM64 answer ISO (FAT32 hybrid, volume label
`AUTOUNATTEND`) from a profile dict, then stages a firstboot PowerShell
payload at `$OEM$\$1\autopilot\firstboot.ps1`.

## Role inputs

| Variable | Default | Required? | Notes |
|---|---|---|---|
| `utm_answer_admin_pass` | *(none)* | **Yes** | From vault. |
| `utm_answer_admin_user` | `Administrator` | No | |
| `utm_answer_locale` | `en-US` | No | |
| `utm_answer_timezone` | `Pacific Standard Time` | No | |
| `utm_answer_product_key` | `""` | No | Optional Windows key. |
| `utm_answer_windows_edition` | `Windows 11 Pro` | No | Must match install.wim entry. |
| `utm_answer_iso_dir` | `output/answer-isos` | No | |
| `vm_name` | *(caller-set)* | **Yes** | Used as ISO filename stem + default hostname. |

`oem_profile_resolver` must have run before this role — `_oem_profile`
facts are used to populate `org_name`.

## Role outputs

| Fact | Value |
|---|---|
| `utm_answer_iso_path` | Absolute path to the generated `.iso`. |

## OEM profile mapping

| Profile fact | Answer ISO field |
|---|---|
| `_oem_profile.manufacturer` | `org_name` (`<RegisteredOrganization>`) |
| `vm_name` | `hostname` (overridable via `utm_answer_hostname`) |
| `utm_answer_admin_user` | Local admin account name |
| `utm_answer_admin_pass` | Local admin + auto-logon password |

## $OEM$ structure on the ISO

```
autounattend.xml          ← auto-detected by Windows Setup
$OEM$/
  $1/
    autopilot/
      firstboot.ps1       → C:\autopilot\firstboot.ps1
```

`$OEM$\$1\` maps to `C:\` during Windows text-mode setup.

## Sentinel protocol

`firstboot.ps1` writes `C:\autopilot\autopilot-firstboot.done` on
completion. `utm_build_win11_template.yml` polls for this file via
`utmctl exec` before suspending the template VM.
