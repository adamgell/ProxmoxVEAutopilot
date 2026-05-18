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
  readonly payload?: unknown;
}
