import type { CustomRunProgress, RunStatus } from "../../types/mail";

export const SCAN_STEPS = [
  { title: "Connecting mailbox", microcopy: "Connecting to your Gmail source and reading recent activity.", stagePrefix: "scan" },
  { title: "Classifying headers", microcopy: "Reading headers and snippets to find candidate attention items.", stagePrefix: "phase1" },
  { title: "Checking open threads", microcopy: "Checking candidate threads to skip conversations you already answered.", stagePrefix: "check_replied" },
  { title: "Evaluating items", microcopy: "LLM is reviewing each candidate and preparing judgments.", stagePrefix: "evaluate" },
  { title: "Preparing brief", microcopy: "Saving cards, cleanup bundles, and scan history.", stagePrefix: "finalizing" },
];

const STAGE_STEP_MAP: Record<string, number> = {
  queued: 0, parse_intent: 0, sync_gmail_state: 0, scan: 0, scanning: 0, scan_cache: 0, scan_done: 0, scan_fallback: 0, scan_fallback_empty: 0,
  storage_filter: 1, thread_dedup: 1, phase1: 1, phase1_done: 1,
  filtering: 1,
  check_replied: 2, already_replied_filter: 2,
  read_context: 2, read_context_done: 2,
  evaluate: 3, evaluate_done: 3, phase2: 3,
  plan: 4, finalizing: 4, storage_saved: 4, reading_cards: 4, read_cards_error: 4, storage_error: 4, done: 4,
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
    sync_gmail_state: "Syncing Gmail state.",
    scan: "Reading Gmail source.",
    scanning: "Reading Gmail source.",
    scan_cache: "Loading Gmail cache.",
    scan_done: "New messages loaded.",
    scan_fallback: "Gmail API unreachable — using cached emails.",
    scan_fallback_empty: "Gmail API unreachable and cache empty — no emails available.",
    storage_filter: "Skipping messages already processed.",
    filtering: "Filtering messages.",
    thread_dedup: "Deduplicating threads.",
    check_replied: "Checking candidate thread status.",
    already_replied_filter: "Filtering already-replied threads.",
    phase1: "Finding candidate attention items.",
    phase1_done: "Candidate scan complete.",
    read_context: "Reading context for candidates.",
    read_context_done: "Context loaded.",
    evaluate: "Evaluating cards.",
    evaluate_done: "Evaluation complete.",
    phase2: "Evaluating cards.",
    plan: "Building action plan.",
    finalizing: "Saving brief.",
    storage_saved: "Persisting cards locally.",
    storage_error: "Failed to persist cards.",
    reading_cards: "Reading cards from storage.",
    read_cards_error: "Failed to read cards.",
    done: "Scan complete.",
  };
  const count = progress.current && progress.total ? ` (${progress.current}/${progress.total})` : "";
  return `${labels[stage || ""] || stage || "Scanning."}${count}`;
}

