import { createRoot } from "react-dom/client";

import { App } from "./App";
import type { AppBootstrap } from "./contracts";
import "./styles.css";

function bootstrapFromRoot(root: HTMLElement): AppBootstrap {
  return {
    ...(root.dataset.buildSha ? { buildSha: root.dataset.buildSha } : {}),
    ...(root.dataset.buildTime ? { buildTime: root.dataset.buildTime } : {})
  };
}

const rootElement = document.getElementById("react-root");

if (!rootElement) {
  throw new Error("React root element #react-root was not found.");
}

createRoot(rootElement).render(<App bootstrap={bootstrapFromRoot(rootElement)} />);
