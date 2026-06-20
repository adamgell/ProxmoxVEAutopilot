import { expect, test, type Page } from "@playwright/test";

async function mockReadApis(page: Page) {
  await page.route("**/api/services", async (route) => {
    await route.fulfill({
      json: {
        available: true,
        services: [{ service: "autopilot", status: "healthy", age_seconds: 5, detail: "running" }]
      }
    });
  });
  await page.route("**/api/jobs/running", async (route) => {
    await route.fulfill({
      json: {
        running: [{ id: "job-running", playbook: "provision.yml", target: "PC-001", progress_pct: 25 }],
        running_count: 1,
        queued_count: 1
      }
    });
  });
  await page.route("**/api/jobs/recent?limit=5", async (route) => {
    await route.fulfill({
      json: {
        jobs: [
          {
            id: "job-complete",
            playbook: "template.yml",
            status: "complete",
            duration: "3m 0s",
            target: "Win11-Template"
          }
        ]
      }
    });
  });
  await page.route("**/api/fleet/summary", async (route) => {
    await route.fulfill({ json: { total: 4, ad_joined_pct: 75, autopilot_pct: 50, intune_pct: 25 } });
  });
  await page.route("**/api/jobs", async (route) => {
    await route.fulfill({
      json: [
        {
          id: "job-running",
          playbook: "provision.yml",
          status: "running",
          duration: "2m 0s",
          args: { vm_name: "PC-001" },
          paused: false
        },
        {
          id: "job-failed",
          playbook: "capture.yml",
          status: "failed",
          duration: "1m 0s",
          args: { serial: "SN-001" },
          paused: false
        }
      ]
    });
  });
  await page.route("**/api/monitoring/runtime-services", async (route) => {
    await route.fulfill({
      json: {
        available: true,
        error: "",
        containers: [
          {
            id: "abc123",
            name: "autopilot",
            service: "autopilot",
            image: "proxmox-autopilot:latest",
            status: "running",
            health: "healthy",
            restart_count: 0,
            log_url: "/api/monitoring/service-logs?container=autopilot"
          }
        ]
      }
    });
  });
  await page.route("**/api/monitoring/deployments/summary", async (route) => {
    await route.fulfill({ json: { total: 2, running: 1, succeeded: 1, failed: 0 } });
  });
  await page.route("**/api/monitoring/keytab/health", async (route) => {
    await route.fulfill({ json: { status: "ok", detail: "keytab valid" } });
  });
  await page.route("**/api/monitoring/signals", async (route) => {
    await route.fulfill({
      json: {
        generated_at: "2026-05-19T00:00:00Z",
        build: { sha_short: "abc1234", build_time: "2026-05-18T12:00:00Z" },
        source_health: { runtime_available: true, setup_health: "ready" },
        metrics: [],
        signals: [
          {
            id: "runtime",
            family: "runtime",
            label: "Runtime containers",
            status: "healthy",
            tone: "good",
            summary: "autopilot is healthy"
          },
          {
            id: "build-host",
            family: "build_host",
            label: "Build host agent",
            status: "ready",
            tone: "good",
            summary: "buildhost-100 heartbeat fresh"
          }
        ],
        operator_paths: [
          {
            id: "server-deploy",
            priority: 20,
            label: "Windows Server OSDeploy artifact is available",
            status: "ready",
            tone: "good",
            summary: "Open the existing OSDeploy execution flow.",
            action_label: "Open server deploy",
            href: "/osdeploy"
          }
        ],
        lifecycle_lanes: [
          {
            id: "provisioned",
            label: "Provisioned",
            value: "2/3",
            detail: "Running in Proxmox and visible to the monitor.",
            status: "attention",
            tone: "active"
          }
        ],
        deployment_health: {
          summary: {
            total: 4,
            active: 1,
            running: 1,
            completed: 2,
            succeeded: 2,
            failed: 1,
            stuck: 0,
            regressed: 1,
            slow: 0,
            median_completion_seconds: 300,
            p95_completion_seconds: 900,
            recent_failure_rate: 0.25
          },
          active: [
            {
              deployment_key: "osdeploy/run-1",
              deployment_type: "osdeploy",
              current_phase: "windows_setup",
              elapsed_seconds: 120,
              health: "running",
              state: "running",
              next_expected_evidence: "agent heartbeat"
            }
          ],
          recent_completions: [],
          bottlenecks: [
            {
              deployment_type: "osdeploy",
              phase_key: "windows_setup",
              phase_label: "Windows setup",
              count: 1,
              health: "regressed",
              p95_seconds: 900
            }
          ]
        },
        services: [
          {
            service_id: "autopilot-monitor",
            status: "ok",
            age_seconds: 12,
            detail: "sweep idle"
          }
        ],
        runtime: {
          available: true,
          error: "",
          containers: [
            {
              name: "autopilot",
              service: "autopilot",
              image: "proxmox-autopilot:latest",
              status: "running",
              health: "healthy"
            }
          ]
        },
        fleet_attention: [
          {
            vmid: 101,
            vm_name: "WIN-SRV-01",
            node: "pve1",
            lifecycle: "Needs check",
            tone: "bad",
            pve_status: "running",
            windows: "WIN-SRV-01",
            serial: "SER-101",
            ad: "ok",
            entra: "missing",
            intune: "missing",
            last_checked: "2026-05-19T00:00:00Z",
            href: "/devices/101"
          }
        ]
      }
    });
  });
  await page.route("**/api/monitoring/service-logs?tail=180&container=autopilot", async (route) => {
    await route.fulfill({
      json: {
        container: "autopilot",
        service: "autopilot",
        tail: 180,
        lines: ["2026-05-19T00:00:00Z autopilot ready"]
      }
    });
  });
  await page.route("**/api/vms/fleet", async (route) => {
    await route.fulfill({
      json: {
        generated_at: "2026-05-19T00:00:00Z",
        cache_age_seconds: 14,
        cache_refreshing: false,
        monitor_sweep: { running: false, vm_count: 1 },
        ap_error: "",
        vms: [
          {
            vmid: 108,
            name: "WrkGrp-525570B6",
            hostname: "WRKGRP-525570B6",
            serial: "WrkGrp-525570B6",
            status: "running",
            ip_address: "192.168.2.49",
            in_autopilot: true,
            in_intune: false,
            aad_joined: true,
            part_of_domain: false,
            has_hash: true,
            target_os: "windows"
          }
        ],
        missing_vms: [],
        agents: [
          {
            agent_id: "agent-wrkgrp-525570b6",
            approval_status: "active",
            vmid: 108,
            computer_name: "WRKGRP-525570B6",
            primary_ipv4: "192.168.2.49",
            qga_state: "Running",
            current_phase: "cloudosd",
            last_heartbeat_at: "2026-05-19T00:00:00Z",
            hash_capture_supported: true
          }
        ],
        autopilot_devices: [
          {
            id: "device-1",
            serial: "WrkGrp-525570B6",
            display_name: "WRKGRP-525570B6",
            group_tag: "Lab",
            profile_status: "assigned",
            profile_ok: true,
            enrollment_state: "enrolled",
            has_local_hash: true
          }
        ]
      }
    });
  });
  await page.route("**/api/cloud/devices", async (route) => {
    await route.fulfill({
      json: {
        groups: [
          {
            serial: "SER-101",
            display_name: "ACME-101",
            intune: { id: "intune-101", display_name: "ACME-101" },
            autopilot: { id: "ap-101", profile: "Autopilot profile", group_tag: "Lab" },
            entra: { id: "entra-101", display_name: "ACME-101" },
            pve: { vmid: 108, name: "ACME-101" }
          }
        ],
        unmatched: {},
        meta: {},
        windows_only: true,
        deletions: []
      }
    });
  });
  await page.route("**/api/hashes", async (route) => {
    await route.fulfill({
      json: {
        hash_files: [{ filename: "ACME-101_hwid.csv", serial: "SER-101", group_tag: "Lab", size: 2048, in_intune: false }]
      }
    });
  });
  await page.route("**/api/files", async (route) => {
    await route.fulfill({ json: { files: [{ filename: "AutopilotAgent.msi", size: 4096, mtime: "2026-05-19T12:00:00+00:00" }] } });
  });
  await page.route("**/api/settings", async (route) => {
    await route.fulfill({
      json: {
        sections: [
          {
            section: "General",
            source: "vars",
            fields: [{ key: "hypervisor_type", label: "Hypervisor", type: "select", value: "proxmox", options: ["proxmox"], labels: { proxmox: "Proxmox" } }]
          }
        ],
        saved: false,
        hypervisor_type: "proxmox",
        proxmox_bootstrap: { host: "pve2", disk_storage: "local-lvm", iso_storage: "local", root_password_set: true, default_token_id: "autopilot@pve!autopilot" }
      }
    });
  });
  await page.route("**/api/monitoring/settings/full", async (route) => {
    await route.fulfill({
      json: {
        settings: { enabled: true, interval_seconds: 300, ad_credential_id: 0, updated_at: "2026-05-19T12:00:00+00:00" },
        search_ous: [{ id: 1, dn: "OU=Workstations,DC=example,DC=com", label: "Workstations", enabled: true, sort_order: 10 }],
        domain_creds: [],
        keytab: { status: "ok", checked_at: "2026-05-19T12:00:00+00:00", message: "keytab valid" }
      }
    });
  });
  await page.route("**/api/credentials", async (route) => {
    await route.fulfill({ json: [{ id: 7, name: "ACME Domain Join", type: "domain_join", updated_at: "2026-05-19T12:00:00+00:00" }] });
  });
}

