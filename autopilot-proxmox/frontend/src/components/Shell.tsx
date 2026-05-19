import type { ReactNode } from "react";

import type { AppBootstrap } from "../contracts";
import { migratedRoutes, operatorNavGroups, reactRouteForPath } from "../routes";
import { Panel } from "./ui";

function currentPageLabel(path: string): string {
  return reactRouteForPath(path)?.label ?? "Shell";
}

export function OperatorShell({
  bootstrap,
  path,
  socketState,
  children
}: {
  readonly bootstrap: AppBootstrap;
  readonly path: string;
  readonly socketState?: string | undefined;
  readonly children: ReactNode;
}) {
  const buildLabel = bootstrap.buildSha ? `Build ${bootstrap.buildSha}` : "Build unknown";
  const pageLabel = currentPageLabel(path);

  return (
    <div className="workspace">
      <aside className="workspace__rail">
        <a className="workspace__brand" href="/react/dashboard" aria-label="Proxmox VE Autopilot dashboard">
          <span>Autopilot</span>
          <small>Operator</small>
        </a>
        <nav className="workspace__nav" aria-label="Operator workspace">
          {operatorNavGroups.map((group) => (
            <section key={group.label} aria-labelledby={`nav-${group.label.toLowerCase()}`}>
              <h2 id={`nav-${group.label.toLowerCase()}`}>{group.label}</h2>
              {group.items.map((item) => (
                <a
                  key={item.path}
                  className={[
                    item.path === path ? "is-current" : "",
                    item.legacy ? "is-legacy" : ""
                  ].filter(Boolean).join(" ")}
                  href={item.path}
                  aria-label={item.legacy ? `${item.label} legacy page` : item.label}
                  aria-current={item.path === path ? "page" : undefined}
                >
                  <span>{item.label}</span>
                  {item.legacy ? <small>Jinja</small> : null}
                </a>
              ))}
            </section>
          ))}
        </nav>
      </aside>

      <div className="workspace__main">
        <header className="workspace__topbar">
          <div>
            <span className="workspace__kicker">React operator console</span>
            <strong>{pageLabel}</strong>
          </div>
          <div className="workspace__status" aria-label="Runtime status">
            {socketState ? <span className={`socket-state socket-state--${socketState}`}>Live {socketState}</span> : null}
            <span>{buildLabel}</span>
            {bootstrap.buildTime ? <time dateTime={bootstrap.buildTime}>{bootstrap.buildTime}</time> : null}
          </div>
        </header>
        <main className="workspace__content">{children}</main>
      </div>
    </div>
  );
}

export function ShellIndex({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  return (
    <OperatorShell bootstrap={bootstrap} path="/react-shell">
      <section className="page-head" aria-labelledby="shell-title">
        <div>
          <p>Observe</p>
          <h1 id="shell-title">Proxmox VE Autopilot</h1>
        </div>
        <a className="action-link" href="/">Jinja console</a>
      </section>

      <section className="metric-strip" aria-label="Migrated routes">
        {migratedRoutes.map((route) => (
          <a key={route.path} href={route.path} aria-label={`Open ${route.label}`}>
            <span>{route.group}</span>
            <strong>{route.label}</strong>
          </a>
        ))}
      </section>

      <section className="section-grid">
        {operatorNavGroups.map((group) => (
          <Panel key={group.label} title={group.label}>
            <div className="link-stack">
              {group.items.map((item) => (
                <a
                  key={item.path}
                  href={item.path}
                  className={item.legacy ? "legacy-link" : undefined}
                  aria-label={item.legacy ? item.label : `${item.label} route`}
                >
                  <span>{item.label}</span>
                  {item.legacy ? <small>existing page</small> : <small>{item.phase}</small>}
                </a>
              ))}
            </div>
          </Panel>
        ))}
      </section>
    </OperatorShell>
  );
}

interface PageFrameProps {
  readonly bootstrap: AppBootstrap;
  readonly title: string;
  readonly section: string;
  readonly path: string;
  readonly children: ReactNode;
  readonly socketState?: string;
  readonly action?: ReactNode;
}

export function PageFrame({ bootstrap, title, section, path, children, socketState, action }: PageFrameProps) {
  return (
    <OperatorShell bootstrap={bootstrap} path={path} socketState={socketState}>
      <header className="page-head">
        <div>
          <p>{section}</p>
          <h1>{title}</h1>
        </div>
        {action}
      </header>
      {children}
    </OperatorShell>
  );
}
