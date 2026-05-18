export interface AppBootstrap {
  readonly buildSha?: string;
  readonly buildTime?: string;
}

export interface MigratedRoute {
  readonly path: string;
  readonly label: string;
  readonly phase: "foundation" | "read-only" | "operational";
}

export interface LiveSocketMessage {
  readonly topic?: string;
  readonly type?: string;
  readonly data?: unknown;
  readonly payload?: unknown;
}

export interface ServiceHealth {
  readonly service?: string;
  readonly service_id?: string;
  readonly service_type?: string;
  readonly status?: string;
  readonly age_seconds?: number | null;
  readonly detail?: string;
  readonly message?: string;
}

export interface ServicesResponse {
  readonly services: readonly ServiceHealth[];
  readonly available: boolean;
  readonly error?: string;
}

export interface RunningJob {
  readonly id: string;
  readonly playbook?: string;
  readonly target?: string;
  readonly started?: string | null;
  readonly elapsed_seconds?: number;
  readonly progress_pct?: number;
  readonly paused?: boolean;
}

export interface RunningJobsResponse {
  readonly running: readonly RunningJob[];
  readonly running_count: number;
  readonly queued_count: number;
}

export interface RecentJob {
  readonly id: string;
  readonly playbook?: string | null;
  readonly status?: string | null;
  readonly started?: string | null;
  readonly ended?: string | null;
  readonly duration?: string | null;
  readonly target?: string;
}

export interface RecentJobsResponse {
  readonly jobs: readonly RecentJob[];
}

export interface JobTableRow {
  readonly id: string;
  readonly playbook?: string | null;
  readonly status?: string | null;
  readonly started?: string | null;
  readonly ended?: string | null;
  readonly duration?: string | null;
  readonly args?: Readonly<Record<string, unknown>>;
  readonly paused?: boolean;
}

export interface JobsTableResponse {
  readonly jobs: readonly JobTableRow[];
}

export interface FleetSummary {
  readonly total: number;
  readonly ad_joined_pct?: number;
  readonly autopilot_pct?: number;
  readonly intune_pct?: number;
  readonly mde_pct?: number;
  readonly [key: string]: unknown;
}

export interface MonitoringSummary {
  readonly devices: number;
  readonly ad: number;
  readonly entra: number;
  readonly intune: number;
}

export interface CockpitSummary {
  readonly readiness_score: number;
  readonly jobs: RunningJobsResponse;
  readonly recent_jobs: readonly RecentJob[];
  readonly services: ServicesResponse;
  readonly fleet: FleetSummary;
  readonly monitoring: MonitoringSummary;
}

export interface JobsLivePayload {
  readonly running?: RunningJobsResponse;
  readonly recent?: RecentJobsResponse;
  readonly table?: JobsTableResponse;
  readonly generated_at?: string;
}
