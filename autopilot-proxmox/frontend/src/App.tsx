import type { AppBootstrap } from "./contracts";
import { migratedRoutes } from "./routes";

interface AppProps {
  readonly bootstrap: AppBootstrap;
}

export function App({ bootstrap }: AppProps) {
  const buildLabel = bootstrap.buildSha ? `Build ${bootstrap.buildSha}` : "Build unknown";

  return (
    <main className="shell">
      <section className="shell__hero" aria-labelledby="shell-title">
        <div>
          <p className="shell__eyebrow">React shell foundation</p>
          <h1 id="shell-title">Proxmox VE Autopilot</h1>
          <p className="shell__copy">
            The authenticated React runtime is mounted. Operational pages remain on the existing
            Jinja console until each route passes parity checks.
          </p>
        </div>
        <div className="shell__status" aria-label="Build status">
          <span>{buildLabel}</span>
          {bootstrap.buildTime ? <time dateTime={bootstrap.buildTime}>{bootstrap.buildTime}</time> : null}
        </div>
      </section>

      <section className="shell__panel" aria-labelledby="routes-title">
        <h2 id="routes-title">Migrated routes</h2>
        <ul>
          {migratedRoutes.map((route) => (
            <li key={route.path}>
              <a href={route.path}>{route.label}</a>
              <span>{route.phase}</span>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
