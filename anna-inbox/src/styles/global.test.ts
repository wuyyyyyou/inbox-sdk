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
