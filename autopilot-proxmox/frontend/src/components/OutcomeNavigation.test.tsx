import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import type { AppBootstrap, OperatorMode, OperatorOutcome, OperatorQuickRoute } from "../contracts";
import { OperatorTopBar, OutcomeCardGrid, OutcomeModeRail, QuickRouteLane, SystemTray } from "./OutcomeNavigation";

const modes: readonly OperatorMode[] = [
  { id: "home", label: "Home", longLabel: "Home", href: "/react-shell" },
  { id: "deploy", label: "Deploy", longLabel: "Deploy", href: "/react/cloudosd" },
  { id: "build", label: "Build", longLabel: "Build", href: "/react/task-engine" }
];

const outcomes: readonly OperatorOutcome[] = [
  {
    id: "deploy-desktop",
    mode: "deploy",
    eyebrow: "Recommended",
    title: "Deploy a Windows desktop",
    summary: "Open OSDCloud Desktop.",
    primaryHref: "/react/cloudosd",
    actionLabel: "Start desktop run",
    tone: "good",
    relatedRoutes: [{ label: "Jobs", href: "/react/jobs", purpose: "Live output" }]
  }
];

const quickRoutes: readonly OperatorQuickRoute[] = [
  { label: "Jobs", href: "/react/jobs", summary: "Live output and pause gates", mode: "home" }
];

const bootstrap: AppBootstrap = {
  buildSha: "abc1234",
  buildTime: "2026-06-19T19:00:00Z",
  userName: "Adam"
};

describe("OutcomeNavigation components", () => {
  test("renders mode rail with active mode", () => {
    render(<OutcomeModeRail modes={modes} activeMode="deploy" />);

    expect(screen.getByRole("navigation", { name: "Outcome modes" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Deploy" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Home" })).not.toHaveAttribute("aria-current");
  });

  test("renders outcome cards with primary actions and related routes", () => {
    render(<OutcomeCardGrid outcomes={outcomes} />);

    expect(screen.getByRole("heading", { name: "Deploy a Windows desktop" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start desktop run" })).toHaveAttribute("href", "/react/cloudosd");
    expect(screen.getByRole("link", { name: "Jobs" })).toHaveAttribute("href", "/react/jobs");
  });

  test("renders quick routes", () => {
    render(<QuickRouteLane quickRoutes={quickRoutes} />);

    expect(screen.getByRole("navigation", { name: "Quick routes" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Jobs Live output and pause gates" })).toHaveAttribute("href", "/react/jobs");
  });

  test("renders top bar command and operator identity", () => {
    render(<OperatorTopBar bootstrap={bootstrap} query="" onQueryChange={() => {}} onSubmit={() => {}} />);

    expect(screen.getByRole("link", { name: "Proxmox VE Autopilot home" })).toHaveAttribute("href", "/react-shell");
    expect(screen.getByRole("searchbox", { name: "Search console" })).toBeInTheDocument();
    expect(screen.getByText("Adam")).toBeInTheDocument();
  });

  test("renders system tray build and socket state", () => {
    render(<SystemTray bootstrap={bootstrap} socketState="open" />);

    expect(screen.getByText("Live open")).toBeInTheDocument();
    expect(screen.getByText("Build abc1234")).toBeInTheDocument();
  });
});
