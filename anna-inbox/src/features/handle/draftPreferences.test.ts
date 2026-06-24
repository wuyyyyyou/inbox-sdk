import { describe, expect, it } from "vitest";
import { DEFAULT_DRAFT_PREFERENCES } from "../../types/mail";
import { buildDraftPreferencesInstruction, resolveDraftPreferences } from "./draftPreferences";

describe("draft preferences helpers", () => {
  it("fills in default values for unset controls", () => {
    expect(resolveDraftPreferences({ tone: "Direct" })).toEqual({
      ...DEFAULT_DRAFT_PREFERENCES,
      tone: "Direct",
    });
  });

  it("combines freeform instructions with structured draft controls", () => {
    expect(buildDraftPreferencesInstruction({ length: "Brief", mood: "Urgent" }, "Ask for a time next week")).toBe(
      [
        "Ask for a time next week",
        "",
        "Use these draft preferences:",
        "- Length: Brief",
        "- Writing style: Natural",
        "- Tone: Warm",
        "- Mood: Urgent",
        "",
        "Keep the reply natural, specific, and ready to send. Avoid generic AI-sounding phrasing.",
      ].join("\n"),
    );
  });
});
