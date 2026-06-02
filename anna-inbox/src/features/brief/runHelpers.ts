import type { CustomRunProgress, RunStatus } from "../../types/mail";

export const SCAN_STEPS = [
  { title: "Connecting mailbox", microcopy: "Connecting to your Gmail source and reading recent activity.", stagePrefix: "scan" },
  { title: "Processing messages", microcopy: "Filtering duplicates, deduplicating threads, and running first-pass classification.", stagePrefix: "storage_filter" },
  { title: "Reading context", microcopy: "Fetching thread context and message details for candidates.", stagePrefix: "read_context" },
  { title: "Evaluating items", microcopy: "LLM is reviewing each candidate and preparing judgments.", stagePrefix: "evaluate" },
  { title: "Preparing brief", microcopy: "Building action plan and saving cards to local storage.", stagePrefix: "plan" },
];

const STAGE_STEP_MAP: Record<string, number> = {
  queued: 0, parse_intent: 0, scan: 0, scan_done: 0,
  storage_filter: 1, thread_dedup: 1, phase1: 1, phase1_done: 1,
  read_context: 2, read_context_done: 2,
  evaluate: 3, evaluate_done: 3,
  plan: 4, storage_saved: 4, done: 4,
  planning: 0, planning_done: 0,
};

export function stageToStep(stage: string): number {
  return STAGE_STEP_MAP[stage] ?? 0;
}

export function scanStageLabel(stage: string | undefined, progress: Record<string, unknown> = {}): string {
  const labels: Record<string, string> = {
    queued: "Scan queued.",
    planning: "Generating scan plan with Anna LLM.",
    planning_done: "Plan ready. Starting scan.",
    parse_intent: "Choosing scan strategy.",
    scan: "Reading Gmail source.",
    scan_done: "New messages loaded.",
    storage_filter: "Skipping messages already processed.",
    phase1: "Finding candidate attention items.",
    phase1_done: "Candidate scan complete.",
    read_context: "Reading context for candidates.",
    read_context_done: "Context loaded.",
    evaluate: "Evaluating cards.",
    evaluate_done: "Evaluation complete.",
    plan: "Building action plan.",
    storage_saved: "Persisting cards locally.",
    done: "Scan complete.",
  };
  const count = progress.current && progress.total ? ` (${progress.current}/${progress.total})` : "";
  return `${labels[stage || ""] || stage || "Scanning."}${count}`;
}

export function scanProgressLabel(stage: string | undefined, progress: Record<string, unknown> = {}): string {
  const p = progress;
  if (!stage || stage === "queued") return "";
  if (stage === "scan" || stage === "parse_intent") return "Connecting to Gmail...";
  if (stage === "scan_done") return `${p.scanned || 0} emails loaded`;
  if (stage === "storage_filter") return `${p.skipped || 0} skipped, ${p.new || 0} new`;
  if (stage === "thread_dedup") return `${p.after || 0} after dedup`;
  if (stage === "phase1") return `Classifying ${p.scanned || 0} emails...`;
  if (stage === "phase1_done") return `${p.candidates || 0} candidates, ${p.low_value || 0} low-priority`;
  if (stage === "read_context") return `Reading context ${p.current || 0}/${p.total || 0}`;
  if (stage === "read_context_done") return `${p.total || 0} contexts loaded`;
  if (stage === "evaluate") return `Evaluating ${p.evaluated || 0}/${p.total || 0}`;
  if (stage === "evaluate_done") return `${p.evaluated || p.total || 0} evaluated`;
  if (stage === "plan") return "Building action plan...";
  if (stage === "storage_saved") return "Cards saved.";
  if (stage === "done") return "Scan complete.";
  return "";
}

export const CUSTOM_PROGRESS_STEPS = [
  { key: "planning", label: "Plan" },
  { key: "searching", label: "Search" },
  { key: "reading", label: "Read" },
  { key: "answering", label: "Answer" },
  { key: "done", label: "Done" },
];

export function customStageKey(stage: string | undefined, status: string | undefined): string {
  if (status === "done") return "done";
  if (status === "failed") return "failed";
  if (stage === "scan" || stage === "scan_done") return "searching";
  if (stage === "read_context" || stage === "read_context_done") return "reading";
  if (stage === "evaluate" || stage === "evaluate_done") return "answering";
  if (stage === "done") return "done";
  return "planning";
}

export function customStageCopy(stageKey: string, progress: Record<string, unknown> = {}): string {
  if (stageKey === "planning") return "Shaping the mailbox scan into a focused plan.";
  if (stageKey === "searching") {
    const found = Number(progress.scanned || 0);
    return found ? `Found ${found} relevant message${found === 1 ? "" : "s"}.` : "Running the planned Gmail searches.";
  }
  if (stageKey === "reading") {
    const current = Number(progress.current || 0);
    const total = Number(progress.total || 0);
    return total ? `Reading context ${current}/${total}.` : "Opening the relevant email context.";
  }
  if (stageKey === "answering") return "The mail evidence is ready. Anna is composing the answer.";
  if (stageKey === "done") return "Answer ready.";
  return "The scan stopped before an answer was produced.";
}

export function makeCustomRunProgress(
  previous: CustomRunProgress | null,
  status: RunStatus,
  fallback: { runId?: string; question?: string; startedAt?: string } = {},
): CustomRunProgress {
  const progress = status.progress || {};
  const partial = {
    ...(previous?.partial || {}),
    ...(status.partial || {}),
  };
  const stageKey = customStageKey(status.stage, status.status);
  return {
    runId: status.run_id || fallback.runId || "",
    question: fallback.question || previous?.question || "",
    status: status.status || "running",
    stage: status.stage || "planning",
    stageKey,
    progress,
    partial,
    startedAt: status.started_at || fallback.startedAt || previous?.startedAt || "",
    updatedAt: status.updated_at || "",
  };
}