export function scanProgressLabel(stage: string | undefined, progress: Record<string, unknown> = {}): string {
  const p = progress;
  if (!stage || stage === "queued") return "";
  if (stage === "sync_gmail_state") {
    const resolved = Number(p.resolved_replied || 0);
    const checked = Number(p.checked_threads || 0);
    return resolved > 0 ? `${resolved} replied threads synced` : `Checking Gmail state${checked ? ` · ${checked} threads` : ""}`;
  }
  if (stage === "scan" || stage === "scanning" || stage === "parse_intent") {
    const fetched = Number(p.messages_fetched || p.current || 0);
    const max = Number(p.max_messages || p.total || 0);
    if (max && fetched > 0) return `Fetching emails ${fetched}/${max}`;
    return "Connecting to Gmail...";
  }
  if (stage === "scan_cache") return `${p.lite_count || 0}/${p.matched_ids || 0} cached emails ready`;
  if (stage === "scan_done") return `${p.scanned || 0} emails loaded`;
  if (stage === "scan_fallback") return `Gmail unreachable, using ${p.cached_count || 0} cached`;
  if (stage === "scan_fallback_empty") return "No cache available — check network & token";
  if (stage === "storage_filter") return `${p.skipped || 0} skipped, ${p.new || 0} new`;
  if (stage === "thread_dedup") return `${p.after || 0} after dedup`;
  if (stage === "check_replied") return `Checking candidate threads ${p.current || 0}/${p.total || 0}`;
  if (stage === "already_replied_filter") return `${p.filtered || 0} already replied`;
  if (stage === "filtering") return `${p.new || 0} new after filtering`;
  if (stage === "phase1") {
    const current = Number(p.current || 0);
    const total = Number(p.total || p.scanned || 0);
    if (total) return `Classifying headers ${current}/${total}`;
    return `Classifying ${p.scanned || 0} emails...`;
  }
  if (stage === "phase1_done") return `${p.candidates || 0} candidates, checking open threads next`;
  if (stage === "read_context") return `Reading context ${p.current || 0}/${p.total || 0}`;
  if (stage === "read_context_done") return `${p.total || 0} contexts loaded`;
  if (stage === "phase2" || stage === "evaluate") {
    const detail = `Evaluating ${p.evaluated || 0}/${p.total || 0}`;
    const fb = Number(p.fallback || 0);
    return fb > 0 ? `${detail} · ${fb} fallback` : detail;
  }
  if (stage === "evaluate_done") {
    const fb = Number(p.fallback || 0);
    const total = Number(p.evaluated || p.total || 0);
    if (fb > 0) return `${total} evaluated · ${fb} fallback`;
    return `${total} evaluated`;
  }
  if (stage === "plan") {
    const n = Number(p.judgments || 0);
    return n ? `Building action plan from ${n} evaluations...` : "Building action plan...";
  }
  if (stage === "finalizing") return "Saving brief and scan state...";
  if (stage === "storage_saved") return "Cards saved.";
  if (stage === "storage_error") {
    const reason = p.reason ? `: ${p.reason}` : "";
    return `Storage write failed${reason}`;
  }
  if (stage === "reading_cards") {
    const n = Number(p.main_items || 0);
    return n ? `Reading ${n} cards from storage...` : "Reading cards from storage...";
  }
  if (stage === "read_cards_error") {
    const reason = p.reason ? `: ${p.reason}` : "";
    return `Failed to read cards${reason}`;
  }
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
  // Legacy custom scan stages
  if (stage === "scan" || stage === "scan_done") return "searching";
  if (stage === "read_context" || stage === "read_context_done") return "reading";
  if (stage === "evaluate" || stage === "evaluate_done") return "answering";
  // New ask pipeline stages (plan → search → filter → context → answer → guard)
  if (stage === "plan" || stage === "plan_done") return "planning";
  if (stage === "search" || stage === "search_done" || stage === "filter_done") return "searching";
  if (stage === "answer") return "answering";
  // AI turn 统一入口：routing 映射到 planning 文案
  if (stage === "routing" || stage === "routing_done") return "planning";
  if (stage === "read") return "reading";
  if (stage === "done") return "done";
  return "planning";
}

export function customStageCopy(stageKey: string, progress: Record<string, unknown> = {}): string {
  if (stageKey === "planning") {
    const title = String(progress.title || "");
    return title ? `Plan ready: ${title}` : "Shaping the mailbox scan into a focused plan.";
  }
  if (stageKey === "searching") {
    const found = Number(progress.scanned || progress.candidates || 0);
    return found ? `Found ${found} relevant message${found === 1 ? "" : "s"}.` : "Running the planned Gmail searches.";
  }
  if (stageKey === "reading") {
    const current = Number(progress.current || 0);
    const total = Number(progress.total || 0);
    return total ? `Reading context ${current}/${total}.` : "Opening the relevant email context.";
  }
  if (stageKey === "answering") {
    const candidates = Number(progress.candidates || 0);
    return candidates ? `Analyzing ${candidates} candidate${candidates === 1 ? "" : "s"} with Anna LLM.` : "The mail evidence is ready. Anna is composing the answer.";
  }
  if (stageKey === "done") return "Answer ready.";
  return "The scan stopped before an answer was produced.";
}

export type CustomExecutionStep = {
  label: string;
  status: "complete" | "active" | "pending";
};

export function customExecutionSteps(stage: string | undefined, progress: Record<string, unknown> = {}): CustomExecutionStep[] {
  const days = Number(progress.scan_window_days || 0);
  const scanned = Number(progress.scanned || 0);
  const threads = Number(progress.threads || 0);
  const labels = [
    "Understanding request",
    days > 0 ? `Searching selected time range (${days} days)` : "Searching selected time range",
    scanned > 0 ? `Found ${scanned} emails${threads > 0 ? ` in ${threads} threads` : ""}` : "Finding matching emails and threads",
    "Filtering relevant mail",
    "Reading necessary context",
    "Preparing result",
  ];
  const activeIndex = stage === "search" ? 1
    : stage === "search_done" ? 2
      : stage === "filter_done" ? 3
        : stage === "read_context" || stage === "read_context_done" ? 4
          : stage === "answer" || stage === "done" ? 5
            : 0;
  return labels.map((label, index) => ({
    label,
    status: index < activeIndex ? "complete" : index === activeIndex ? "active" : "pending",
  }));
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
