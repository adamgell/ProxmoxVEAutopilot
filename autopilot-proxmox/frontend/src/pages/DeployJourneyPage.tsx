import { useCallback, useMemo, useState } from "react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import type { AppBootstrap, RunningJobsResponse, ServicesResponse } from "../contracts";
import { usePolling } from "../hooks/usePolling";

interface CloudosdJourneyPayload {
  readonly ready_artifacts?: readonly unknown[];
  readonly active_runs?: readonly unknown[];
  readonly cloudosd_cache?: {
    readonly summary?: {
      readonly ready?: number;
      readonly total?: number;
    };
  };
}

interface DeployJourneyItem {
  readonly label: string;
  readonly badge: string;
  readonly href: string;
  readonly active?: boolean;
}

interface DeployJourneyStep {
  readonly title: string;
  readonly body: string;
  readonly href: string;
  readonly active?: boolean;
}

interface DeployCheckpoint {
  readonly title: string;
  readonly value: string;
  readonly detail: string;
  readonly tone: "good" | "active" | "neutral";
}

const emptyServices: ServicesResponse = {
  available: true,
  services: []
};

const emptyRunning: RunningJobsResponse = {
  running: [],
  running_count: 0,
  queued_count: 0
};

const emptyCloudosd: CloudosdJourneyPayload = {
  active_runs: [],
  ready_artifacts: [],
  cloudosd_cache: {
    summary: {
      ready: 0,
      total: 0
    }
  }
};

const finishItems: readonly DeployJourneyItem[] = [
  { label: "Deploy desktop", badge: "OSDCloud", href: "/react/cloudosd", active: true },
  { label: "Deploy server", badge: "OSDeploy", href: "/react/osdeploy" },
  { label: "Use existing VM", badge: "Provision", href: "/react/provision" },
  { label: "Verify machine", badge: "Evidence", href: "/react/vms" },
  { label: "Build media", badge: "Tools", href: "/react/task-engine" },
  { label: "Watch health", badge: "Signals", href: "/react/monitoring" }
];

const journeySteps: readonly DeployJourneyStep[] = [
  {
    title: "Choose deployment path",
    body: "Desktop route is selected. Server and existing VM flows remain one click away.",
    href: "/react/deploy",
    active: true
  },
  {
    title: "Configure VM and media",
    body: "Tenant, node, storage, CPU, memory, disk, and promoted ISO.",
    href: "/react/provision"
  },
  {
    title: "Watch Windows handoff",
    body: "Install, first boot, cleanup, Sysprep OOBE return, and agent events.",
    href: "/react/jobs"
  },
  {
    title: "Verify readiness",
    body: "Hardware hash, Autopilot upload, Intune visibility, and heartbeat proof.",
    href: "/react/vms"
  }
];

const routeShortcuts: readonly DeployJourneyItem[] = [
  { label: "OSDCloud Desktop", badge: "start", href: "/react/cloudosd" },
  { label: "Provision", badge: "configure", href: "/react/provision" },
  { label: "Jobs", badge: "watch", href: "/react/jobs" },
  { label: "VMs", badge: "console", href: "/react/vms" },
  { label: "Hashes", badge: "proof", href: "/react/hashes" },
  { label: "Cloud Devices", badge: "Intune", href: "/react/devices" }
];

function plural(count: number, singular: string, pluralValue = `${singular}s`): string {
  return count === 1 ? singular : pluralValue;
}

function serviceSummary(services: ServicesResponse): string {
  if (!services.available) {
    return "Service inventory unavailable.";
  }
  const readyCount = services.services.filter((service) =>
    ["healthy", "ok", "ready", "running"].includes((service.status || "").toLowerCase())
  ).length;
  if (services.services.length === 0) {
    return "Service inventory reachable.";
  }
  return `${String(readyCount)} ${plural(readyCount, "service")} reporting ready.`;
}

function checkpointTone(services: ServicesResponse, running: RunningJobsResponse): DeployCheckpoint["tone"] {
  if (!services.available) {
    return "neutral";
  }
  return running.running_count > 0 || running.queued_count > 0 ? "active" : "good";
}

function buildCheckpoints(
  services: ServicesResponse,
  running: RunningJobsResponse,
  cloudosd: CloudosdJourneyPayload
): readonly DeployCheckpoint[] {
  const readyArtifacts = cloudosd.ready_artifacts?.length ?? 0;
  const readyCache = cloudosd.cloudosd_cache?.summary?.ready ?? 0;
  const totalCache = cloudosd.cloudosd_cache?.summary?.total ?? 0;

  return [
    {
      title: "Controller and builder ready",
      value: services.available ? "Online" : "Check",
      detail: `${String(running.running_count)} running, ${String(running.queued_count)} queued.`,
      tone: checkpointTone(services, running)
    },
    {
      title: "CloudOSD media promoted",
      value: readyArtifacts > 0 ? "Ready" : "Needed",
      detail: `${String(readyArtifacts)} promoted ${plural(readyArtifacts, "artifact")} available.`,
      tone: readyArtifacts > 0 ? "good" : "neutral"
    },
    {
      title: "Cache health visible",
      value: `${String(readyCache)}/${String(totalCache)}`,
      detail: totalCache > 0 ? "CloudOSD cache entries are reporting status." : "Cache summary has not reported yet.",
      tone: readyCache > 0 ? "good" : "neutral"
    },
    {
      title: "Graph visibility can lag",
      value: "Expect delay",
      detail: "Autopilot upload proof can appear before Intune search catches up.",
      tone: "neutral"
    }
  ];
}

