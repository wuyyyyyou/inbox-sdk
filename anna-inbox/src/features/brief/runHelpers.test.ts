import { describe, expect, it } from "vitest";
import { customStageCopy, customStageKey, makeCustomRunProgress, scanProgressLabel, scanStageLabel, stageToStep } from "./runHelpers";

describe("run helpers", () => {
  it("maps backend stages to scan steps", () => {
    expect(stageToStep("scan")).toBe(0);
    expect(stageToStep("phase1")).toBe(1);
    expect(stageToStep("read_context")).toBe(2);
    expect(stageToStep("evaluate")).toBe(3);
    expect(stageToStep("done")).toBe(4);
  });

  it("formats scan labels", () => {
    expect(scanStageLabel("evaluate", { current: 1, total: 3 })).toBe("Evaluating cards. (1/3)");
    expect(scanProgressLabel("phase1_done", { candidates: 2, low_value: 5 })).toBe("2 candidates, 5 low-priority");
  });

  it("normalizes custom scan stages", () => {
    expect(customStageKey("scan", "running")).toBe("searching");
    expect(customStageKey("read_context", "running")).toBe("reading");
    expect(customStageKey("evaluate", "running")).toBe("answering");
    expect(customStageKey("anything", "done")).toBe("done");
    expect(customStageCopy("searching", { scanned: 2 })).toContain("2 relevant");
  });

  it("merges partial custom run progress", () => {
    const progress = makeCustomRunProgress(
      { runId: "r1", question: "q", status: "running", stage: "planning", stageKey: "planning", progress: {}, partial: { plan: { title: "Old" } } },
      { run_id: "r1", status: "running", stage: "scan", partial: { sources: [{ subject: "S" }] } },
    );
    expect(progress.stageKey).toBe("searching");
    expect(progress.partial).toHaveProperty("plan");
    expect(progress.partial).toHaveProperty("sources");
  });
});
