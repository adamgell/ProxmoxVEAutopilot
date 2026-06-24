import { LogOut, Search } from "lucide-react";
import type { FormEvent } from "react";

import type {
  AppBootstrap,
  OperatorMode,
  OperatorModeId,
  OperatorNavGroup,
  OperatorOutcome,
  OperatorQuickRoute
} from "../contracts";
import { formatShortDateTime } from "../viewModels";

export function OutcomeModeRail({
  modes,
  activeMode
}: {
  readonly modes: readonly OperatorMode[];
  readonly activeMode: OperatorModeId;
}) {
  return (
    <nav className="outcome-rail" aria-label="Outcome modes">
      {modes.map((mode) => {
        const isActive = mode.id === activeMode;
        return (
          <a
            key={mode.id}
            className={isActive ? "is-active" : undefined}
            href={mode.href}
            aria-current={isActive ? "page" : undefined}
            title={mode.longLabel}
          >
            {mode.label}
          </a>
        );
      })}
    </nav>
  );
}

export function OperatorTopBar({
  bootstrap,
  query,
  onQueryChange,
  onSubmit
}: {
  readonly bootstrap: AppBootstrap;
  readonly query: string;
  readonly onQueryChange: (value: string) => void;
  readonly onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const userLabel = bootstrap.userName || bootstrap.userEmail || "Signed in";

  return (
    <header className="outcome-topbar" aria-label="Global console status">
      <a className="outcome-brand" href="/react-shell" aria-label="Proxmox VE Autopilot home">
        <span className="outcome-brand__mark" aria-hidden="true" />
        <span>
          <strong>Proxmox VE Autopilot</strong>
          <small>Control room</small>
        </span>
      </a>
      <form className="outcome-command" role="search" onSubmit={onSubmit}>
        <Search aria-hidden="true" focusable="false" size={16} strokeWidth={2.4} />
        <input
          type="search"
          value={query}
          onChange={(event) => {
            onQueryChange(event.currentTarget.value);
          }}
          placeholder="Type a VM, job, serial, route, or run ID"
          aria-label="Search console"
        />
      </form>
      <div className="outcome-operator">
        <span className="outcome-user" title={bootstrap.userEmail || userLabel}>
          {userLabel}
        </span>
        <a className="outcome-logout" href="/auth/logout" aria-label={`Log out ${userLabel}`}>
          <LogOut aria-hidden="true" focusable="false" size={16} strokeWidth={2.4} />
          <span>Log out</span>
        </a>
      </div>
    </header>
  );
}

export function OutcomeCardGrid({ outcomes }: { readonly outcomes: readonly OperatorOutcome[] }) {
  return (
    <section className="outcome-card-grid" aria-label="Operator outcomes">
      {outcomes.map((outcome) => (
        <article key={outcome.id} className={`outcome-card outcome-card--${outcome.tone}`} data-tone={outcome.tone}>
          <span className={`outcome-pill outcome-pill--${outcome.tone}`}>{outcome.eyebrow}</span>
          <h2>{outcome.title}</h2>
          <p>{outcome.summary}</p>
          <a className="outcome-card__primary" href={outcome.primaryHref}>
            {outcome.actionLabel}
          </a>
          <div className="outcome-card__routes" aria-label={`${outcome.title} related routes`}>
            {outcome.relatedRoutes.map((route) => (
              <a
                key={`${outcome.id}-${route.href}-${route.label}`}
                href={route.href}
                aria-label={route.label}
              >
                <strong>{route.label}</strong>
                <span aria-hidden="true">{route.purpose}</span>
              </a>
            ))}
          </div>
        </article>
      ))}
    </section>
  );
}

export function QuickRouteLane({ quickRoutes }: { readonly quickRoutes: readonly OperatorQuickRoute[] }) {
  return (
    <nav className="quick-route-lane" aria-label="Quick routes">
      {quickRoutes.map((route) => (
        <a key={`${route.href}-${route.label}`} href={route.href}>
          <strong>{route.label}</strong>
          {" "}
          <span>{route.summary}</span>
        </a>
      ))}
    </nav>
  );
}

export function OperatorRouteMap({ groups }: { readonly groups: readonly OperatorNavGroup[] }) {
  return (
    <nav className="operator-route-map" aria-label="Route map">
      {groups.map((group) => {
        const activeItems = group.items.filter((item) => item.active);
        if (activeItems.length === 0) {
          return null;
        }
        return (
          <article key={group.label} className="operator-route-group" role="group" aria-label={group.label}>
            <h2>{group.label}</h2>
            <div className="operator-route-group__links">
              {activeItems.map((route) => {
                const hasConcretePath = !route.path.includes(":");
                const phaseLabel = route.phase === "legacy" ? "read-only" : route.phase;
                if (!hasConcretePath) {
                  return (
                    <div key={`${group.label}-${route.path}`} className="operator-route-detail">
                      <strong>{route.label}</strong>
                      <span>detail</span>
                    </div>
                  );
                }
                return (
                  <a key={`${group.label}-${route.path}`} href={route.path} aria-label={`${route.label} ${phaseLabel}`}>
                    <strong>{route.label}</strong>
                    <span>{phaseLabel}</span>
                  </a>
                );
              })}
            </div>
          </article>
        );
      })}
    </nav>
  );
}

export function SystemTray({
  bootstrap,
  socketState
}: {
  readonly bootstrap: AppBootstrap;
  readonly socketState?: string | undefined;
}) {
  const buildLabel = bootstrap.buildSha ? `Build ${bootstrap.buildSha}` : "Build unknown";

  return (
    <aside className="outcome-system-tray" aria-label="Runtime status">
      {socketState ? <span className={`socket-state socket-state--${socketState}`}>Live {socketState}</span> : null}
      <span>{buildLabel}</span>
      {bootstrap.buildTime ? (
        <time dateTime={bootstrap.buildTime}>{formatShortDateTime(bootstrap.buildTime)}</time>
      ) : null}
    </aside>
  );
}
