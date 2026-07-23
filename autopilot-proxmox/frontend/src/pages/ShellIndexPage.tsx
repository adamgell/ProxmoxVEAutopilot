import type { AppBootstrap } from "../contracts";
import { OperatorShell } from "../components/Shell";
import { OperatorRouteMap, OutcomeCardGrid, QuickRouteLane } from "../components/OutcomeNavigation";
import { operatorNavGroups, operatorOutcomes, operatorQuickRoutes } from "../routes";

function OnboardingHero({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const status = bootstrap.onboarding?.status ?? "absent";
  if (status === "absent" || status === "complete" || status === "aborted") {
    return null;
  }
  if (status === "launched") {
    return (
      <a className="onboarding-resume__monitor" href="/react/onboarding/setup">
        Resume the setup monitor
      </a>
    );
  }
  // status === 'pending' | 'in_progress'
  return (
    <section className="onboarding-resume" aria-label="Resume onboarding">
      <div>
        <strong>Resume onboarding</strong>
        <span>You started the setup wizard but did not finish.</span>
      </div>
      <a className="onboarding-resume__cta" href="/react/onboarding">Resume</a>
    </section>
  );
}

export function ShellIndexPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  return (
    <OperatorShell bootstrap={bootstrap} path="/react-shell">
      <OnboardingHero bootstrap={bootstrap} />
      <section className="control-room-hero" aria-labelledby="control-room-title">
        <div>
          <h1 id="control-room-title">What are you trying to finish?</h1>
          <p>
            Pick the operator outcome first. The menu routes to the right surface after that:
            deployment runs, lab networks, build tools, fleet proof, live jobs, or settings.
          </p>
        </div>
        <aside className="suggested-next" aria-label="Suggested next step">
          <h2>Suggested next step</h2>
          <a href="/react/deploy"><span>Open guided Deploy path</span><strong>Deploy</strong></a>
          <a href="/react/labs" aria-label="Create managed lab Labs"><span>Create managed lab</span><strong>Labs</strong></a>
          <a href="/react/networks"><span>Check lab network scope</span><strong>Infra</strong></a>
          <a href="/react/vms"><span>Check VM evidence</span><strong>Watch</strong></a>
        </aside>
      </section>
      <OperatorRouteMap groups={operatorNavGroups} />
      <OutcomeCardGrid outcomes={operatorOutcomes} />
      <QuickRouteLane quickRoutes={operatorQuickRoutes} />
    </OperatorShell>
  );
}
