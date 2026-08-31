"use client";

import { useChat } from "@ai-sdk/react";
import {
  DefaultChatTransport,
  getToolName,
  isToolUIPart,
  lastAssistantMessageIsCompleteWithApprovalResponses,
  type ToolUIPart,
  type UIMessage,
} from "ai";
import {
  AlertCircle, ArrowDown, Bot, Check, CheckCircle2, ChevronDown, Circle,
  Clock3, Download, FileJson, FilePlus2, FileSpreadsheet, Files, Loader2,
  Layers3, Menu, MessageSquarePlus, Paperclip, PanelRight, Play, Send, ShieldCheck,
  Sparkles, Square, Wrench, X, Settings, KeyRound,
} from "lucide-react";
import { FormEvent, Fragment, useEffect, useMemo, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type RowFilter = { exclude_prefixes: string[]; exclude_exact_values: string[]; exclude_contains: string[]; exclude_regexes: string[] };
type Operation = { id: string; sheet: string; source_sheet?: string; mode: "add" | "concatenate"; description: string; alignment?: string; placement?: string; row_keys: string[]; value_columns: number[]; row_filter?: RowFilter };
type Conflict = { id: string; type?: string; actual?: unknown; source_file: string; sheet: string; cell?: string; message: string; allowed_actions: string[]; resolution?: string };
type DecisionOption = { action: string; label: string; description: string };
type RuntimeDecision = { id: string; phase: string; code: string; question: string; message: string; context: Record<string, unknown>; options: DecisionOption[]; created_at: string; selected_action?: string; user_note?: string };
type RunEvent = { at: string; kind: string; message: string; data?: Record<string, unknown> };
type UploadedWorkbook = { id: string; role: string; filename: string; sha256: string };
type CompiledSource = { source_id: string; source_file: string; source_sheet: string; target_sheet: string; columns: { target_column: number; source_column: number; header_path: string[]; matched_by: string }[]; rows: { source_row: number; target_row?: number; row_key: string }[] };
type CompiledOperation = { operation_id: string; mode: string; sources: CompiledSource[] };
type VerificationCheck = { name: string; passed: boolean; expected_cells?: number; expected_rows?: number; mismatches?: unknown[] };
type BatchProgress = { batch_size: number; total_sources: number; total_work_units: number; completed_work_units: number; current_operation?: string; current_batch: number; batches_in_operation: number; processed_sources: number; status: "idle" | "running" | "completed" | "failed"; started_at?: string; completed_at?: string };
type Run = {
  id: string; state: string; created_at: string; updated_at: string;
  template?: UploadedWorkbook; sources: UploadedWorkbook[];
  spec?: { operations: Operation[]; rationale: string; guideline_citations: string[] };
  compiled_plan?: { spec_hash: string; operations: CompiledOperation[] };
  planner?: { kind: string; model: string; evidence_sha256: string; attempts?: number };
  spec_hash?: string; approved_spec_hash?: string; conflicts: Conflict[];
  decisions: RuntimeDecision[]; excluded_sources: string[]; execution_attempts: number;
  batch_size: number; batch_progress?: BatchProgress;
  recovery_attempts?: { id: string; code: string; action: string; attempt: number; max_attempts: number; outcome: string; message: string }[];
  events: RunEvent[]; conversation: UIMessage[];
  verification?: { passed: boolean; checks: VerificationCheck[] }; error?: string;
  model_profile_id?: string;
};
type ModelProfileSummary = { id: string; provider: "openai" | "minimax" | "deepseek" | "custom"; base_url: string; model: string; api_mode: string; timeout: number; is_default: boolean; has_api_key: boolean };
type ModelConnections = { version: number; default: string; profiles: ModelProfileSummary[] };

const conflictLabels: Record<string, string> = { treat_as_zero: "Treat as zero", keep_marker: "Keep marker", skip_cell: "Skip cell", exclude_source: "Exclude source", abort: "Abort" };
const eventLabels: Record<string, { title: string; tool?: string }> = {
  run_created: { title: "Merge task created" }, files_uploaded: { title: "Workbook files attached", tool: "store_workbooks" },
  inspection_started: { title: "Inspecting workbook structures", tool: "inspect_workbooks" }, inspection_completed: { title: "Workbook evidence extracted", tool: "inspect_workbooks" },
  model_plan_ready: { title: "Merge plan generated", tool: "submit_merge_plan" }, planning_failed: { title: "Planning failed", tool: "submit_merge_plan" },
  plan_approved: { title: "Legacy plan approval recorded" }, write_approved: { title: "Local file write approved" }, conflict_resolved: { title: "User-provided rule recorded" },
  executor_tool_started: { title: "Python executor started", tool: "execute_approved_merge" }, execution_completed: { title: "Workbook merged and verified", tool: "reconcile_workbook" },
  execution_failed: { title: "Execution failed", tool: "execute_approved_merge" }, runtime_decision_requested: { title: "Business input required" },
  runtime_decision_resolved: { title: "Runtime instruction recorded" }, run_cancelled: { title: "Merge task cancelled" },
  recovery_started: { title: "Automatic recovery started", tool: "recover_merge" }, recovery_succeeded: { title: "Automatic recovery succeeded", tool: "recover_merge" },
  recovery_failed: { title: "Automatic recovery attempt failed", tool: "recover_merge" }, recovery_exhausted: { title: "Automatic recovery exhausted", tool: "recover_merge" },
  input_change_detected: { title: "Input change detected; replanning required", tool: "validate_input_hashes" },
  batch_configuration_updated: { title: "Batch processing configured", tool: "configure_source_batches" },
  batch_execution_started: { title: "Bounded source batches started", tool: "process_source_batches" },
  batch_execution_completed: { title: "All source batches completed", tool: "process_source_batches" },
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.detail;
    throw new Error(typeof detail === "string" ? detail : detail?.message ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function metadataTime(message: UIMessage): string | undefined {
  const metadata = message.metadata;
  if (!metadata || typeof metadata !== "object") return undefined;
  const value = (metadata as Record<string, unknown>).createdAt;
  return typeof value === "string" ? value : undefined;
}
function withMessageTimes(messages: UIMessage[]): UIMessage[] {
  let cursor = Date.now();
  return messages.map((message) => {
    const explicit = metadataTime(message);
    if (message.role === "user" && explicit) cursor = new Date(explicit).getTime();
    else cursor += 1;
    return { ...message, metadata: { ...(message.metadata && typeof message.metadata === "object" ? message.metadata : {}), createdAt: new Date(cursor).toISOString() } };
  });
}
function stateLabel(state: string): string { return state.replaceAll("_", " "); }
function stateTone(state: string): string {
  if (state === "completed") return "bg-emerald-100 text-emerald-800";
  if (state === "failed" || state === "cancelled") return "bg-rose-100 text-rose-800";
  if (["awaiting_human", "awaiting_approval", "awaiting_user_input", "awaiting_write_approval"].includes(state)) return "bg-amber-100 text-amber-800";
  if (["executing", "inspecting", "recovering"].includes(state)) return "bg-blue-100 text-blue-800";
  return "bg-[#e8ece8] text-[#526058]";
}
function operationFilters(operation: Operation): string[] {
  const filter = operation.row_filter;
  if (!filter) return [];
  return [...filter.exclude_prefixes.map((v) => `prefix “${v}”`), ...filter.exclude_exact_values.map((v) => `exact “${v}”`), ...filter.exclude_contains.map((v) => `contains “${v}”`), ...filter.exclude_regexes.map((v) => `regex ${v}`)];
}

function FileChip({ file, role, onRemove }: { file: File; role: string; onRemove: () => void }) {
  return <div className="flex min-w-0 items-center gap-2 rounded-lg border border-[#daddd6] bg-[#f7f8f5] px-2.5 py-2"><FileSpreadsheet size={16} className="shrink-0 text-[#347257]" /><div className="min-w-0"><p className="truncate text-xs font-medium">{file.name}</p><p className="text-[10px] uppercase tracking-[.08em] text-[#848780]">{role}</p></div><button onClick={onRemove} className="ml-1 rounded p-0.5 text-[#90928c] hover:bg-[#e6e8e2]" aria-label={`Remove ${file.name}`}><X size={13} /></button></div>;
}

function EmptyConversation(props: { template: File | null; sources: File[]; instruction: string; batchSize: number; busy: boolean; error: string | null; onTemplate: (file: File | null) => void; onSources: (files: File[]) => void; onInstruction: (value: string) => void; onBatchSize: (value: number) => void; onSubmit: () => void }) {
  const templateInput = useRef<HTMLInputElement>(null), sourceInput = useRef<HTMLInputElement>(null);
  return <div className="flex min-h-0 flex-1 flex-col">
    <div className="flex flex-1 items-center justify-center px-5 py-10"><div className="w-full max-w-2xl text-center"><span className="mx-auto grid size-11 place-items-center rounded-xl border border-[#dfe5df] bg-white text-[#275d46] shadow-sm"><Bot size={21} /></span><h1 className="mt-5 text-2xl font-semibold tracking-[-.035em]">What should I merge?</h1><p className="mx-auto mt-2 max-w-md text-sm leading-6 text-[#747670]">Attach one template and the source workbooks. I’ll resolve structural differences automatically and ask only when business meaning is unavailable.</p></div></div>
    <div className="px-4 pb-5 md:px-8 md:pb-7"><div className="mx-auto max-w-3xl rounded-2xl border border-[#d8d9d3] bg-white p-3 shadow-[0_8px_30px_rgba(38,42,36,.08)]">
      {(props.template || props.sources.length > 0) && <div className="mb-2 grid gap-2 border-b border-[#ecece8] pb-3 sm:grid-cols-2">{props.template && <FileChip file={props.template} role="Template" onRemove={() => props.onTemplate(null)} />}{props.sources.map((file, i) => <FileChip key={`${file.name}-${i}`} file={file} role="Source" onRemove={() => props.onSources(props.sources.filter((_, index) => index !== i))} />)}</div>}
      <textarea value={props.instruction} onChange={(e) => props.onInstruction(e.target.value)} className="min-h-20 w-full resize-none bg-transparent px-2 py-1 text-sm leading-6 outline-none placeholder:text-[#999b95]" placeholder="Describe the merge task, or simply attach your workbooks…" />
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2"><div className="flex flex-wrap items-center gap-1"><button onClick={() => templateInput.current?.click()} className="flex items-center gap-2 rounded-lg px-2.5 py-2 text-xs font-medium text-[#61645e] hover:bg-[#f1f1ed]"><FilePlus2 size={15} />Template</button><button onClick={() => sourceInput.current?.click()} className="flex items-center gap-2 rounded-lg px-2.5 py-2 text-xs font-medium text-[#61645e] hover:bg-[#f1f1ed]"><Paperclip size={15} />Sources</button><label className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-[#61645e]" title="Maximum source workbooks held open in one executor batch"><Layers3 size={15} />Batch<input aria-label="Source batch size" type="number" min={1} max={500} value={props.batchSize} onChange={(event) => props.onBatchSize(Math.max(1, Math.min(500, Number(event.target.value) || 1)))} className="w-14 rounded-md border border-[#d8d9d3] bg-[#fafaf8] px-1.5 py-1 text-center text-xs outline-none focus:border-[#6b917c]" /></label><input ref={templateInput} type="file" accept=".xlsx" className="sr-only" onChange={(e) => props.onTemplate(e.target.files?.[0] ?? null)} /><input ref={sourceInput} type="file" accept=".xlsx" multiple className="sr-only" onChange={(e) => props.onSources(Array.from(e.target.files ?? []))} /></div><button onClick={props.onSubmit} disabled={props.busy || !props.template || props.sources.length === 0} className="flex h-9 items-center gap-2 rounded-lg bg-[#275d46] px-3.5 text-xs font-semibold text-white disabled:opacity-40">{props.busy ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}{props.busy ? "Starting task…" : "Analyze and plan"}</button></div>
    </div>{props.error && <p className="mx-auto mt-3 max-w-3xl text-center text-xs text-rose-700">{props.error}</p>}<p className="mt-2 text-center text-[11px] text-[#9a9b96]">The model proposes operations. Deterministic Python performs and verifies the workbook merge.</p></div>
  </div>;
}

function MessageBubble({ role, text, time, showTime }: { role: UIMessage["role"]; text: string; time?: string; showTime: boolean }) {
  const user = role === "user";
  return <div className={`flex gap-3 ${user ? "justify-end" : "justify-start"}`}>{!user && <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-[#e5eee8] text-[#275d46]"><Bot size={14} /></span>}<div className={`max-w-[86%] ${user ? "rounded-2xl rounded-tr-md bg-[#e8e9e4] px-4 py-2.5" : "pt-1"}`}><p className="whitespace-pre-wrap text-sm leading-6">{text}</p>{showTime && <p className={`mt-1 text-[10px] ${user ? "text-right" : ""} text-[#a0a29c]`}>{time ? formatTime(time) : ""}</p>}</div></div>;
}
function JsonDetail({ value }: { value: unknown }) {
  if (value === undefined || value === null) return null;
  return <pre className="mt-2 max-h-44 overflow-auto rounded-lg bg-[#20231f] p-3 text-[10px] leading-5 text-[#d7ddd8]">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>;
}
function ToolCard({ part, run, onApproval }: { part: ToolUIPart; run: Run; onApproval: (id: string, approved: boolean) => void }) {
  const name = getToolName(part), running = part.state === "input-streaming" || part.state === "input-available", failed = part.state === "output-error" || part.state === "output-denied";
  const approval = part.state === "approval-requested" ? part.approval : undefined;
  return <div className="ml-10 overflow-hidden rounded-xl border border-[#d4dbd5] bg-[#fbfbf9]"><details open={part.state === "approval-requested" || failed}><summary className="flex cursor-pointer list-none items-center gap-3 px-3.5 py-3"><span className="grid size-8 place-items-center rounded-lg bg-[#e4eee8] text-[#286348]" aria-label="Tool call"><Wrench size={15} /></span><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><span className="text-[9px] font-bold uppercase tracking-[.1em] text-[#678071]">Tool</span><p className="font-mono text-xs font-semibold text-[#303a33]">{name}</p></div><p className="mt-0.5 text-[11px] text-[#858780]">Called by the agent</p></div><span className={`flex items-center gap-1.5 rounded-full px-2 py-1 text-[9px] font-semibold ${failed ? "bg-rose-100 text-rose-700" : running ? "bg-blue-100 text-blue-700" : "bg-emerald-100 text-emerald-700"}`}>{running ? <Loader2 size={11} className="animate-spin" /> : failed ? <AlertCircle size={11} /> : <Check size={11} />}{part.state.replaceAll("-", " ")}</span><ChevronDown size={14} className="text-[#999b95]" /></summary><div className="border-t border-[#e7e7e2] px-3.5 py-3">{"input" in part && <><p className="text-[10px] font-semibold uppercase tracking-[.09em] text-[#8b8d86]">Parameters</p><JsonDetail value={part.input} /></>}{part.state === "output-available" && <><p className="mt-3 text-[10px] font-semibold uppercase tracking-[.09em] text-[#8b8d86]">Result</p><JsonDetail value={part.output} /></>}{part.state === "output-error" && <p className="mt-2 text-xs text-rose-700">{part.errorText}</p>}{approval && <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3"><p className="text-xs font-semibold text-amber-900">Allow this agent to write local output files?</p><p className="mt-1 text-[11px] leading-5 text-amber-800">This is the only approval for the reviewed plan. It covers staging, bounded recovery, verification, and publication of <b>merged.xlsx</b> and <b>audit.json</b>.</p><p className="mt-1 font-mono text-[10px] text-amber-800">{run.template?.filename} + {run.sources.length} source file{run.sources.length === 1 ? "" : "s"} · batches of {run.batch_size} · {run.spec_hash?.slice(0, 18)}…</p><div className="mt-3 flex gap-2"><button onClick={() => onApproval(approval.id, false)} className="rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs text-amber-900">Cancel</button><button onClick={() => onApproval(approval.id, true)} className="rounded-lg bg-[#275d46] px-3 py-1.5 text-xs font-semibold text-white">Approve file write</button></div></div>}</div></details></div>;
}

function isExecutionTool(part: ToolUIPart): boolean {
  const name = getToolName(part);
  return name === "execute_approved_merge" || name === "execute_merge_configuration";
}

function MessageParts({ message, run, onApproval }: { message: UIMessage; run: Run; onApproval: (id: string, approved: boolean) => void }) {
  const textIndexes = message.parts.flatMap((part, index) => part.type === "text" ? [index] : []);
  const lastTextIndex = textIndexes.at(-1);
  return <div className="space-y-3">{message.parts.map((part, index) => {
    if (part.type === "text") return <MessageBubble key={`${message.id}-text-${index}`} role={message.role} text={part.text} time={metadataTime(message)} showTime={index === lastTextIndex} />;
    if (isToolUIPart(part)) {
      const completedExecution = run.state === "completed" && isExecutionTool(part) && part.state === "output-available";
      return <Fragment key={`${message.id}-tool-${index}`}><ToolCard part={part} run={run} onApproval={onApproval} />{completedExecution && <ResultCard run={run} />}</Fragment>;
    }
    return null;
  })}</div>;
}

function RunEventCard({ event, pending }: { event: RunEvent; pending?: boolean }) {
  const display = eventLabels[event.kind] ?? { title: event.message }, failed = event.kind.includes("failed") || event.kind === "run_cancelled";
  return <div className="ml-10 flex items-start gap-3 rounded-xl border border-[#e2e3de] bg-white px-3.5 py-3"><span className={`mt-0.5 grid size-7 shrink-0 place-items-center rounded-md ${failed ? "bg-rose-100 text-rose-700" : pending ? "bg-blue-100 text-blue-700" : "bg-[#edf2ed] text-[#4f755f]"}`}>{display.tool ? <Wrench size={13} /> : pending ? <Loader2 size={13} className="animate-spin" /> : failed ? <AlertCircle size={13} /> : <Check size={13} />}</span><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2">{display.tool && <span className="text-[9px] font-bold uppercase tracking-[.1em] text-[#678071]">Tool</span>}<p className="text-xs font-semibold">{display.title}</p>{display.tool && <span className="rounded bg-[#f0f1ed] px-1.5 py-0.5 font-mono text-[9px] text-[#59665d]">{display.tool}</span>}</div><p className="mt-1 text-[11px] leading-5 text-[#7d8079]">{event.message}</p></div><time className="text-[10px] text-[#a1a39d]">{formatTime(event.at)}</time></div>;
}
function PlanCard({ run, busy, onApprove, onExecute }: { run: Run; busy: boolean; onApprove: () => void; onExecute: () => void }) {
  if (!run.spec) return null;
  const approved = run.approved_spec_hash === run.spec_hash, unresolved = run.conflicts.filter((c) => !c.resolution).length;
  return <div className="ml-10 overflow-hidden rounded-2xl border border-[#d8ded9] bg-white shadow-[0_5px_18px_rgba(37,50,41,.05)]"><div className="flex flex-wrap items-start justify-between gap-3 border-b border-[#e7e9e5] px-4 py-4"><div><div className="flex items-center gap-2"><Sparkles size={15} className="text-[#347257]" /><p className="text-sm font-semibold">Proposed merge plan</p></div><p className="mt-1 text-[11px] text-[#858880]">{run.planner?.model ?? "Configured model"} · {run.spec.operations.length} operations</p></div><span className={`rounded-full px-2.5 py-1 text-[10px] font-semibold ${approved ? "bg-emerald-100 text-emerald-800" : "bg-amber-100 text-amber-800"}`}>{approved ? "Approved" : "Review required"}</span></div><div className="space-y-2 p-4">{run.spec.operations.map((op) => <details key={op.id} className="group rounded-xl border border-[#e4e6e1] bg-[#fafbf8]" open={run.spec!.operations.length <= 3}><summary className="flex cursor-pointer list-none items-center gap-3 px-3.5 py-3"><span className={`rounded-md px-2 py-1 text-[9px] font-bold uppercase ${op.mode === "add" ? "bg-blue-100 text-blue-800" : "bg-violet-100 text-violet-800"}`}>{op.mode}</span><div className="min-w-0 flex-1"><p className="truncate text-xs font-semibold">{op.source_sheet && op.source_sheet !== op.sheet ? `${op.source_sheet} → ${op.sheet}` : op.sheet}</p><p className="mt-0.5 truncate text-[11px] text-[#84877f]">{op.description}</p></div><ChevronDown size={14} className="text-[#989a94] transition group-open:rotate-180" /></summary><div className="border-t border-[#e8e9e5] px-3.5 py-3 text-[11px] leading-5 text-[#6f736b]"><p><b className="font-medium text-[#444842]">Alignment:</b> {op.alignment ?? "row key"} · <b className="font-medium text-[#444842]">Placement:</b> {(op.placement ?? "in_place").replaceAll("_", " ")}</p>{op.row_keys.length > 0 && <p><b className="font-medium text-[#444842]">Keys:</b> {op.row_keys.join(", ")}</p>}{op.value_columns.length > 0 && <p><b className="font-medium text-[#444842]">Value columns:</b> {op.value_columns.join(", ")}</p>}{operationFilters(op).length > 0 && <p><b className="font-medium text-[#444842]">Filters:</b> {operationFilters(op).join(" · ")}</p>}</div></details>)}</div><div className="border-t border-[#e7e9e5] bg-[#fbfcfa] px-4 py-3"><p className="text-xs leading-5 text-[#6f736c]">{run.spec.rationale}</p><div className="mt-2 flex flex-wrap gap-1.5">{run.spec.guideline_citations.map((c) => <span key={c} className="rounded bg-[#edf1ed] px-2 py-1 font-mono text-[9px] text-[#5c695f]">{c}</span>)}</div><div className="mt-4 flex flex-wrap items-center justify-between gap-3"><p className="font-mono text-[9px] text-[#9a9c96]">SHA-256 {run.spec_hash?.slice(0, 18)}…</p>{!approved ? <button onClick={onApprove} disabled={busy} className="flex items-center gap-2 rounded-lg bg-[#275d46] px-3.5 py-2 text-xs font-semibold text-white disabled:opacity-40"><ShieldCheck size={14} />Approve exact plan</button> : unresolved > 0 ? <span className="text-[11px] font-medium text-amber-700">Resolve {unresolved} conflict{unresolved === 1 ? "" : "s"}</span> : run.state === "plan_ready" ? <button onClick={onExecute} disabled={busy} className="flex items-center gap-2 rounded-lg bg-[#275d46] px-3.5 py-2 text-xs font-semibold text-white disabled:opacity-40"><Play size={14} />Ask agent to execute</button> : null}</div></div></div>;
}

function ReviewPlanCard({ run, busy, onExecute }: { run: Run; busy: boolean; onExecute: () => void }) {
  if (!run.spec) return null;
  const unresolved = run.conflicts.filter((conflict) => !conflict.resolution).length;
  return <div className="ml-10 overflow-hidden rounded-2xl border border-[#d8ded9] bg-white shadow-[0_5px_18px_rgba(37,50,41,.05)]">
    <div className="flex items-start justify-between gap-3 border-b border-[#e7e9e5] px-4 py-4"><div><div className="flex items-center gap-2"><Sparkles size={15} className="text-[#347257]" /><p className="text-sm font-semibold">Reviewed merge plan</p></div><p className="mt-1 text-[11px] text-[#858880]">{run.planner?.model ?? "Configured model"} · {run.spec.operations.length} operations · no write has occurred</p></div><span className="rounded-full bg-blue-100 px-2.5 py-1 text-[10px] font-semibold text-blue-800">Ready for review</span></div>
    <div className="space-y-2 p-4">{run.spec.operations.map((op) => {
      const compiled = run.compiled_plan?.operations.find((item) => item.operation_id === op.id);
      const shifted = compiled?.sources.flatMap((source) => source.columns.filter((column) => column.source_column !== column.target_column).map((column) => `${source.source_file}: ${column.source_column} → ${column.target_column}`)) ?? [];
      const mappedRows = compiled?.sources.reduce((count, source) => count + source.rows.length, 0) ?? 0;
      return <details key={op.id} className="rounded-xl border border-[#e4e6e1] bg-[#fafbf8]" open={shifted.length > 0}><summary className="flex cursor-pointer list-none items-start gap-3 px-3.5 py-3"><span className={`rounded-md px-2 py-1 text-[9px] font-bold uppercase ${op.mode === "add" ? "bg-blue-100 text-blue-800" : "bg-violet-100 text-violet-800"}`}>{op.mode}</span><div className="min-w-0 flex-1"><p className="truncate text-xs font-semibold">{op.source_sheet && op.source_sheet !== op.sheet ? `${op.source_sheet} → ${op.sheet}` : op.sheet}</p><p className="mt-0.5 text-[11px] text-[#84877f]">{op.description}</p><p className="mt-1 text-[10px] text-[#6f776f]">{compiled?.sources.length ?? 0} source mapping{compiled?.sources.length === 1 ? "" : "s"} · {mappedRows} rows</p></div><ChevronDown size={14} className="mt-1 text-[#989a94]" /></summary><div className="border-t border-[#e5e7e2] px-3.5 py-3 text-[10px] leading-5 text-[#6f736b]">{shifted.length > 0 ? <><p className="font-semibold text-amber-800">Structural differences resolved by headers</p>{shifted.slice(0, 12).map((mapping) => <p key={mapping} className="font-mono">{mapping}</p>)}</> : <p>All reviewed source columns align with the template. Exact row and column mappings are stored in the audit.</p>}{operationFilters(op).length > 0 && <p className="mt-1"><b>Excluded rows:</b> {operationFilters(op).join(" · ")}</p>}</div></details>;
    })}</div>
    <div className="border-t border-[#e7e9e5] bg-[#fbfcfa] px-4 py-3"><p className="text-xs leading-5 text-[#6f736c]">{run.spec.rationale}</p><div className="mt-3 flex flex-wrap items-center justify-between gap-3"><p className="font-mono text-[9px] text-[#9a9c96]">SHA-256 {run.spec_hash?.slice(0, 18)}…</p>{unresolved > 0 ? <span className="text-[11px] font-medium text-amber-700">One answer will be reused for matching issues · {unresolved} occurrence{unresolved === 1 ? "" : "s"}</span> : run.state !== "completed" ? <button onClick={onExecute} disabled={busy} className="flex items-center gap-2 rounded-lg bg-[#275d46] px-3.5 py-2 text-xs font-semibold text-white disabled:opacity-40"><Play size={14} />Run merge</button> : null}</div></div>
  </div>;
}
void PlanCard;

function ConflictCard({ conflict, busy, onResolve }: { conflict: Conflict; busy: boolean; onResolve: (action: string) => void }) {
  return <div className="ml-10 rounded-xl border border-amber-200 bg-amber-50/70 p-4"><div className="flex items-start gap-3"><AlertCircle size={17} className="mt-0.5 shrink-0 text-amber-700" /><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><p className="text-xs font-semibold text-amber-950">Preflight decision required</p>{conflict.resolution && <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[9px] font-semibold text-emerald-800">Resolved · {conflictLabels[conflict.resolution] ?? conflict.resolution}</span>}</div><p className="mt-2 text-xs leading-5 text-amber-900">{conflict.message}</p><p className="mt-1 font-mono text-[10px] text-amber-800/70">{conflict.source_file} · {conflict.sheet}{conflict.cell ? ` · ${conflict.cell}` : ""}</p>{!conflict.resolution && <div className="mt-3 flex flex-wrap gap-2">{conflict.allowed_actions.map((action) => <button key={action} onClick={() => onResolve(action)} disabled={busy} className={`rounded-lg border px-3 py-1.5 text-[11px] font-medium disabled:opacity-40 ${action === "abort" ? "border-rose-300 bg-white text-rose-800" : "border-amber-300 bg-white text-amber-900"}`}>{conflictLabels[action] ?? action}</button>)}</div>}</div></div></div>;
}
void ConflictCard;
function UserKnowledgeCard({ conflict, run, busy, onResolve }: { conflict: Conflict; run: Run; busy: boolean; onResolve: (action: string) => void }) {
  const matching = run.conflicts.filter((item) => (item.type ?? "conflict") === (conflict.type ?? "conflict") && JSON.stringify(item.actual) === JSON.stringify(conflict.actual));
  return <div className="ml-10 rounded-xl border border-amber-200 bg-amber-50/70 p-4"><div className="flex items-start gap-3"><AlertCircle size={17} className="mt-0.5 shrink-0 text-amber-700" /><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><p className="text-xs font-semibold text-amber-950">Business meaning required</p>{matching.length > 1 && <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[9px] font-semibold text-amber-800">{matching.length} matching occurrences</span>}{conflict.resolution && <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[9px] font-semibold text-emerald-800">Answered · {conflictLabels[conflict.resolution] ?? conflict.resolution}</span>}</div><p className="mt-2 text-xs leading-5 text-amber-900">{conflict.message}</p><p className="mt-1 text-[10px] text-amber-800/70">The files do not establish a unique interpretation. Your answer will be reused for identical occurrences in this task.</p>{!conflict.resolution && <div className="mt-3 flex flex-wrap gap-2">{conflict.allowed_actions.map((action) => <button key={action} onClick={() => onResolve(action)} disabled={busy} className={`rounded-lg border px-3 py-1.5 text-[11px] font-medium disabled:opacity-40 ${action === "abort" ? "border-rose-300 bg-white text-rose-800" : "border-amber-300 bg-white text-amber-900"}`}>{conflictLabels[action] ?? action}</button>)}</div>}</div></div></div>;
}
function DecisionCard({ decision, busy, onRespond }: { decision: RuntimeDecision; busy: boolean; onRespond: (action: string, note: string) => void }) {
  const [note, setNote] = useState(decision.user_note ?? "");
  return <div className="ml-10 rounded-2xl border border-amber-300 bg-[#fffaf0] p-4"><div className="flex items-start gap-3"><span className="grid size-8 shrink-0 place-items-center rounded-lg bg-amber-100 text-amber-800"><AlertCircle size={16} /></span><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><p className="text-sm font-semibold text-[#5f451b]">{decision.question}</p><span className="rounded bg-amber-100 px-1.5 py-0.5 font-mono text-[9px] text-amber-800">{decision.phase}/{decision.code}</span></div><p className="mt-2 text-xs leading-5 text-[#7d6948]">{decision.message}</p>{Object.keys(decision.context).length > 0 && <details className="mt-2"><summary className="cursor-pointer text-[10px] font-medium text-[#8b734d]">Show runtime context</summary><JsonDetail value={decision.context} /></details>}{decision.selected_action ? <div className="mt-3 flex items-center gap-2 text-xs font-medium text-emerald-800"><CheckCircle2 size={14} />Instruction recorded: {decision.options.find((o) => o.action === decision.selected_action)?.label ?? decision.selected_action}</div> : <><textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional instruction for the audit trail" className="mt-3 min-h-16 w-full rounded-lg border border-amber-200 bg-white px-3 py-2 text-xs outline-none" /><div className="mt-3 grid gap-2 sm:grid-cols-2">{decision.options.map((option) => <button key={option.action} onClick={() => onRespond(option.action, note)} disabled={busy} className={`rounded-lg border bg-white px-3 py-2 text-left disabled:opacity-40 ${option.action === "abort" ? "border-rose-200 text-rose-800" : "border-amber-300 text-[#6a4b1d]"}`}><span className="block text-xs font-semibold">{option.label}</span><span className="mt-0.5 block text-[10px] leading-4 opacity-70">{option.description}</span></button>)}</div></>}</div></div></div>;
}
function ResultCard({ run }: { run: Run }) {
  const checks = run.verification?.checks ?? [], cells = checks.find((check) => check.name === "cell_reconciliation")?.expected_cells ?? 0, rows = checks.find((check) => check.name === "row_reconciliation")?.expected_rows ?? 0;
  return <div className="ml-10 overflow-hidden rounded-2xl border border-emerald-200 bg-emerald-50"><div className="flex items-start gap-3 p-4"><span className="grid size-9 shrink-0 place-items-center rounded-xl bg-emerald-700 text-white"><CheckCircle2 size={19} /></span><div><p className="text-sm font-semibold text-emerald-950">Merged workbook reconciled</p><p className="mt-1 text-xs text-emerald-800">{checks.filter((check) => check.passed).length} checks passed · {cells} cells and {rows} rows matched expected sources</p><p className="mt-1 text-[11px] text-emerald-800">{run.execution_attempts} execution attempt{run.execution_attempts === 1 ? "" : "s"} · {run.batch_progress?.total_work_units ?? 0} bounded batch units · unchanged template regions verified</p>{run.excluded_sources.length > 0 && <p className="mt-1 text-[11px] text-emerald-800">{run.excluded_sources.length} source{run.excluded_sources.length === 1 ? "" : "s"} excluded by recorded instruction</p>}</div></div><div className="flex flex-wrap gap-2 border-t border-emerald-200 bg-white/50 px-4 py-3"><a href={`${API}/api/runs/${run.id}/output`} className="flex items-center gap-2 rounded-lg bg-emerald-800 px-3 py-2 text-xs font-semibold text-white"><Download size={14} />Download merged .xlsx</a><a href={`${API}/api/runs/${run.id}/audit`} className="flex items-center gap-2 rounded-lg border border-emerald-300 bg-white px-3 py-2 text-xs text-emerald-900"><FileJson size={14} />Audit and lineage</a></div></div>;
}

type FeedItem = { id: string; time: number; kind: "message"; message: UIMessage } | { id: string; time: number; kind: "event"; event: RunEvent } | { id: string; time: number; kind: "plan" } | { id: string; time: number; kind: "conflict"; conflict: Conflict } | { id: string; time: number; kind: "decision"; decision: RuntimeDecision } | { id: string; time: number; kind: "result" };
function buildFeed(run: Run, messages: UIMessage[]): FeedItem[] {
  const items: FeedItem[] = [], first = new Date(run.created_at).getTime();
  let conversationCursor = first;
  messages.forEach((message) => {
    const explicit = metadataTime(message);
    if (message.role === "user" && explicit) conversationCursor = new Date(explicit).getTime();
    else conversationCursor += 1;
    items.push({ id: `message-${message.id}`, kind: "message", message, time: conversationCursor });
  });
  const toolParts = messages.flatMap((message) => message.parts.filter(isToolUIPart));
  const hasExecutionTool = toolParts.some(isExecutionTool);
  const hasCompletedExecutionTool = toolParts.some((part) => isExecutionTool(part) && part.state === "output-available");
  run.events.forEach((event, i) => {
    if (hasExecutionTool && (event.kind === "executor_tool_started" || event.kind === "execution_completed")) return;
    items.push({ id: `event-${event.at}-${i}`, kind: "event", event, time: new Date(event.at).getTime() });
  });
  const planEvent = run.events.find((event) => event.kind === "model_plan_ready"), planTime = planEvent ? new Date(planEvent.at).getTime() : first;
  if (run.spec) items.push({ id: "plan", kind: "plan", time: planTime + 1 });
  const conflictGroups = new Set<string>();
  run.conflicts.forEach((conflict, i) => {
    const key = `${conflict.type ?? "conflict"}:${JSON.stringify(conflict.actual)}`;
    if (conflictGroups.has(key)) return;
    conflictGroups.add(key);
    items.push({ id: `conflict-${conflict.id}`, kind: "conflict", conflict, time: planTime + 2 + i });
  });
  run.decisions.forEach((decision) => items.push({ id: `decision-${decision.id}`, kind: "decision", decision, time: new Date(decision.created_at).getTime() + 1 }));
  const complete = run.events.find((event) => event.kind === "execution_completed");
  if (run.state === "completed" && complete && !hasCompletedExecutionTool) items.push({ id: "result", kind: "result", time: new Date(complete.at).getTime() + 1 });
  return items.sort((a, b) => a.time - b.time);
}

function RunConversation({ run, busy, onRunChange, onError }: { run: Run; busy: boolean; onRunChange: (run: Run) => void; onError: (message: string | null) => void }) {
  const [input, setInput] = useState(""), [actionBusy, setActionBusy] = useState(false), bottom = useRef<HTMLDivElement>(null), scroller = useRef<HTMLDivElement>(null), followLatest = useRef(true);
  const transport = useMemo(() => new DefaultChatTransport({ api: `${API}/api/runs/${run.id}/chat` }), [run.id]);
  const { messages, sendMessage, status, error, addToolApprovalResponse, stop } = useChat({ id: run.id, messages: run.conversation ?? [], transport, sendAutomaticallyWhen: lastAssistantMessageIsCompleteWithApprovalResponses,
    onFinish: ({ messages: finished }) => { void (async () => { try { await api<Run>(`/api/runs/${run.id}/conversation`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ messages: withMessageTimes(finished) }) }); onRunChange(await api<Run>(`/api/runs/${run.id}`)); } catch (reason) { onError(reason instanceof Error ? reason.message : "Could not persist the conversation"); } })(); },
  });
  const active = status === "submitted" || status === "streaming", interactionBusy = busy || active || actionBusy, feed = useMemo(() => buildFeed(run, messages), [run, messages]);
  useEffect(() => { if (followLatest.current) bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [feed.length, status]);
  useEffect(() => {
    if (!active && !["executing", "recovering"].includes(run.state)) return;
    const timer = window.setInterval(() => {
      void api<Run>(`/api/runs/${run.id}`).then(onRunChange).catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [active, run.id, run.state, onRunChange]);
  async function resolveConflict(conflict: Conflict, action: string) { setActionBusy(true); try { onRunChange(await api<Run>(`/api/runs/${run.id}/conflicts/${conflict.id}/resolve`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ action, scope: "identical_in_run" }) })); } catch (e) { onError(e instanceof Error ? e.message : "Conflict resolution failed"); } finally { setActionBusy(false); } }
  async function respond(decision: RuntimeDecision, action: string, note: string) { setActionBusy(true); try { let updated = await api<Run>(`/api/runs/${run.id}/decisions/${decision.id}/resolve`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ action, note: note.trim() || null }) }); onRunChange(updated); if (action === "return_to_planning") updated = await api<Run>(`/api/runs/${run.id}/inspect`, { method: "POST" }); else if (updated.state === "plan_ready") updated = await api<Run>(`/api/runs/${run.id}/execute`, { method: "POST" }); onRunChange(updated); } catch (e) { onError(e instanceof Error ? e.message : "Could not resume the task"); } finally { setActionBusy(false); } }
  async function retryPlanning() { setActionBusy(true); onError(null); try { onRunChange(await api<Run>(`/api/runs/${run.id}/inspect`, { method: "POST" })); } catch (e) { onError(e instanceof Error ? e.message : "Model planning retry failed"); onRunChange(await api<Run>(`/api/runs/${run.id}`)); } finally { setActionBusy(false); } }
  function submit(event: FormEvent) { event.preventDefault(); const text = input.trim(); if (!text || active) return; setInput(""); void sendMessage({ text, metadata: { createdAt: new Date().toISOString() } }); }
  function execute() { if (!active) void sendMessage({ text: "Run the reviewed merge plan now. Use the write tool directly so I receive one approval immediately before local output files are written.", metadata: { createdAt: new Date().toISOString() } }); }
  const planningFailed = run.state === "failed" && run.events.some((event) => event.kind === "planning_failed");
  return <div className="flex min-h-0 flex-1 flex-col"><div ref={scroller} onScroll={() => { const node = scroller.current; if (node) followLatest.current = node.scrollHeight - node.scrollTop - node.clientHeight < 120; }} className="min-h-0 flex-1 overflow-y-auto"><div className="mx-auto w-full max-w-3xl space-y-4 px-4 py-7 md:px-6 md:py-9">{feed.map((item) => <Fragment key={item.id}>{item.kind === "message" && <MessageParts message={item.message} run={run} onApproval={(id, approved) => void addToolApprovalResponse({ id, approved })} />}{item.kind === "event" && <RunEventCard event={item.event} pending={busy && item.event.kind === "inspection_completed"} />}{item.kind === "plan" && <ReviewPlanCard run={run} busy={interactionBusy} onExecute={execute} />}{item.kind === "conflict" && <UserKnowledgeCard conflict={item.conflict} run={run} busy={interactionBusy} onResolve={(action) => void resolveConflict(item.conflict, action)} />}{item.kind === "decision" && <DecisionCard decision={item.decision} busy={interactionBusy} onRespond={(action, note) => void respond(item.decision, action, note)} />}{item.kind === "result" && <ResultCard run={run} />}</Fragment>)}{interactionBusy && <div className="ml-10 flex items-center gap-2 py-2 text-xs text-[#747770]"><Loader2 size={14} className="animate-spin text-[#347257]" />{busy ? "The agent is inspecting and planning…" : actionBusy ? "Applying your instruction…" : status === "submitted" ? "Agent is starting…" : "Agent is working…"}</div>}{(error || run.error) && <div className="ml-10 flex items-start gap-2 rounded-xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-800"><AlertCircle size={15} className="mt-0.5 shrink-0" /><div><p>{error?.message ?? run.error}</p>{planningFailed && <button onClick={() => void retryPlanning()} disabled={interactionBusy} className="mt-3 rounded-lg border border-rose-300 bg-white px-3 py-1.5 text-xs font-semibold text-rose-800 disabled:opacity-40">Retry model planning</button>}</div></div>}<div ref={bottom} /></div></div><div className="border-t border-[#e5e5e0] bg-[#f9f9f7]/95 px-4 py-4 md:px-8 md:py-5"><form onSubmit={submit} className="mx-auto max-w-3xl rounded-2xl border border-[#d8d9d3] bg-white p-3 shadow-[0_7px_24px_rgba(38,42,36,.07)]"><textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }} className="min-h-14 w-full resize-none bg-transparent px-2 py-1 text-sm leading-6 outline-none placeholder:text-[#999b95]" placeholder="Ask about the plan, conflicts, execution, or verification…" /><div className="mt-1 flex items-center justify-between"><div className="flex items-center gap-2 text-[10px] text-[#999b95]"><Bot size={13} />{run.planner?.model ?? "Configured agent"}{run.planner && (run.planner.attempts ?? 1) > 1 ? ` · ${run.planner.attempts} planning attempts` : ""}</div>{active ? <button type="button" onClick={() => void stop()} className="grid size-9 place-items-center rounded-lg bg-[#ecece8]" aria-label="Stop"><Square size={13} fill="currentColor" /></button> : <button disabled={!input.trim() || actionBusy} className="grid size-9 place-items-center rounded-lg bg-[#275d46] text-white disabled:opacity-35" aria-label="Send"><ArrowDown size={16} className="-rotate-90" /></button>}</div></form></div></div>;
}

function TaskDetails({ run }: { run: Run | null }) {
  if (!run) return <div className="rounded-xl border border-dashed border-[#d4d5cf] px-4 py-5 text-center"><FileSpreadsheet className="mx-auto text-[#9a9c96]" size={20} /><p className="mt-2 text-xs font-medium">No active task</p><p className="mt-1 text-[11px] text-[#8b8d87]">Files, plan, and verification will appear here.</p></div>;
  const stages = [["Files", Boolean(run.template && run.sources.length)], ["Plan", Boolean(run.spec)], ["Questions", run.conflicts.every((item) => Boolean(item.resolution))], ["Write approval", run.approved_spec_hash === run.spec_hash], ["Execution", run.execution_attempts > 0], ["Verified", run.state === "completed"]] as const;
  const current = stages.findIndex(([, done]) => !done);
  const batch = run.batch_progress;
  const batchPercent = batch && batch.total_work_units > 0 ? Math.round(batch.completed_work_units / batch.total_work_units * 100) : 0;
  return <div className="space-y-5"><section><p className="text-[10px] font-semibold uppercase tracking-[.1em] text-[#8d8f89]">Progress</p><div className="mt-3 space-y-2.5">{stages.map(([label, done], i) => <div key={label} className="flex items-center gap-2.5 text-xs"><span className={`grid size-5 place-items-center rounded-full ${done ? "bg-[#dfeee5] text-[#2e7553]" : i === current ? "bg-blue-100 text-blue-700" : "bg-[#ecece8] text-[#a0a29b]"}`}>{done ? <Check size={11} /> : i === current ? <Clock3 size={11} /> : <Circle size={8} />}</span><span className={done ? "text-[#444842]" : "text-[#858880]"}>{label}</span></div>)}</div></section><section className="border-t border-[#e6e6e1] pt-5"><div className="flex items-center justify-between"><p className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[.1em] text-[#8d8f89]"><Layers3 size={13} />Source batches</p><span className="text-[10px] text-[#858880]">size {run.batch_size}</span></div>{batch ? <div className="mt-3"><div className="h-1.5 overflow-hidden rounded-full bg-[#e7e8e3]"><div className="h-full rounded-full bg-[#347257] transition-[width]" style={{ width: `${batchPercent}%` }} /></div><div className="mt-2 flex justify-between text-[10px] text-[#777a73]"><span>{batch.completed_work_units}/{batch.total_work_units} work units</span><span>{batchPercent}%</span></div>{batch.current_operation && <p className="mt-1 truncate text-[10px] text-[#777a73]">{batch.current_operation} · batch {batch.current_batch}/{batch.batches_in_operation}</p>}<p className="mt-1 text-[10px] text-[#92948e]">{batch.processed_sources}/{batch.total_sources} sources reached</p></div> : <p className="mt-2 text-[11px] leading-5 text-[#858880]">The executor will keep at most {run.batch_size} source workbooks in each processing batch.</p>}</section><section className="border-t border-[#e6e6e1] pt-5"><p className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[.1em] text-[#8d8f89]"><Files size={13} />Files</p><div className="mt-3 space-y-2">{run.template && <WorkbookSummary file={run.template} label="Template" />}{run.sources.map((source) => <WorkbookSummary key={source.id || source.sha256} file={source} label={run.excluded_sources.includes(source.id) || run.excluded_sources.includes(source.filename) ? "Excluded source" : "Source"} />)}</div></section>{run.spec && <section className="border-t border-[#e6e6e1] pt-5"><p className="text-[10px] font-semibold uppercase tracking-[.1em] text-[#8d8f89]">Plan summary</p><div className="mt-3 space-y-2">{run.spec.operations.map((op) => <div key={op.id} className="flex items-center justify-between gap-2 text-xs"><span className="truncate">{op.sheet}</span><span className={`rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase ${op.mode === "add" ? "bg-blue-100 text-blue-800" : "bg-violet-100 text-violet-800"}`}>{op.mode}</span></div>)}</div></section>}<section className="border-t border-[#e6e6e1] pt-5"><div className="flex items-center gap-2 text-[#3e6b53]"><ShieldCheck size={15} /><p className="text-xs font-semibold">Execution safeguards</p></div><ul className="mt-3 space-y-2 text-[11px] text-[#777a73]"><li>Exact plan, mapping, and batch configuration binding</li><li>Untouched original workbooks</li><li>Independent cell and row reconciliation</li><li>Persistent human decisions</li></ul></section></div>;
}
function WorkbookSummary({ file, label }: { file: UploadedWorkbook; label: string }) { return <div className="rounded-lg border border-[#e1e3dd] bg-white px-3 py-2"><p className="truncate text-xs font-medium">{file.filename}</p><p className="mt-0.5 text-[9px] uppercase tracking-[.08em] text-[#858880]">{label}</p></div>; }

const providerDefaults: Record<string, string> = {
  openai: "https://api.openai.com/v1",
  minimax: "https://api.minimax.io/v1",
  deepseek: "https://api.deepseek.com",
  custom: "",
};
function normalizedEndpoint(value: string): string { return value.trim().replace(/\/+$/, ""); }

function ModelConnectionsDialog({ connections, onClose, onChanged, onError }: { connections: ModelConnections | null; onClose: () => void; onChanged: (value: ModelConnections) => void; onError: (value: string) => void }) {
  const [selected, setSelected] = useState(connections?.default ?? "");
  const current = connections?.profiles.find((item) => item.id === selected);
  const [profileId, setProfileId] = useState(current?.id ?? "");
  const [provider, setProvider] = useState<ModelProfileSummary["provider"]>(current?.provider ?? "custom");
  const [baseUrl, setBaseUrl] = useState(current?.base_url ?? "");
  const [model, setModel] = useState(current?.model ?? "");
  const [apiKey, setApiKey] = useState("");
  const [timeout, setTimeoutValue] = useState(current?.timeout ?? 60);
  const [busy, setBusy] = useState(false), [probe, setProbe] = useState<string | null>(null);
  const hasUnsavedChanges = !current
    || profileId.trim() !== current.id
    || provider !== current.provider
    || normalizedEndpoint(baseUrl) !== normalizedEndpoint(current.base_url)
    || model.trim() !== current.model
    || timeout !== current.timeout
    || apiKey.length > 0;

  function choose(id: string) {
    setSelected(id);
    const item = connections?.profiles.find((profile) => profile.id === id);
    if (!item) return;
    setProfileId(item.id); setProvider(item.provider); setBaseUrl(item.base_url);
    setModel(item.model); setTimeoutValue(item.timeout); setApiKey(""); setProbe(null);
  }
  function newProfile() {
    setSelected(""); setProfileId(""); setProvider("custom"); setBaseUrl("");
    setModel(""); setTimeoutValue(60); setApiKey(""); setProbe(null);
  }
  async function save() {
    if (!profileId.trim() || !baseUrl.trim() || !model.trim()) return;
    setBusy(true); setProbe(null);
    try {
      const updated = await api<ModelConnections>(`/api/model-connections/${encodeURIComponent(profileId.trim())}`, {
        method: "PUT", headers: { "content-type": "application/json" },
        body: JSON.stringify({ provider, base_url: baseUrl.trim(), model: model.trim(), api_key: apiKey || null, timeout }),
      });
      setApiKey(""); setSelected(profileId.trim()); onChanged(updated); setProbe("Saved locally");
    } catch (reason) { onError(reason instanceof Error ? reason.message : "Could not save model connection"); }
    finally { setBusy(false); }
  }
  async function testConnection() {
    if (hasUnsavedChanges || !selected) {
      setProbe("Save this connection locally before testing it.");
      return;
    }
    setBusy(true); setProbe("Testing streaming and tool calls…");
    try {
      const result = await api<{ connected: boolean; planning_compatible: boolean }>(`/api/model-connections/${encodeURIComponent(selected)}/probe`, { method: "POST" });
      setProbe(result.connected && result.planning_compatible ? "Connected · planning tools supported" : result.connected ? "Connected · planning tool call unsupported" : "Connection failed");
    } catch (reason) { const message = reason instanceof Error ? reason.message : "Connection test failed"; setProbe(message); onError(message); }
    finally { setBusy(false); }
  }
  async function activate() {
    if (!selected) return;
    setBusy(true);
    try { onChanged(await api<ModelConnections>(`/api/model-connections/${encodeURIComponent(selected)}/activate`, { method: "POST" })); setProbe("Used for new merge tasks"); }
    catch (reason) { onError(reason instanceof Error ? reason.message : "Could not select model"); }
    finally { setBusy(false); }
  }
  return <div className="fixed inset-0 z-50 grid place-items-center bg-black/35 p-4" onClick={onClose}><div className="flex max-h-[88vh] w-full max-w-3xl overflow-hidden rounded-2xl border border-[#d9dbd5] bg-[#fbfbf9] shadow-2xl" onClick={(event) => event.stopPropagation()}>
    <aside className="w-52 shrink-0 border-r border-[#e1e2dd] bg-[#f0f0ec] p-3"><div className="flex items-center justify-between px-2 py-2"><p className="text-xs font-semibold">Model connections</p><button onClick={newProfile} className="rounded-md border border-[#d6d8d1] bg-white px-2 py-1 text-[10px]">New</button></div><div className="mt-2 space-y-1">{connections?.profiles.map((item) => <button key={item.id} onClick={() => choose(item.id)} className={`w-full rounded-lg px-2.5 py-2 text-left ${selected === item.id ? "bg-[#dce4dd]" : "hover:bg-[#e5e6e0]"}`}><p className="truncate text-xs font-medium">{item.id}</p><p className="mt-0.5 truncate text-[9px] text-[#7d8079]">{item.provider} · {item.model}</p>{item.is_default && <span className="mt-1 inline-block rounded bg-[#275d46] px-1.5 py-0.5 text-[8px] font-semibold uppercase text-white">Active</span>}</button>)}</div></aside>
    <section className="min-w-0 flex-1 overflow-y-auto p-5"><div className="flex items-start justify-between"><div><p className="flex items-center gap-2 text-sm font-semibold"><KeyRound size={16} className="text-[#347257]" />Local LLM connection</p><p className="mt-1 text-[11px] text-[#7e817a]">The API key is stored only in the backend&apos;s private local keys file and is never returned to this page.</p></div><button onClick={onClose}><X size={17} /></button></div>
      <div className="mt-5 grid gap-4 sm:grid-cols-2"><label className="text-[11px] font-medium text-[#555951]">Profile name<input value={profileId} onChange={(e) => setProfileId(e.target.value)} className="mt-1.5 w-full rounded-lg border border-[#d7d9d3] bg-white px-3 py-2 text-xs outline-none focus:border-[#6b917c]" placeholder="my-model" /></label><label className="text-[11px] font-medium text-[#555951]">Provider<select value={provider} onChange={(e) => { const next = e.target.value as ModelProfileSummary["provider"]; setProvider(next); if (!baseUrl || Object.values(providerDefaults).includes(baseUrl)) setBaseUrl(providerDefaults[next]); }} className="mt-1.5 w-full rounded-lg border border-[#d7d9d3] bg-white px-3 py-2 text-xs"><option value="openai">OpenAI</option><option value="minimax">MiniMax</option><option value="deepseek">DeepSeek</option><option value="custom">Other OpenAI-compatible</option></select></label><label className="text-[11px] font-medium text-[#555951] sm:col-span-2">Endpoint address<input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} className="mt-1.5 w-full rounded-lg border border-[#d7d9d3] bg-white px-3 py-2 font-mono text-xs outline-none focus:border-[#6b917c]" placeholder="https://provider.example/v1" /></label><label className="text-[11px] font-medium text-[#555951]">Model name<input value={model} onChange={(e) => setModel(e.target.value)} className="mt-1.5 w-full rounded-lg border border-[#d7d9d3] bg-white px-3 py-2 text-xs outline-none focus:border-[#6b917c]" placeholder="model-id" /></label><label className="text-[11px] font-medium text-[#555951]">Timeout (seconds)<input type="number" min={1} max={600} value={timeout} onChange={(e) => setTimeoutValue(Math.max(1, Math.min(600, Number(e.target.value) || 60)))} className="mt-1.5 w-full rounded-lg border border-[#d7d9d3] bg-white px-3 py-2 text-xs outline-none focus:border-[#6b917c]" /></label><label className="text-[11px] font-medium text-[#555951] sm:col-span-2">API key<input type="password" autoComplete="new-password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} className="mt-1.5 w-full rounded-lg border border-[#d7d9d3] bg-white px-3 py-2 text-xs outline-none focus:border-[#6b917c]" placeholder={current?.has_api_key ? "Leave blank to keep the stored key" : "Required for a new connection"} /></label></div>
      {probe && <p className="mt-4 rounded-lg bg-[#edf1ed] px-3 py-2 text-xs text-[#466151]">{probe}</p>}<div className="mt-5 flex flex-wrap justify-end gap-2"><button onClick={() => void testConnection()} disabled={busy} className="rounded-lg border border-[#cfd5cf] bg-white px-3 py-2 text-xs disabled:opacity-40">Test connection</button><button onClick={() => void save()} disabled={busy || !profileId.trim() || !baseUrl.trim() || !model.trim()} className="rounded-lg border border-[#7c9a89] bg-white px-3 py-2 text-xs font-semibold text-[#275d46] disabled:opacity-40">Save locally</button><button onClick={() => void activate()} disabled={busy || !selected || connections?.default === selected} className="rounded-lg bg-[#275d46] px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">Use for new tasks</button></div>
    </section></div></div>;
}

export function AgentWorkspace() {
  const [runs, setRuns] = useState<Run[]>([]), [run, setRun] = useState<Run | null>(null);
  const [template, setTemplate] = useState<File | null>(null), [sources, setSources] = useState<File[]>([]), [instruction, setInstruction] = useState("");
  const [batchSize, setBatchSize] = useState(50);
  const [connections, setConnections] = useState<ModelConnections | null>(null), [settingsOpen, setSettingsOpen] = useState(false);
  const [busy, setBusy] = useState(false), [notice, setNotice] = useState<string | null>(null), [tasksOpen, setTasksOpen] = useState(false), [detailsOpen, setDetailsOpen] = useState(false), [modelState, setModelState] = useState("Model not checked"), [backendState, setBackendState] = useState<"checking" | "connected" | "unavailable">("checking");
  function loadRuns() { setBackendState("checking"); void api<Run[]>("/api/runs").then((items) => { setRuns(items); setBackendState("connected"); setNotice(null); }).catch((error) => { setBackendState("unavailable"); setNotice(error instanceof Error ? `Backend unavailable: ${error.message}` : "Backend unavailable"); }); }
  useEffect(() => { let active = true; void Promise.all([api<Run[]>("/api/runs"), api<ModelConnections>("/api/model-connections")]).then(([items, configured]) => { if (active) { setRuns(items); setConnections(configured); setBackendState("connected"); } }).catch((error) => { if (active) { setBackendState("unavailable"); setNotice(error instanceof Error ? `Backend unavailable: ${error.message}` : "Backend unavailable"); } }); return () => { active = false; }; }, []);
  function updateRun(next: Run) { setRun(next); setRuns((current) => [next, ...current.filter((item) => item.id !== next.id)]); }
  function newTask() { setRun(null); setTemplate(null); setSources([]); setInstruction(""); setBatchSize(50); setNotice(null); setTasksOpen(false); }
  async function selectRun(id: string) { setNotice(null); try { updateRun(await api<Run>(`/api/runs/${id}`)); setTasksOpen(false); } catch (e) { setNotice(e instanceof Error ? e.message : "Could not open the task"); } }
  async function beginTask() { if (!template || !sources.length) return; setBusy(true); setNotice(null); try { const created = await api<Run>("/api/runs", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ model_profile_id: connections?.default ?? null }) }); await api<Run>(`/api/runs/${created.id}/batch-settings`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ batch_size: batchSize }) }); const form = new FormData(); form.append("template", template); sources.forEach((file) => form.append("sources", file)); await api<Run>(`/api/runs/${created.id}/files`, { method: "POST", body: form }); const message: UIMessage = { id: `user-${Date.now()}`, role: "user", metadata: { createdAt: new Date().toISOString() }, parts: [{ type: "text", text: instruction.trim() || `Merge ${sources.length} source workbook${sources.length === 1 ? "" : "s"} into ${template.name}.` }] }; updateRun(await api<Run>(`/api/runs/${created.id}/conversation`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ messages: [message] }) })); updateRun(await api<Run>(`/api/runs/${created.id}/inspect`, { method: "POST" })); } catch (e) { setNotice(e instanceof Error ? e.message : "Could not start the merge task"); } finally { setBusy(false); } }
  async function probeModel() { setModelState("Checking model…"); try { const result = await api<{ connected: boolean }>("/api/model/probe", { method: "POST" }); setModelState(result.connected ? "Model connected" : "Model unavailable"); } catch { setModelState("Model check failed"); } }
  const activeProfile = connections?.profiles.find((item) => item.id === connections.default);
  const sidebar = <aside className="flex h-full w-64 shrink-0 flex-col border-r border-[#deded9] bg-[#efefeb] p-3"><div className="flex items-center justify-between px-2 py-2"><div className="flex items-center gap-2 text-sm font-semibold"><span className="grid size-8 place-items-center rounded-lg bg-[#275d46] text-white"><FileSpreadsheet size={17} /></span>Excel Merge Agent</div><button onClick={() => setTasksOpen(false)} className="lg:hidden" aria-label="Close"><X size={17} /></button></div><button onClick={newTask} disabled={backendState !== "connected"} className="mt-4 flex items-center gap-2 rounded-lg border border-[#d8d8d2] bg-white px-3 py-2.5 text-sm font-medium shadow-sm disabled:opacity-45"><MessageSquarePlus size={16} />New merge task</button><p className="mt-7 px-2 text-[11px] font-semibold uppercase tracking-[.12em] text-[#8a8b86]">Recent tasks</p><div className="mt-2 min-h-0 flex-1 space-y-1 overflow-y-auto">{runs.map((item) => <button key={item.id} onClick={() => void selectRun(item.id)} className={`w-full rounded-lg px-3 py-2.5 text-left ${run?.id === item.id ? "bg-[#dfe2db]" : "hover:bg-[#e6e7e2]"}`}><p className="truncate text-xs font-medium">{item.template?.filename ?? "New workbook merge"}</p><div className="mt-1 flex justify-between"><p className="text-[10px] capitalize text-[#7d8079]">{stateLabel(item.state)}</p><time className="text-[9px] text-[#999b95]">{formatTime(item.updated_at)}</time></div></button>)}{runs.length === 0 && backendState === "connected" && <p className="px-2 py-3 text-xs text-[#8a8c85]">No merge tasks yet.</p>}{backendState === "checking" && <p className="flex items-center gap-2 px-2 py-3 text-xs text-[#8a8c85]"><Loader2 size={12} className="animate-spin" />Connecting…</p>}{backendState === "unavailable" && <button onClick={loadRuns} className="mx-2 mt-2 flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800"><AlertCircle size={13} />Backend unavailable · retry</button>}</div><div className="border-t border-[#d8d8d2] pt-3"><p className="flex items-center gap-2 px-2 text-xs text-[#6f716b]"><span className={`size-2 rounded-full ${backendState === "connected" ? "bg-[#2f9365]" : backendState === "checking" ? "bg-amber-400" : "bg-rose-500"}`} />Backend {backendState}</p><button onClick={() => void probeModel()} disabled={backendState !== "connected"} className="mt-2 flex items-center gap-2 px-2 text-xs text-[#6f716b] disabled:opacity-45"><span className={`size-2 rounded-full ${modelState.includes("connected") ? "bg-[#2f9365]" : modelState.includes("failed") || modelState.includes("unavailable") ? "bg-rose-500" : "bg-[#a7aaa3]"}`} />{modelState}</button><button onClick={() => setSettingsOpen(true)} disabled={backendState !== "connected"} className="mt-2 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-[#5f625c] hover:bg-[#e4e5df] disabled:opacity-45"><Settings size={13} /><span className="min-w-0 flex-1"><span className="block truncate">{activeProfile?.model ?? "Model settings"}</span>{activeProfile && <span className="block truncate text-[9px] text-[#92948e]">{activeProfile.provider} · {activeProfile.id}</span>}</span></button></div></aside>;
  return <main className="flex h-screen overflow-hidden bg-[#f7f7f5] text-[#20211f]"><div className="hidden lg:block">{sidebar}</div>{tasksOpen && <div className="fixed inset-0 z-40 bg-black/25 lg:hidden" onClick={() => setTasksOpen(false)}><div className="h-full w-64" onClick={(e) => e.stopPropagation()}>{sidebar}</div></div>}<section className="flex min-w-0 flex-1 flex-col"><header className="flex h-14 shrink-0 items-center justify-between border-b border-[#e3e3df] bg-[#fbfbf9]/95 px-4 md:px-6"><div className="flex min-w-0 items-center gap-3"><button onClick={() => setTasksOpen(true)} className="grid size-8 place-items-center lg:hidden" aria-label="Open tasks"><Menu size={18} /></button><div className="min-w-0"><p className="truncate text-sm font-semibold">{run?.template?.filename ?? "New workbook merge"}</p><div className="mt-0.5 flex items-center gap-2"><span className={`rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase ${run ? stateTone(run.state) : "bg-[#ecece8] text-[#7c7e78]"}`}>{run ? stateLabel(run.state) : "ready for files"}</span>{run && <span className="text-[10px] text-[#92948e]">{run.sources.length} source{run.sources.length === 1 ? "" : "s"}</span>}</div></div></div><button onClick={() => setDetailsOpen(true)} className="grid size-8 place-items-center rounded-lg border border-[#deded9] bg-white xl:hidden" aria-label="Open details"><PanelRight size={16} /></button></header>{notice && <div className="flex shrink-0 items-center justify-between border-b border-rose-200 bg-rose-50 px-4 py-2 text-xs text-rose-800"><span className="flex items-center gap-2"><AlertCircle size={14} />{notice}</span><button onClick={() => setNotice(null)}><X size={14} /></button></div>}<div className="flex min-h-0 flex-1">{run ? <RunConversation key={run.id} run={run} busy={busy} onRunChange={updateRun} onError={setNotice} /> : <EmptyConversation template={template} sources={sources} instruction={instruction} batchSize={batchSize} busy={busy} error={null} onTemplate={setTemplate} onSources={setSources} onInstruction={setInstruction} onBatchSize={setBatchSize} onSubmit={() => void beginTask()} />}<aside className="hidden w-72 shrink-0 overflow-y-auto border-l border-[#e3e3df] bg-[#fbfbf9] p-5 xl:block"><p className="mb-5 text-xs font-semibold uppercase tracking-[.1em] text-[#858780]">Task details</p><TaskDetails run={run} /></aside></div></section>{detailsOpen && <div className="fixed inset-0 z-40 bg-black/25 xl:hidden" onClick={() => setDetailsOpen(false)}><aside className="ml-auto h-full w-[min(88vw,340px)] overflow-y-auto bg-[#fbfbf9] p-5 shadow-xl" onClick={(e) => e.stopPropagation()}><div className="mb-5 flex justify-between"><p className="text-xs font-semibold uppercase tracking-[.1em] text-[#858780]">Task details</p><button onClick={() => setDetailsOpen(false)}><X size={17} /></button></div><TaskDetails run={run} /></aside></div>}{settingsOpen && <ModelConnectionsDialog connections={connections} onClose={() => setSettingsOpen(false)} onChanged={setConnections} onError={(message) => setNotice(message)} />}</main>;
}
