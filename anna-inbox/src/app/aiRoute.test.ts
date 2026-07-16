import { describe, expect, it } from "vitest";
import { buildRevisionPrompt } from "./aiRoute";

describe("buildRevisionPrompt", () => {
  it("returns the visible prompt when there is no draft", () => {
    expect(buildRevisionPrompt("Make it shorter")).toBe("Make it shorter");
  });

  it("wraps an existing draft as quoted content", () => {
    const prompt = buildRevisionPrompt("Make it shorter", "Original draft");
    expect(prompt).toContain("Make it shorter");
    expect(prompt).toContain("Original draft");
    expect(prompt).toContain("quoted content");
  });
});
