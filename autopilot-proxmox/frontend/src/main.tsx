import { createRoot } from "react-dom/client";

import { App } from "./App";
import type { AppBootstrap } from "./contracts";
import "./styles.css";

function bootstrapFromRoot(root: HTMLElement): AppBootstrap {
  let onboarding: AppBootstrap["onboarding"] | undefined;
  if (root.dataset.onboarding) {
    try {
      onboarding = JSON.parse(root.dataset.onboarding) as AppBootstrap["onboarding"];
    } catch {
      onboarding = { status: "absent" };
    }
  }
  return {
    ...(root.dataset.buildVersion ? { buildVersion: root.dataset.buildVersion } : {}),
    ...(root.dataset.buildSha ? { buildSha: root.dataset.buildSha } : {}),
    ...(root.dataset.buildTime ? { buildTime: root.dataset.buildTime } : {}),
    ...(root.dataset.userName ? { userName: root.dataset.userName } : {}),
    ...(root.dataset.userEmail ? { userEmail: root.dataset.userEmail } : {}),
    ...(onboarding ? { onboarding } : {})
  };
}

const rootElement = document.getElementById("react-root");

if (!rootElement) {
  throw new Error("React root element #react-root was not found.");
}

createRoot(rootElement).render(<App bootstrap={bootstrapFromRoot(rootElement)} />);
