import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const styles = readFileSync(new URL("./global.css", import.meta.url), "utf8");

describe("mail tabs manage tooltip", () => {
  it("resets the tab underline positioning before applying tooltip positioning", () => {
    const tooltipRule = styles.match(
      /\.mail-tabs \.mail-tabs-manage::after,[\s\S]*?\r?\n}\r?\n/,
    )?.[0];

    expect(tooltipRule).toContain("right: auto;");
    expect(tooltipRule).toContain("bottom: auto;");
    expect(tooltipRule).toContain("height: auto;");
  });
});

describe("mail feed overflow", () => {
  it("prevents row action overlays from creating a horizontal scrollbar", () => {
    const feedRule = styles.match(/\.mail-feed \{[\s\S]*?\r?\n}\r?\n/)?.[0];

    expect(feedRule).toContain("overflow-x: clip;");
    expect(feedRule).toContain("overflow-y: auto;");
  });
});

describe("inbox selection toolbar", () => {
  it("uses a consistent icon-button size and exposes its tooltips", () => {
    const clearRule = styles.match(/\.inbox-selection-clear \{[\s\S]*?\r?\n}\r?\n/)?.[0];
    const tooltipRule = styles.match(
      /\.inbox-selection-clear::after,[\s\S]*?\r?\n}\r?\n/,
    )?.[0];

    expect(clearRule).toContain("width: 32px;");
    expect(clearRule).toContain("height: 32px;");
    expect(tooltipRule).toContain("content: attr(data-tooltip);");
    expect(tooltipRule).toContain("top: calc(100% + 8px);");
  });
});
