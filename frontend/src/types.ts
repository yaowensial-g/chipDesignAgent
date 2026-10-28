export type DocumentKind = "FS" | "AS" | "ICD";

export interface ExtractedDocument {
  id: string;
  name: string;
  kind: DocumentKind;
  suggested_kind: DocumentKind | null;
  version: string;
  content: string;
  size_bytes: number;
}

export interface Issue {
  issue_id: string;
  title: string;
  description: string;
  status: string;
  discussion_round: number;
}

export interface Finding {
  finding_id: string;
  reviewer: string;
  title: string;
  description: string;
  severity: string;
  status: string;
}

export interface TaskEvent {
  event_type: string;
  message: string;
  agent_id: string | null;
}

export interface TaskView {
  task_id: string;
  project_id: string;
  module_name: string;
  status: string;
  issues: Issue[];
  findings: Finding[];
  quality_warnings: string[];
  events: TaskEvent[];
  pending_user_request: Record<string, unknown> | null;
  ds_markdown: string | null;
}

export interface ChatResponse {
  session_id: string;
  assistant_message: string;
  route: WorkflowRoute;
  route_reason: string | null;
  task: TaskView | null;
  run: RunView | null;
}

export type WorkflowRoute = "orchestrated";

export interface RunEvent {
  event_id: string;
  run_id: string;
  sequence: number;
  event_type: string;
  message: string;
  agent_id: string | null;
  created_at: string;
}

export interface RunView {
  run_id: string;
  session_id: string;
  project_id: string;
  module_name: string;
  workspace_id: string | null;
  run_type: string;
  status: string;
  user_instruction: string;
  route_reason: string;
  legacy_task_id: string | null;
  tool_run_id: string | null;
  patch_id: string | null;
  pending_user_request: Record<string, unknown> | null;
  result: Record<string, unknown>;
  events: RunEvent[];
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: string;
  role: "assistant" | "user";
  content: string;
  time: string;
  route?: WorkflowRoute;
}

export interface ModelSettings {
  enabled: boolean;
  provider: string;
  api_style: "responses" | "chat_completions";
  model: string;
  base_url: string;
  timeout_seconds: number;
  max_output_tokens: number;
  api_key_source: string;
  has_api_key: boolean;
  api_key_masked: string | null;
  ready: boolean;
}

export interface ModelSettingsInput {
  enabled: boolean;
  provider: "openai" | "deepseek";
  api_style: "responses" | "chat_completions";
  model: string;
  base_url: string;
  api_key?: string;
  clear_api_key?: boolean;
  timeout_seconds: number;
  max_output_tokens: number;
}

export interface DiagnosticCheck {
  check_id: string;
  label: string;
  status: "pass" | "warning" | "error";
  detail: string;
  action: string | null;
}

export interface DiagnosticReport {
  status: "ok" | "warning" | "error";
  generated_at: string;
  version: string;
  checks: DiagnosticCheck[];
}

export interface Workspace {
  workspace_id: string;
  project_id: string;
  display_name: string;
  root_path: string;
  created_at: string;
}

export interface ToolDefinition {
  name: string;
  description: string;
  allowed_roles: string[];
  mutability: string;
  requires_user_approval: boolean;
  idempotent: boolean;
  timeout_seconds: number;
}

export interface ToolRun {
  run_id: string;
  workspace_id: string;
  tool_name: string;
  status: string;
  arguments: Record<string, unknown>;
  command: string[];
  exit_code: number | null;
  stdout: string;
  stderr: string;
  created_at: string;
  updated_at: string;
}

export interface EngineeringIssue {
  issue_id: string;
  run_id: string;
  queue_index: number;
  stage: string;
  tool: string;
  severity: string;
  code: string | null;
  message: string;
  file: string | null;
  line: number | null;
  fingerprint: string;
  status: string;
  user_decision: Record<string, unknown> | null;
  root_cause_analysis: string;
  evidence: string[];
  impact: string;
  suggestion: string;
  confidence: number;
  verification_scope: string[];
}

export interface CandidatePatch {
  patch_id: string;
  workspace_id: string;
  target_path: string;
  reason: string;
  sources: string[];
  diff: string;
  status: string;
  created_at: string;
  applied_at: string | null;
}

export interface RTLCandidate {
  workspace: Workspace;
  patch: CandidatePatch;
  candidate_content: string;
  agent_id: string;
}

