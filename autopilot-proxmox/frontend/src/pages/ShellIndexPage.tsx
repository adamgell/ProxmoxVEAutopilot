import type { AppBootstrap } from "../contracts";
import { OperatorShell } from "../components/Shell";
import { OperatorRouteMap, OutcomeCardGrid, QuickRouteLane } from "../components/OutcomeNavigation";
import { operatorNavGroups, operatorOutcomes, operatorQuickRoutes } from "../routes";

export function ShellIndexPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  return (
    <OperatorShell bootstrap={bootstrap} path="/react-shell">
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
          <a href="/react/provision"><span>Open Provision launch</span><strong>Deploy</strong></a>
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
