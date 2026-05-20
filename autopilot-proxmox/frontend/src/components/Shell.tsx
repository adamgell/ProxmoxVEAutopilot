import { useId, useState, type FormEvent, type ReactNode } from "react";
import { LogOut, Search } from "lucide-react";

import type { AppBootstrap } from "../contracts";
import { operatorNavGroups } from "../routes";
import { formatShortDateTime } from "../viewModels";

function legacyPathForReactPath(path: string): string {
  if (path === "/react/jobs") {
    return "/legacy/jobs";
  }
  if (path === "/react/monitoring") {
    return "/monitoring";
  }
  if (path === "/react/vms" || /^\/react\/vms\/\d+$/u.test(path)) {
    return "/legacy/vms";
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
  const legacyPath = legacyPathForReactPath(path);
  const commandId = useId();
  const [commandQuery, setCommandQuery] = useState("");
  const routes = operatorNavGroups.flatMap((group) => group.items);
  const userLabel = bootstrap.userName || bootstrap.userEmail || "Signed in";

  function submitCommandSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = commandQuery.trim();
    if (!query) {
      return;
    }
    const normalized = query.toLowerCase();
    const route = routes.find((item) => item.label.toLowerCase() === normalized)
      ?? routes.find((item) => item.label.toLowerCase().includes(normalized));
    if (route) {
      window.location.assign(route.path);
      return;
    }
    if (/^\d+$/u.test(query)) {
      window.location.assign(`/react/vms/${query}`);
      return;
    }
    window.location.assign(`/react/vms?search=${encodeURIComponent(query)}`);
  }

  return (
    <div className="workspace">
      <a className="skip-link" href="#react-content">Skip to content</a>
      <header className="workspace__globalbar" aria-label="Global console status">
        <a className="workspace__global-brand" href="/react/dashboard" aria-label="Proxmox VE Autopilot dashboard">
          <span className="workspace__brand-mark" aria-hidden="true">
            <svg viewBox="0 0 64 64" focusable="false">
              <rect x="0" y="0" width="64" height="64" rx="12" ry="12" />
              <g>
                <polyline className="workspace__brand-mark-red" points="14,22 32,10 50,22" />
                <polyline className="workspace__brand-mark-green" points="14,38 32,26 50,38" />
                <polyline className="workspace__brand-mark-blue" points="14,54 32,42 50,54" />
              </g>
            </svg>
          </span>
          <span>
            <strong>Proxmox VE Autopilot</strong>
            <small>Operator console</small>
          </span>
        </a>
        <form className="workspace__command" role="search" onSubmit={submitCommandSearch}>
          <label className="sr-only" htmlFor={commandId}>Search console</label>
          <Search aria-hidden="true" focusable="false" size={16} strokeWidth={2.4} />
          <input
            id={commandId}
            type="search"
            list={`${commandId}-routes`}
            value={commandQuery}
            onChange={(event) => setCommandQuery(event.currentTarget.value)}
            placeholder="Search routes, VMs, jobs"
            aria-label="Search console"
          />
          <datalist id={`${commandId}-routes`}>
            {routes.map((route) => <option key={route.path} value={route.label} />)}
          </datalist>
        </form>
        <div className="workspace__operator">
          <span className="workspace__user" title={bootstrap.userEmail || userLabel}>
            <span aria-hidden="true">{userLabel.slice(0, 1).toUpperCase()}</span>
            <strong>{userLabel}</strong>
          </span>
          <a className="workspace__logout" href="/auth/logout" aria-label={`Log out ${userLabel}`}>
            <LogOut aria-hidden="true" focusable="false" size={16} strokeWidth={2.4} />
            <span>Log out</span>
          </a>
        </div>
      </header>
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
        <main id="react-content" className="workspace__content" tabIndex={-1}>{children}</main>
      </div>
      <aside className="workspace__system-tray" aria-label="Runtime status">
        <a className="ui-mode-switch" href={legacyPath} aria-label="Switch to Legacy UI">Legacy UI</a>
        {socketState ? <span className={`socket-state socket-state--${socketState}`}>Live {socketState}</span> : null}
        <span>{buildLabel}</span>
        {bootstrap.buildTime ? (
          <time dateTime={bootstrap.buildTime}>{formatShortDateTime(bootstrap.buildTime)}</time>
        ) : null}
      </aside>
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