test("renders the React shell without layout overlap", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "What are you trying to finish?" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Outcome modes" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Start desktop run" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open signals" })).toBeVisible();
  await expect(page.getByText("Jinja")).toHaveCount(0);

  const hero = await page.locator(".outcome-topbar").boundingBox();
  const panel = await page.locator(".workspace__content").boundingBox();

  expect(hero).not.toBeNull();
  expect(panel).not.toBeNull();
  if (!hero || !panel) {
    throw new Error("React shell layout regions were not measurable.");
  }
  expect(hero.y + hero.height).toBeLessThanOrEqual(panel.y + 1);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 390, height: 844 }
]) {
  test(`renders dashboard read-only route without overlap on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockReadApis(page);
    await page.goto("/react/dashboard");

    await expect(page.getByRole("heading", { name: "Dashboard", exact: true })).toBeVisible();
    await expect(page.getByText("PC-001")).toBeVisible();
    await expect(page.getByLabel("Launchpad").getByRole("link", { name: "Provision", exact: true })).toHaveAttribute("href", "/react/provision");

    const header = await page.locator(".page-head").boundingBox();
    const metrics = await page.locator(".metric-strip").first().boundingBox();
    expect(header).not.toBeNull();
    expect(metrics).not.toBeNull();
    if (!header || !metrics) {
      throw new Error("Dashboard layout regions were not measurable.");
    }
    expect(header.y + header.height).toBeLessThanOrEqual(metrics.y + 1);
  });

  test(`renders jobs read-only route without overlap on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockReadApis(page);
    await page.goto("/react/jobs");

    await expect(page.getByRole("heading", { name: "Jobs", exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "job-running" })).toBeVisible();
    await expect(page.getByLabel("Filter jobs")).toBeVisible();

    const metrics = await page.locator(".metric-strip").first().boundingBox();
    const filter = await page.locator(".filter").boundingBox();
    expect(metrics).not.toBeNull();
    expect(filter).not.toBeNull();
    if (!metrics || !filter) {
      throw new Error("Jobs layout regions were not measurable.");
    }
    expect(metrics.y + metrics.height).toBeLessThanOrEqual(filter.y + 1);
  });

  test(`renders Signals Hub read-only route without overlap on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockReadApis(page);
    await page.goto("/react/monitoring");

    await expect(page.getByRole("heading", { name: "Signals Hub", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Signal families" })).toBeVisible();
    await expect(page.getByText("Build host agent")).toBeVisible();
    await expect(page.getByRole("link", { name: "Open server deploy" })).toBeVisible();
    await expect(page.locator("main").getByRole("link", { name: "Monitoring settings", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Deployment speed" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Lifecycle lanes" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Service health" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Runtime containers" })).toBeVisible();
    await page.getByRole("button", { name: "Tail" }).click();
    await expect(page.getByText("2026-05-19T00:00:00Z autopilot ready")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Fleet attention" })).toBeVisible();

    const header = await page.locator(".page-head").boundingBox();
    const metrics = await page.locator(".metric-strip").first().boundingBox();
    expect(header).not.toBeNull();
    expect(metrics).not.toBeNull();
    if (!header || !metrics) {
      throw new Error("Signals Hub layout regions were not measurable.");
    }
    expect(header.y + header.height).toBeLessThanOrEqual(metrics.y + 1);
  });

  test(`renders VMs Fleet route without overlap on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockReadApis(page);
    await page.goto("/react/vms");

    await expect(page.getByRole("heading", { name: "VMs", exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "WrkGrp-525570B6" })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "Managed By" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Screenshot VM 108" })).toHaveCount(0);
    await page.getByRole("link", { name: "WrkGrp-525570B6" }).click();
    await expect(page).toHaveURL(/\/react\/vms\/108$/);
    await expect(page.getByRole("button", { name: "Screenshot VM 108" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Delete VM 108" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Console VM 108" })).toBeVisible();
    await expect(page.getByText("agent-wrkgrp-525570b6")).toBeVisible();
    await expect(page.getByRole("link", { name: "Console VM 108" })).toHaveCount(0);
    await page.getByRole("button", { name: "Console VM 108" }).click();
    await expect(page.getByRole("region", { name: "VM action workspace" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Open legacy console" })).toHaveAttribute("href", "/vms/108/console");
    await page.getByRole("button", { name: "Expand console" }).click();
    await expect(page.locator(".vm-action-workspace--expanded")).toBeVisible();
    const expandedTarget = await page.locator(".vm-action-workspace--expanded .vm-action-workspace__target").boundingBox();
    const expandedTabs = await page.locator(".vm-action-workspace--expanded .vm-action-tabs").boundingBox();
    const expandedConsole = await page.locator(".vm-action-workspace--expanded .vm-console-screen").boundingBox();
    expect(expandedTarget).not.toBeNull();
    expect(expandedTabs).not.toBeNull();
    expect(expandedConsole).not.toBeNull();
    if (!expandedTarget || !expandedTabs || !expandedConsole) {
      throw new Error("Expanded VM action workspace regions were not measurable.");
    }
    expect(expandedTarget.height).toBeLessThanOrEqual(90);
    expect(expandedTabs.height).toBeLessThanOrEqual(44);
    expect(expandedConsole.height).toBeGreaterThan(expandedTarget.height + expandedTabs.height);
    await page.getByRole("button", { name: "Minimize action" }).click();
    await expect(page.getByRole("button", { name: "Restore action" })).toBeVisible();

    const hero = await page.locator(".vm-detail-hero").boundingBox();
    const toolbar = await page.locator(".vm-detail-toolbar").boundingBox();
    expect(hero).not.toBeNull();
    expect(toolbar).not.toBeNull();
    if (!hero || !toolbar) {
      throw new Error("VM detail layout regions were not measurable.");
    }
    expect(hero.y + hero.height).toBeLessThanOrEqual(toolbar.y + 1);
  });

  for (const route of [
    { path: "/react/devices", heading: "Cloud Devices", text: "ACME-101" },
    { path: "/react/legacy-vms", heading: "Classic VM Table", text: "WRKGRP-525570B6" },
    { path: "/react/hashes", heading: "Hashes", text: "ACME-101_hwid.csv" },
    { path: "/react/files", heading: "Files", text: "AutopilotAgent.msi" },
    { path: "/react/settings", heading: "Settings", text: "Proxmox bootstrap" },
    { path: "/react/credentials", heading: "Credentials", text: "ACME Domain Join" },
    { path: "/react/monitoring/settings", heading: "Monitoring Settings", text: "OU=Workstations,DC=example,DC=com" }
  ]) {
    test(`renders ${route.path} without overlap on ${viewport.name}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await mockReadApis(page);
      await page.goto(route.path);

      await expect(page.locator("main").getByRole("heading", { name: route.heading, exact: true })).toBeVisible();
      await expect(page.getByText(route.text).first()).toBeVisible();

      const header = await page.locator(".page-head").boundingBox();
      const firstPanel = await page.locator(".panel").first().boundingBox();
      expect(header).not.toBeNull();
      expect(firstPanel).not.toBeNull();
      if (!header || !firstPanel) {
        throw new Error(`${route.path} layout regions were not measurable.`);
      }
      expect(header.y + header.height).toBeLessThanOrEqual(firstPanel.y + 180);
    });
  }
}

test("renders outcome shell on desktop and mobile widths", async ({ page }) => {
  await mockReadApis(page);
  await page.setViewportSize({ width: 1440, height: 980 });
  await page.goto("/react-shell");

  await expect(page.getByRole("navigation", { name: "Outcome modes" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "What are you trying to finish?" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Deploy a Windows desktop" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Quick routes" })).toBeVisible();

  await page.setViewportSize({ width: 390, height: 980 });
  await expect(page.getByRole("link", { name: "Deploy", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Set", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Deploy a Windows desktop" })).toBeVisible();

  const horizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(horizontalOverflow).toBe(false);
});
