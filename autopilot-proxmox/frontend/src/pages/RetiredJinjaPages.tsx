import { useCallback, useMemo, useState } from "react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, OperatorGroupLabel } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { textValue } from "../utilityModels";

type PagePayload = Readonly<Record<string, unknown>>;

interface RetiredPageConfig {
  readonly title: string;
  readonly section: OperatorGroupLabel;
  readonly path: string;
  readonly endpoint: (path: string) => string;
  readonly primaryKeys: readonly string[];
}

function asRecord(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Readonly<Record<string, unknown>> : {};
}

function asArray(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

function rowKey(row: unknown, index: number): string {
  const record = asRecord(row);
  return textValue(record.id ?? record.run_id ?? record.name ?? record.item_id ?? record.filename ?? record.path, String(index));
}

function labelForRecord(record: Readonly<Record<string, unknown>>): string {
  return textValue(
    record.name
    ?? record.label
    ?? record.display_name
    ?? record.requested_vm_name
    ?? record.vm_name
    ?? record.run_id
    ?? record.id
    ?? record.item_id
    ?? record.kind
    ?? record.filename
    ?? record.path,
    "item"
  );
}

function summaryForRecord(record: Readonly<Record<string, unknown>>): string {
  return textValue(
    record.status
    ?? record.state
    ?? record.detail
    ?? record.target_os
    ?? record.phase
    ?? record.path
    ?? record.filename
    ?? record.description,
    "-"
  );
}

function PanelList({ title, value }: { readonly title: string; readonly value: unknown }) {
  const rows = asArray(value);
  if (!rows.length) {
    return (
      <Panel title={title}>
        <p className="empty">No {title.toLowerCase()}.</p>
      </Panel>
    );
  }
  return (
    <Panel title={title}>
      <div className="table-wrap">
        <table className="jobs-table utility-table">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">State</th>
              <th scope="col">ID</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => {
              const record = asRecord(row);
              return (
                <tr key={rowKey(row, index)}>
                  <td>{labelForRecord(record)}</td>
                  <td>{summaryForRecord(record)}</td>
                  <td>{textValue(record.id ?? record.run_id ?? record.item_id ?? record.vmid)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function DetailPanel({ payload }: { readonly payload: PagePayload }) {
  const detail = asRecord(payload.job ?? payload.run ?? payload.sequence ?? payload.template ?? payload.tracking);
  if (!Object.keys(detail).length) {
    return null;
  }
  return (
    <Panel title="Detail">
      <dl className="utility-definition-grid">
        {Object.entries(detail).slice(0, 12).map(([key, value]) => (
          <div key={key}>
            <dt>{key.replaceAll("_", " ")}</dt>
            <dd>{textValue(typeof value === "object" ? JSON.stringify(value) : value)}</dd>
          </div>
        ))}
      </dl>
    </Panel>
  );
}

export function RetiredJinjaPage({
  bootstrap,
  config
}: {
  readonly bootstrap: AppBootstrap;
  readonly config: RetiredPageConfig;
}) {
  const [payload, setPayload] = useState<PagePayload>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<PagePayload>(config.endpoint(window.location.pathname)));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load page");
    } finally {
      setLoading(false);
    }
  }, [config]);

  usePolling(load);

  const metrics = useMemo(() => config.primaryKeys.map((key) => {
    const value = payload[key];
    return {
      label: key.replaceAll("_", " "),
      value: Array.isArray(value) ? String(value.length) : textValue(value)
    };
  }), [config.primaryKeys, payload]);

  return (
    <PageFrame bootstrap={bootstrap} title={config.title} section={config.section} path={config.path}>
      {loading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading {config.title}</span>
          <div className="load-strip__track" role="progressbar" aria-label={`${config.title} loading`}>
            <span />
          </div>
        </div>
      ) : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label={`${config.title} metrics`}>
        {metrics.map((metric) => <Metric key={metric.label} label={metric.label} value={metric.value} />)}
      </section>
      <DetailPanel payload={payload} />
      <section className="utility-settings-grid">
        {config.primaryKeys.map((key) => (
          <PanelList key={key} title={key.replaceAll("_", " ")} value={payload[key]} />
        ))}
      </section>
    </PageFrame>
  );
}

export const retiredPageConfigs = {
  installTracking: {
    title: "Install Tracking",
    section: "Observe",
    path: "/react/install-tracking",
    endpoint: () => "/api/install-tracking/page",
    primaryKeys: ["items", "runs"]
  },
  provision: {
    title: "Provision",
    section: "Deploy",
    path: "/react/provision",
    endpoint: () => "/api/provision/page",
    primaryKeys: ["sequences", "cloudosd_artifacts", "osdeploy_artifacts", "ubuntu_v2_sequences"]
  },
  cloudosd: {
    title: "OSDCloud Desktop",
    section: "Deploy",
    path: "/react/cloudosd",
    endpoint: () => `/api/cloudosd/page${window.location.search}`,
    primaryKeys: ["runs", "artifacts", "active_runs", "stale_failed_runs"]
  },
  osdeploy: {
    title: "OSDeploy Server",
    section: "Deploy",
    path: "/react/osdeploy",
    endpoint: () => `/api/osdeploy/page${window.location.search}`,
    primaryKeys: ["runs", "artifacts", "active_runs", "stale_failed_runs"]
  },
  template: {
    title: "Template",
    section: "Build",
    path: "/react/template",
    endpoint: () => "/api/template/page",
    primaryKeys: ["profiles", "ubuntu_sequences"]
  },
  runs: {
    title: "Runs",
    section: "Observe",
    path: "/react/runs",
    endpoint: () => "/api/runs/page",
    primaryKeys: ["runs"]
  },
  taskEngine: {
    title: "Task Engine",
    section: "Build",
    path: "/react/task-engine",
    endpoint: () => "/api/task-engine/page",
    primaryKeys: ["sequences", "runs", "flow_templates", "content_items"]
  },
  answerIsos: {
    title: "Answer ISOs",
    section: "Build",
    path: "/react/answer-isos",
    endpoint: () => "/api/answer-isos/page",
    primaryKeys: ["rows"]
  },
  sequences: {
    title: "Sequences",
    section: "Fleet",
    path: "/react/sequences",
    endpoint: () => "/api/sequences/page",
    primaryKeys: ["sequences"]
  },
  utmVms: {
    title: "UTM VMs",
    section: "Fleet",
    path: "/react/utm-vms",
    endpoint: () => "/api/utm-vms/page",
    primaryKeys: ["vms", "isos"]
  }
} as const satisfies Readonly<Record<string, RetiredPageConfig>>;

export function retiredConfigForPath(path: string): RetiredPageConfig | undefined {
  const jobMatch = /^\/react\/jobs\/([^/]+)$/u.exec(path);
  if (jobMatch?.[1]) {
    const jobId = jobMatch[1];
    return {
      title: "Job Detail",
      section: "Observe",
      path,
      endpoint: () => `/api/jobs/${encodeURIComponent(jobId)}/page`,
      primaryKeys: ["log"]
    };
  }
  const cloudRunMatch = /^\/react\/cloudosd\/runs\/([^/]+)$/u.exec(path);
  if (cloudRunMatch?.[1]) {
    const runId = cloudRunMatch[1];
    return {
      title: "OSDCloud Run",
      section: "Deploy",
      path,
      endpoint: () => `/api/cloudosd/runs/${encodeURIComponent(runId)}/page`,
      primaryKeys: ["events", "v2_steps", "related_jobs"]
    };
  }
  const osdeployRunMatch = /^\/react\/osdeploy\/runs\/([^/]+)$/u.exec(path);
  if (osdeployRunMatch?.[1]) {
    const runId = osdeployRunMatch[1];
    return {
      title: "OSDeploy Run",
      section: "Deploy",
      path,
      endpoint: () => `/api/osdeploy/runs/${encodeURIComponent(runId)}/page`,
      primaryKeys: ["events", "v2_steps"]
    };
  }
  const runMatch = /^\/react\/runs\/(\d+)$/u.exec(path);
  if (runMatch?.[1]) {
    const runId = runMatch[1];
    return {
      title: "Run Detail",
      section: "Observe",
      path,
      endpoint: () => `/api/runs/${encodeURIComponent(runId)}/page`,
      primaryKeys: ["steps"]
    };
  }
  if (path === "/react/task-engine/sequences/list") {
    return {
      title: "Task Sequences",
      section: "Build",
      path,
      endpoint: () => `/api/task-engine/sequences/list/page${window.location.search}`,
      primaryKeys: ["sequences", "flow_templates"]
    };
  }
  if (path === "/react/task-engine/sequences/new") {
    return {
      title: "New Task Sequence",
      section: "Build",
      path,
      endpoint: () => `/api/task-engine/sequences/new/page${window.location.search}`,
      primaryKeys: ["nodes", "step_templates", "legacy_sequences"]
    };
  }
  const taskTemplateMatch = /^\/react\/task-engine\/sequences\/templates\/([^/]+)$/u.exec(path);
  if (taskTemplateMatch?.[1]) {
    const templateId = taskTemplateMatch[1];
    return {
      title: "Task Template",
      section: "Build",
      path,
      endpoint: () => `/api/task-engine/sequences/templates/${encodeURIComponent(templateId)}/page`,
      primaryKeys: ["template"]
    };
  }
  const taskEditMatch = /^\/react\/task-engine\/sequences\/([^/]+)\/edit$/u.exec(path);
  if (taskEditMatch?.[1]) {
    const sequenceId = taskEditMatch[1];
    return {
      title: "Edit Task Sequence",
      section: "Build",
      path,
      endpoint: () => `/api/task-engine/sequences/${encodeURIComponent(sequenceId)}/edit/page`,
      primaryKeys: ["nodes", "step_templates", "legacy_sequences"]
    };
  }
  if (path === "/react/sequences/new") {
    return {
      title: "New Sequence",
      section: "Fleet",
      path,
      endpoint: () => "/api/sequences/new/page",
      primaryKeys: ["oem_profiles"]
    };
  }
  const sequenceEditMatch = /^\/react\/sequences\/(\d+)\/edit$/u.exec(path);
  if (sequenceEditMatch?.[1]) {
    const sequenceId = sequenceEditMatch[1];
    return {
      title: "Edit Sequence",
      section: "Fleet",
      path,
      endpoint: () => `/api/sequences/${encodeURIComponent(sequenceId)}/edit/page`,
      primaryKeys: ["oem_profiles"]
    };
  }
  return Object.values(retiredPageConfigs).find((config) => config.path === path);
}
