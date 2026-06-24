import { useState, type FormEvent, type ReactNode } from "react";

import type { AppBootstrap } from "../contracts";
import { resolveCommandTarget } from "../navigation";
import { modeForPath, operatorModes, routeSearchTargets } from "../routes";
import { OperatorTopBar, OutcomeModeRail, SystemTray } from "./OutcomeNavigation";

export const shellNavigator = {
  assign(target: string) {
    window.location.assign(target);
  }
};

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
  const [commandQuery, setCommandQuery] = useState("");
  const activeMode = modeForPath(path);

  function submitCommandSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const target = resolveCommandTarget(commandQuery, routeSearchTargets);
    if (target) {
      shellNavigator.assign(target);
    }
  }

  return (
    <div className="workspace workspace--outcome">
      <a className="skip-link" href="#react-content">Skip to content</a>
      <OperatorTopBar
        bootstrap={bootstrap}
        query={commandQuery}
        onQueryChange={setCommandQuery}
        onSubmit={submitCommandSearch}
      />
      <OutcomeModeRail modes={operatorModes} activeMode={activeMode} />
      <div className="workspace__main">
        <main id="react-content" className="workspace__content" tabIndex={-1}>{children}</main>
      </div>
      <SystemTray bootstrap={bootstrap} socketState={socketState} />
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
