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
