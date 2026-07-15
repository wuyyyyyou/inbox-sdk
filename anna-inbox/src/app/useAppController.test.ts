import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const controllerSource = readFileSync(
  new URL("./useAppController.ts", import.meta.url),
  "utf8",
);
const homeViewSource = readFileSync(
  new URL("../features/home/HomeView.tsx", import.meta.url),
  "utf8",
);

describe("inbox startup settings", () => {
  it("loads inbox settings for the initial mailbox", () => {
    expect(controllerSource).toMatch(
      /await Promise\.all\(\[[\s\S]*?loadInboxSettings\(currentMailbox\),/,
    );
  });
});

describe("Manage Splits tooltip", () => {
  it("uses only the custom tooltip instead of a browser title tooltip", () => {
    const manageSplitsButton = homeViewSource.match(
      /<button[\s\S]*?aria-label="Manage Splits"[\s\S]*?<\/button>/,
    )?.[0];

    expect(manageSplitsButton).toContain('data-tooltip="Manage Splits"');
    expect(manageSplitsButton).not.toContain('title="Manage Splits"');
  });
});

describe("AI scan invocation", () => {
  it("uses the current Scan Plan and a 60-second initial wait in every entry point", () => {
    expect(controllerSource.match(/wait_timeout_seconds: 60/g)?.length || 0).toBeGreaterThanOrEqual(3);
    expect(controllerSource.match(/scan_window_days: scanScope\.scan_window_days/g)?.length || 0).toBeGreaterThanOrEqual(3);
    expect(controllerSource.match(/max_messages: scanScope\.max_messages/g)?.length || 0).toBeGreaterThanOrEqual(3);
  });

  it("uses the Inbox display range as the AI scan time range", () => {
    expect(controllerSource).toMatch(/scan_window_days: clampInt\(settings\.display_range_days, plan\.scan_window_days, 1, 90\)/);
  });

  it("wires the unified startAiTurn path for the sidebar", () => {
    expect(controllerSource).toMatch(/client\.startAiTurn\(/);
    expect(controllerSource).toMatch(/isAiTurnEnabled\(/);
    expect(controllerSource).toMatch(/buildAiTurnUiContext\(/);
  });

  it("resumes a timed-out sidebar turn by polling its existing run", () => {
    expect(controllerSource).toMatch(/resumeAiConversation\(index: number\)/);
    expect(controllerSource).toMatch(/resumeRunId: pendingRun\.runId/);
    expect(controllerSource).toMatch(/options\.resumeRunId\s*\? await client\.getRun\(runId\)/);
    expect(homeViewSource).toContain('aria-label="Refresh timed out request"');
  });
});
