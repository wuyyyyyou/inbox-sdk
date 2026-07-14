import { describe, expect, it } from "vitest";
import { customExecutionSteps, customStageCopy, customStageKey, makeCustomRunProgress, scanProgressLabel, scanStageLabel, stageToStep } from "./runHelpers";

describe("run helpers", () => {
  it("maps backend stages to scan steps", () => {
    expect(stageToStep("scan")).toBe(0);
    expect(stageToStep("phase1")).toBe(1);
    expect(stageToStep("check_replied")).toBe(2);
    expect(stageToStep("read_context")).toBe(2);
    expect(stageToStep("evaluate")).toBe(3);
    expect(stageToStep("done")).toBe(4);
  });

  it("formats scan labels", () => {
    expect(scanStageLabel("evaluate", { current: 1, total: 3 })).toBe("Evaluating cards. (1/3)");
    expect(scanProgressLabel("phase1_done", { candidates: 2, low_value: 5 })).toBe("2 candidates, checking open threads next");
    expect(scanProgressLabel("check_replied", { current: 1, total: 2 })).toBe("Checking candidate threads 1/2");
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

  it("renders only safe scan execution steps and counts", () => {
    const steps = customExecutionSteps("search_done", {
      scan_window_days: 7,
      scanned: 12,
      threads: 8,
      query: "from:private@example.com confidential body",
      subject: "Confidential roadmap",
    });
    const text = steps.map((step) => step.label).join(" ");
    expect(text).toContain("Understanding request");
    expect(text).toContain("Searching selected time range (7 days)");
    expect(text).toContain("Found 12 emails in 8 threads");
    expect(text).not.toContain("private@example.com");
    expect(text).not.toContain("Confidential roadmap");
  });
});
