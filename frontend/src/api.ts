import type {
  ChatResponse,
  DiagnosticReport,
  DocumentKind,
  ExtractedDocument,
  ModelSettings,
  ModelSettingsInput,
  TaskView,
  Workspace,
  ToolDefinition,
  ToolRun,
  EngineeringIssue,
  CandidatePatch,
  RTLCandidate,
  RunView,
} from "./types";

const API_ROOT = "/api/v1";

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const structured = payload?.error;
    if (structured && typeof structured.message === "string") {
      const requestId = structured.request_id ? `请求编号：${structured.request_id}` : "";
      const suggestions = Array.isArray(structured.suggestions) ? structured.suggestions.join("；") : "";
      throw new Error(
        [structured.message, structured.code ? `错误类型：${structured.code}` : "", requestId, suggestions]
          .filter(Boolean)
          .join("｜"),
      );
    }
    const detail = typeof payload?.detail === "string" ? payload.detail : JSON.stringify(payload?.detail ?? "");
    const requestId = response.headers.get("X-Request-ID");
    if (response.status >= 500 && (!detail || detail === "Internal Server Error")) {
      throw new Error(`后端发生内部错误（HTTP ${response.status}）${requestId ? `，请求编号：${requestId}` : ""}。请运行系统自检。`);
    }
    throw new Error(detail || `请求失败（HTTP ${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export async function extractFile(file: File): Promise<Omit<ExtractedDocument, "id" | "kind">> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${API_ROOT}/documents/extract`, {
    method: "POST",
    body: form,
  });
  return parseResponse(response);
}

export interface ChatInput {
  sessionId: string | null;
  projectId: string;
  moduleName: string;
  message: string;
  history: Array<{ role: "user" | "assistant"; content: string }>;
  documents: Array<{
    kind: DocumentKind;
    name: string;
    version: string;
    content: string;
  }>;
  workspaceId?: string | null;
  dsMarkdown?: string | null;
  toolArguments?: Record<string, unknown>;
  userApproved?: boolean;
}

export async function sendChat(input: ChatInput): Promise<ChatResponse> {
  const path = input.sessionId
    ? `/conversations/${encodeURIComponent(input.sessionId)}/messages`
    : "/chat";
  const response = await fetch(`${API_ROOT}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: input.sessionId,
      project_id: input.projectId,
      module_name: input.moduleName,
      message: input.message,
      history: input.history,
      documents: input.documents,
      disabled_agents: [],
      custom_reviewers: [],
      max_discussion_rounds: 2,
      workspace_id: input.workspaceId || null,
      ds_markdown: input.dsMarkdown || null,
      tool_arguments: input.toolArguments || {},
      user_approved: input.userApproved || false,
    }),
  });
  return parseResponse(response);
}

export async function getRun(runId: string): Promise<RunView> {
  return parseResponse(await fetch(`${API_ROOT}/runs/${runId}`, { cache: "no-store" }));
}

export async function resumeRun(
  runId: string,
  action: "provide_resolution" | "accept_assumption" | "ignore" | "approve" | "reject" | "custom" | "cancel",
  content = "",
  evidenceReference?: string,
): Promise<RunView> {
  return parseResponse(await fetch(`${API_ROOT}/runs/${runId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action,
      content,
      evidence_reference: evidenceReference || null,
      tool_arguments: {},
    }),
  }));
}

export async function resumeTask(
  taskId: string,
  action: string,
  content: string,
  evidenceReference?: string,
): Promise<TaskView> {
  const response = await fetch(`${API_ROOT}/tasks/${taskId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action,
      content,
      evidence_reference: evidenceReference || null,
    }),
  });
  return parseResponse(response);
}

export async function getModelSettings(): Promise<ModelSettings> {
  return parseResponse(await fetch(`${API_ROOT}/settings/model`));
}

export async function saveModelSettings(input: ModelSettingsInput): Promise<ModelSettings> {
  const response = await fetch(`${API_ROOT}/settings/model`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return parseResponse(response);
}

export async function testModelConnection(): Promise<{ ok: boolean; message: string; model: string }> {
  const response = await fetch(`${API_ROOT}/settings/model/test`, { method: "POST" });
  return parseResponse(response);
}

export async function runDiagnostics(): Promise<DiagnosticReport> {
  return parseResponse(await fetch(`${API_ROOT}/diagnostics`, { cache: "no-store" }));
}

export async function listWorkspaces(projectId: string): Promise<Workspace[]> {
  return parseResponse(await fetch(`${API_ROOT}/workspaces?project_id=${encodeURIComponent(projectId)}`));
}

export async function createWorkspace(projectId: string): Promise<Workspace> {
  return parseResponse(await fetch(`${API_ROOT}/workspaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_id: projectId, display_name: `${projectId} 工程工作区` }),
  }));
}

export async function listTools(): Promise<ToolDefinition[]> {
  return parseResponse(await fetch(`${API_ROOT}/tools`));
}

export async function createRTLCandidate(input: {
  projectId: string;
  workspaceId?: string;
  moduleName: string;
  dsMarkdown: string;
  targetPath: string;
  documents: Array<{ kind: DocumentKind; name: string; version: string; content: string }>;
}): Promise<RTLCandidate> {
  return parseResponse(await fetch(`${API_ROOT}/rtl/candidates`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project_id: input.projectId,
      workspace_id: input.workspaceId || null,
      module_name: input.moduleName,
      target_path: input.targetPath,
      ds_markdown: input.dsMarkdown,
      documents: input.documents,
    }),
  }));
}

export async function decidePatch(patchId: string, action: "approve" | "reject" | "custom", comment = ""): Promise<CandidatePatch> {
  return parseResponse(await fetch(`${API_ROOT}/patches/${patchId}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, comment }),
  }));
}

export async function startToolRun(input: {
  workspaceId: string;
  toolName: string;
  role: "design" | "verification";
  arguments: Record<string, unknown>;
  userApproved: boolean;
}): Promise<ToolRun> {
  return parseResponse(await fetch(`${API_ROOT}/tool-runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workspace_id: input.workspaceId,
      tool_name: input.toolName,
      role: input.role,
      arguments: input.arguments,
      user_approved: input.userApproved,
    }),
  }));
}

export async function getToolRun(runId: string): Promise<ToolRun> {
  return parseResponse(await fetch(`${API_ROOT}/tool-runs/${runId}`, { cache: "no-store" }));
}

export async function getNextIssue(runId: string): Promise<EngineeringIssue | null> {
  return parseResponse(await fetch(`${API_ROOT}/tool-runs/${runId}/issues/next`, { cache: "no-store" }));
}

export async function decideIssue(issueId: string, action: "approve" | "skip" | "custom" | "explain", comment = ""): Promise<EngineeringIssue> {
  return parseResponse(await fetch(`${API_ROOT}/issues/${issueId}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, comment, patch_id: null }),
  }));
}

