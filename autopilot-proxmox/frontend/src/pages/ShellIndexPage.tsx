import type { AppBootstrap, OperatorFlow, OperatorFlowStep } from "../contracts";
import { OperatorShell } from "../components/Shell";
import { Panel } from "../components/ui";
import { migratedRoutes, operatorFlows, operatorNavGroups } from "../routes";

function OnboardingHero({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const status = bootstrap.onboarding?.status ?? "absent";
  if (status === "absent" || status === "complete" || status === "aborted") {
    return null;
  }
  if (status === "launched") {
    return (
      <a className="onboarding-resume-link" href="/react/onboarding/setup">
        Resume setup monitor
      </a>
    );
  }
  // status === 'pending' | 'in_progress'
  return (
    <section className="onboarding-hero" aria-label="Onboarding">
      <h2>Resume onboarding</h2>
      <p>You started the onboarding wizard but did not finish. Pick up where you left off.</p>
      <a className="onboarding-hero-cta" href="/react/onboarding">
        Resume onboarding
      </a>
    </section>
  );
}

function routeCount(label: string): string {
  const group = operatorNavGroups.find((item) => item.label === label);
  if (!group) {
    return "0";
  }
  return String(group.items.length);
}

function stepClass(step: OperatorFlowStep): string {
  return step.state === "React" ? "flow-step" : "flow-step flow-step--legacy";
}

function FlowCard({ flow }: { readonly flow: OperatorFlow }) {
  return (
    <Panel title={flow.label}>
      <div className="flow-card">
        <p>{flow.summary}</p>
        <ol>
          {flow.steps.map((step) => (
            <li key={`${flow.id}-${step.href}-${step.label}`}>
              <a className={stepClass(step)} href={step.href}>
                <span>{step.label}</span>
                <small>{step.state}</small>
              </a>
            </li>
          ))}
        </ol>
      </div>
    </Panel>
  );
}

export function ShellIndexPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  return (
    <OperatorShell bootstrap={bootstrap} path="/react-shell">
      <OnboardingHero bootstrap={bootstrap} />
      <section className="page-head" aria-labelledby="shell-title">
        <div>
          <p>Operator map</p>
          <h1 id="shell-title">Proxmox VE Autopilot</h1>
        </div>
        <a className="action-link" href="/react/dashboard">Dashboard</a>
      </section>

      <section className="metric-strip metric-strip--workspace" aria-label="Operator map totals">
        <div>
          <span>React</span>
          <strong>{String(migratedRoutes.length)}</strong>
        </div>
        <div>
          <span>Deploy</span>
          <strong>{routeCount("Deploy")}</strong>
        </div>
        <div>
          <span>Build</span>
          <strong>{routeCount("Build")}</strong>
        </div>
        <div>
          <span>Fleet</span>
          <strong>{routeCount("Fleet")}</strong>
        </div>
      </section>

      <section className="flow-board" aria-label="Operator flows">
        {operatorFlows.map((flow) => (
          <FlowCard key={flow.id} flow={flow} />
        ))}
      </section>
    </OperatorShell>
  );
}