function FinishMenu() {
  return (
    <nav className="deploy-outcome-menu" aria-label="Deploy outcomes">
      <h2>Finish this</h2>
      <div>
        {finishItems.map((item) => (
          <a
            key={item.href}
            className={item.active ? "is-active" : undefined}
            href={item.href}
            aria-label={`${item.label} ${item.badge}`}
          >
            <strong>{item.label}</strong>
            <span>{item.badge}</span>
          </a>
        ))}
      </div>
    </nav>
  );
}

function JourneyStepList() {
  return (
    <section className="deploy-path-board" aria-label="Guided Deploy path">
      {journeySteps.map((step, index) => (
        <a
          key={step.href}
          className={step.active ? "deploy-path-step is-active" : "deploy-path-step"}
          href={step.href}
          aria-label={`Step ${String(index + 1)} ${step.title} ${step.body}`}
        >
          <span>Step {String(index + 1)}</span>
          <strong>{step.title}</strong>
          <small>{step.body}</small>
        </a>
      ))}
    </section>
  );
}

function CheckpointsPanel({ checkpoints, serviceDetail }: { readonly checkpoints: readonly DeployCheckpoint[]; readonly serviceDetail: string }) {
  return (
    <section className="deploy-checkpoint-panel" aria-labelledby="deploy-checkpoints-title">
      <div className="deploy-panel-head">
        <div>
          <p>Live checkpoints</p>
          <h2 id="deploy-checkpoints-title">Readiness from existing APIs</h2>
        </div>
        <span>{serviceDetail}</span>
      </div>
      <div className="deploy-checkpoint-list">
        {checkpoints.map((checkpoint) => (
          <div className={`deploy-checkpoint deploy-checkpoint--${checkpoint.tone}`} key={checkpoint.title}>
            <span>{checkpoint.value}</span>
            <strong>{checkpoint.title}</strong>
            <p>{checkpoint.detail}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function RouteShortcutPanel() {
  return (
    <nav className="deploy-shortcut-panel" aria-label="Route shortcuts">
      <div className="deploy-panel-head">
        <div>
          <p>Shortcuts</p>
          <h2>Jump to the focused page</h2>
        </div>
      </div>
      <div className="deploy-shortcut-grid">
        {routeShortcuts.map((item) => (
          <a key={item.href} href={item.href} aria-label={`${item.label} ${item.badge}`}>
            <strong>{item.label}</strong>
            <span>{item.badge}</span>
          </a>
        ))}
      </div>
    </nav>
  );
}

export function DeployJourneyPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [services, setServices] = useState<ServicesResponse>(emptyServices);
  const [running, setRunning] = useState<RunningJobsResponse>(emptyRunning);
  const [cloudosd, setCloudosd] = useState<CloudosdJourneyPayload>(emptyCloudosd);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const [servicesData, runningData, cloudosdData] = await Promise.all([
        fetchJson<ServicesResponse>("/api/services"),
        fetchJson<RunningJobsResponse>("/api/jobs/running"),
        fetchJson<CloudosdJourneyPayload>("/api/cloudosd/page")
      ]);
      setServices(servicesData);
      setRunning(runningData);
      setCloudosd(cloudosdData);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load Deploy journey status");
    }
  }, []);

  usePolling(load);

  const checkpoints = useMemo(() => buildCheckpoints(services, running, cloudosd), [cloudosd, running, services]);
  const activeRuns = cloudosd.active_runs?.length ?? 0;

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Deploy"
      section="Deploy"
      path="/react/deploy"
      action={
        <div className="page-head__actions">
          <a className="utility-button utility-button--muted" href="/react/runs">View prior runs</a>
          <a className="utility-button" href="/react/provision">Configure run</a>
        </div>
      }
    >
      {error ? <p className="notice" role="status">{error}</p> : null}

      <section className="deploy-journey-shell" aria-labelledby="deploy-journey-title">
        <FinishMenu />
        <div className="deploy-journey-main">
          <div className="deploy-journey-header">
            <div>
              <p>Guided deploy journey</p>
              <h2 id="deploy-journey-title">Deploy desktop: guided path</h2>
              <span>Choose the outcome, configure the machine, watch handoff, then prove readiness.</span>
            </div>
            <dl className="deploy-journey-metrics" aria-label="Deploy activity">
              <div>
                <dt>Active runs</dt>
                <dd>{String(activeRuns)}</dd>
              </div>
              <div>
                <dt>Running jobs</dt>
                <dd>{String(running.running_count)}</dd>
              </div>
              <div>
                <dt>Queued</dt>
                <dd>{String(running.queued_count)}</dd>
              </div>
            </dl>
          </div>

          <JourneyStepList />

          <div className="deploy-work-grid">
            <CheckpointsPanel checkpoints={checkpoints} serviceDetail={serviceSummary(services)} />
            <RouteShortcutPanel />
          </div>
        </div>
      </section>
    </PageFrame>
  );
}
