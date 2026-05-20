import type { ReactNode } from "react";

import type { AppBootstrap } from "../contracts";
import { operatorNavGroups, reactRouteForPath } from "../routes";
import { formatShortDateTime } from "../viewModels";

function currentPageLabel(path: string): string {
  if (path === "/react/credentials/new") {
    return "New Credential";
  }
  if (/^\/react\/credentials\/\d+\/edit$/u.test(path)) {
    return "Edit Credential";
  }
  if (/^\/react\/vms\/\d+$/u.test(path)) {
    return "VM Detail";
  }
  return reactRouteForPath(path)?.label ?? "Shell";
}

function legacyPathForReactPath(path: string): string {
  if (path === "/react/jobs") {
    return "/legacy/jobs";
  }
  if (path === "/react/monitoring") {
    return "/monitoring";
  }
  if (path === "/react/vms") {
    return "/legacy/vms";
  }
  const vmMatch = /^\/react\/vms\/(\d+)$/u.exec(path);
  const vmid = vmMatch?.[1];
  if (vmid) {
    return `/legacy/devices/${vmid}`;
  }
  if (path === "/react/legacy-vms") {
    return "/legacy/vms";
  }
  if (path === "/react/devices") {
    return "/legacy/cloud";
  }
  if (path === "/react/hashes") {
    return "/legacy/hashes";
  }
  if (path === "/react/files") {
    return "/legacy/files";
  }
  if (path === "/react/settings") {
    return "/legacy/settings";
  }
  if (path === "/react/credentials") {
    return "/legacy/credentials";
  }
  if (path === "/react/credentials/new") {
    return "/legacy/credentials/new";
  }
  const credMatch = /^\/react\/credentials\/(\d+)\/edit$/u.exec(path);
  const credentialId = credMatch?.[1];
  if (credentialId) {
    return `/legacy/credentials/${credentialId}/edit`;
  }
  if (path === "/react/monitoring/settings") {
    return "/legacy/monitoring/settings";
  }
  return "/legacy/dashboard";
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
  const legacyPath = legacyPathForReactPath(path);

  return (
    <div className="workspace">
      <a className="skip-link" href="#react-content">Skip to content</a>
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
            <a className="ui-mode-switch" href={legacyPath} aria-label="Switch to Legacy UI">Legacy UI</a>
            {socketState ? <span className={`socket-state socket-state--${socketState}`}>Live {socketState}</span> : null}
            <span>{buildLabel}</span>
            {bootstrap.buildTime ? (
              <time dateTime={bootstrap.buildTime}>{formatShortDateTime(bootstrap.buildTime)}</time>
            ) : null}
          </div>
        </header>
        <main id="react-content" className="workspace__content" tabIndex={-1}>{children}</main>
      </div>
    </div>
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
