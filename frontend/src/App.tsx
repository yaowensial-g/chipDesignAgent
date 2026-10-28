import {
  AlertTriangle,
  Activity,
  Bot,
  CheckCircle2,
  ChevronRight,
  CircuitBoard,
  Clock3,
  FileCode2,
  FileText,
  LoaderCircle,
  MessageSquareText,
  Paperclip,
  Plus,
  Send,
  Settings,
  ShieldCheck,
  Trash2,
  UploadCloud,
  UserRound,
  Wrench,
  X,
} from "lucide-react";
import { ChangeEvent, DragEvent, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { extractFile, getModelSettings, getRun, resumeRun, resumeTask, runDiagnostics, sendChat } from "./api";
import { DiagnosticsModal } from "./DiagnosticsModal";
import { ModelSettingsModal } from "./ModelSettingsModal";
import { EngineeringModal } from "./EngineeringModal";
import type { CandidatePatch, ChatMessage, DiagnosticReport, DocumentKind, ExtractedDocument, ModelSettings, RunView, TaskView, WorkflowRoute } from "./types";

type InspectorTab = "ds" | "findings" | "activity";

const workflowLabels: Record<WorkflowRoute, string> = {
  chat: "普通聊天",
  ds_workflow: "DS 任务",
  rtl_workflow: "RTL 任务",
  lint_workflow: "Lint 任务",
  simulation_workflow: "仿真任务",
  cdc_workflow: "CDC 任务",
  synthesis_workflow: "综合任务",
  sta_workflow: "STA 任务",
  test_execution_workflow: "用例任务",
};

const defaultModelSettings: ModelSettings = {
  enabled: false,
  provider: "deepseek",
  api_style: "responses",
  model: "deepseek-flash",
  base_url: "https://api.deepseek.com",
  timeout_seconds: 120,
  max_output_tokens: 6000,
  api_key_source: "none",
  has_api_key: false,
  api_key_masked: null,
  ready: false,
};

const initialMessage: ChatMessage = {
  id: "welcome",
  role: "assistant",
  content:
    "你好，我是 ChipFlow。你可以直接讨论芯片架构、RTL 和验证，也可以明确要求完成 DS、生成 RTL、执行 Lint 或已有用例；编排 Agent 会创建统一 Run，并在需要时请求资料或审批。",
  time: new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }),
};

function App() {
  const [projectId, setProjectId] = useState("demo-project");
  const [moduleName, setModuleName] = useState("dma_ctrl");
  const [documents, setDocuments] = useState<ExtractedDocument[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([initialMessage]);
  const [draft, setDraft] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [task, setTask] = useState<TaskView | null>(null);
  const [run, setRun] = useState<RunView | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("ds");
  const [resolution, setResolution] = useState("");
  const [evidence, setEvidence] = useState("");
  const [modelSettings, setModelSettings] = useState<ModelSettings>(defaultModelSettings);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [diagnosticsLoading, setDiagnosticsLoading] = useState(false);
  const [diagnosticsReport, setDiagnosticsReport] = useState<DiagnosticReport | null>(null);
  const [diagnosticsError, setDiagnosticsError] = useState<string | null>(null);
  const [engineeringOpen, setEngineeringOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    getModelSettings().then(setModelSettings).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!run || !["planned", "running"].includes(run.status)) return;
    const timer = window.setInterval(() => {
      getRun(run.run_id).then(setRun).catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [run?.run_id, run?.status]);

  async function openModelSettings() {
    setError(null);
    try {
      setModelSettings(await getModelSettings());
    } catch {
      setError("暂时无法读取后端模型配置，已打开默认配置；请确认后端服务已经启动。");
    } finally {
      setSettingsOpen(true);
    }
  }

  async function openDiagnostics() {
    setDiagnosticsOpen(true);
    setDiagnosticsLoading(true);
    setDiagnosticsError(null);
    try {
      setDiagnosticsReport(await runDiagnostics());
    } catch (diagnosticError) {
      setDiagnosticsReport(null);
      setDiagnosticsError(diagnosticError instanceof Error ? diagnosticError.message : "系统自检失败");
    } finally {
      setDiagnosticsLoading(false);
    }
  }

  const readyToSend = draft.trim().length > 0 && !busy;
  const statusLabel = useMemo(() => {
    if (!task && !run) return "等待任务";
    const labels: Record<string, string> = {
      waiting_for_user: "等待确认",
      waiting_for_approval: "等待审批",
      delivered: "已交付",
      completed: "已完成",
      cancelled: "已取消",
      reviewing: "评审中",
      executing: "执行中",
      running: "执行中",
      failed: "失败",
    };
    const status = run?.status ?? task?.status ?? "等待任务";
    return labels[status] ?? status;
  }, [task, run]);

  async function handleFiles(files: FileList | File[]) {
    const candidates = Array.from(files);
    if (!candidates.length) return;
    setUploading(true);
    setError(null);
    try {
      const extracted = await Promise.all(
        candidates.map(async (file) => {
          const result = await extractFile(file);
          return {
            ...result,
            id: crypto.randomUUID(),
            kind: result.suggested_kind ?? "AS",
          } satisfies ExtractedDocument;
        }),
      );
      setDocuments((current) => [...current, ...extracted]);
      addAssistant(`已读取 ${extracted.length} 份文档。请检查左侧文档类型和版本，然后发送任务。`);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : "文件上传失败");
    } finally {
      setUploading(false);
    }
  }

  function onFileChange(event: ChangeEvent<HTMLInputElement>) {
    if (event.target.files) void handleFiles(event.target.files);
    event.target.value = "";
  }

  function onDrop(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    void handleFiles(event.dataTransfer.files);
  }

  function addAssistant(content: string, route?: ChatMessage["route"]) {
    setMessages((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        role: "assistant",
        content,
        route,
        time: new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }),
      },
    ]);
  }

  async function submitMessage(event: FormEvent) {
    event.preventDefault();
    if (!readyToSend) return;
    const content = draft.trim();
    setDraft("");
    setError(null);
    setMessages((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        role: "user",
        content,
        time: new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }),
      },
    ]);
    setBusy(true);
    try {
      const response = await sendChat({
        sessionId,
        projectId,
        moduleName,
        message: content,
        history: messages.slice(-20).map(({ role, content: messageContent }) => ({
          role,
          content: messageContent,
        })),
        documents: documents.map(({ kind, name, version, content: documentContent }) => ({
          kind,
          name,
          version,
          content: documentContent,
        })),
        workspaceId: run?.workspace_id,
        dsMarkdown: task?.ds_markdown,
      });
      setSessionId(response.session_id);
      if (response.task) setTask(response.task);
      if (response.run) setRun(response.run);
      addAssistant(response.assistant_message, response.route);
      if (response.task?.status === "waiting_for_user") setInspectorTab("activity");
      if (response.task?.status === "delivered") setInspectorTab("ds");
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "请求失败";
      setError(message);
      addAssistant(`任务未能完成：${message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleResolution(action: "provide_resolution" | "accept_assumption" | "ignore" | "cancel") {
    if (!task) return;
    if (action === "provide_resolution" && !resolution.trim()) {
      setError("请填写真实架构结论后再继续。" );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const updatedRun = run?.legacy_task_id === task.task_id
        ? await resumeRun(run.run_id, action, resolution.trim(), evidence.trim())
        : null;
      const updated = updatedRun?.result?.task
        ? updatedRun.result.task as unknown as TaskView
        : await resumeTask(task.task_id, action, resolution.trim(), evidence.trim());
      if (updatedRun) setRun(updatedRun);
      setTask(updated);
      const userText = action === "cancel" ? "取消当前任务" : resolution || "允许按临时假设继续";
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "user",
          content: userText,
          time: new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }),
        },
      ]);
      addAssistant(
        updated.status === "delivered"
          ? "已接收你的处理意见，工作流已经恢复并完成 DS 交付。"
          : "当前任务已取消。",
      );
      setResolution("");
      setEvidence("");
      setInspectorTab(updated.status === "delivered" ? "ds" : "activity");
    } catch (resumeError) {
      setError(resumeError instanceof Error ? resumeError.message : "恢复任务失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleRunDecision(action: "approve" | "reject" | "custom") {
    if (!run) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await resumeRun(run.run_id, action, resolution.trim(), evidence.trim());
      setRun(updated);
      addAssistant(
        action === "approve"
          ? "候选修改已经批准并应用。"
          : action === "reject"
            ? "候选修改已拒绝，正式文件未改变。"
            : "已记录自定义修改要求，请继续在聊天框中描述新的修改方案。",
      );
      setResolution("");
    } catch (decisionError) {
      setError(decisionError instanceof Error ? decisionError.message : "Run 决策失败");
    } finally {
      setBusy(false);
    }
  }

  function updateDocument(id: string, patch: Partial<ExtractedDocument>) {
    setDocuments((current) => current.map((document) => (document.id === id ? { ...document, ...patch } : document)));
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark"><CircuitBoard size={22} /></div>
          <div>
            <strong>ChipFlow Agent</strong>
            <span>芯片设计协作工作台</span>
          </div>
        </div>
        <div className="topbar-actions">
          <span className={`runtime-badge ${modelSettings?.ready ? "runtime-live" : ""}`}>
            <span className="pulse-dot" />
            {modelSettings?.ready ? `真实模型 · ${modelSettings.model}` : "模拟运行时"}
          </span>
          <span className={`status-badge status-${run?.status ?? task?.status ?? "idle"}`}>{statusLabel}</span>
          <button className="settings-button" type="button" onClick={() => setEngineeringOpen(true)}>
            <Wrench size={16} /> 工程工作台
          </button>
          <button className="settings-button" type="button" onClick={() => void openDiagnostics()}>
            <Activity size={16} /> 系统自检
          </button>
          <button className="settings-button" type="button" onClick={() => void openModelSettings()}>
            <Settings size={16} /> 模型设置
          </button>
        </div>
      </header>

      <aside className="source-panel">
        <section className="panel-section project-section">
          <p className="eyebrow">当前工作区</p>
          <label>
            项目
            <input value={projectId} onChange={(event) => setProjectId(event.target.value)} />
          </label>
          <label>
            目标模块
            <input value={moduleName} onChange={(event) => setModuleName(event.target.value)} />
          </label>
        </section>

        <section className="panel-section document-section">
          <div className="section-heading">
            <div>
              <p className="eyebrow">设计基线</p>
              <h2>输入文档</h2>
            </div>
            <span className="count-pill">{documents.length}</span>
          </div>

          <button
            className={`upload-zone ${uploading ? "is-uploading" : ""}`}
            type="button"
            onClick={() => fileInputRef.current?.click()}
            onDragOver={(event) => event.preventDefault()}
            onDrop={onDrop}
          >
            {uploading ? <LoaderCircle className="spin" size={24} /> : <UploadCloud size={24} />}
            <strong>{uploading ? "正在解析文档" : "上传 FS / AS / ICD"}</strong>
            <span>支持 PDF、DOCX、MD、TXT，单文件不超过 10 MB</span>
          </button>
          <input
            ref={fileInputRef}
            className="visually-hidden"
            type="file"
            multiple
            accept=".pdf,.docx,.md,.txt"
            onChange={onFileChange}
          />

          <div className="document-list">
            {documents.map((document) => (
              <article className="document-card" key={document.id}>
                <div className={`document-icon kind-${document.kind.toLowerCase()}`}>
                  <FileText size={17} />
                </div>
                <div className="document-meta">
                  <strong title={document.name}>{document.name}</strong>
                  <div className="document-controls">
                    <select
                      aria-label={`${document.name} 文档类型`}
                      value={document.kind}
                      onChange={(event) => updateDocument(document.id, { kind: event.target.value as DocumentKind })}
                    >
                      <option value="FS">FS</option>
                      <option value="AS">AS</option>
                      <option value="ICD">ICD</option>
                    </select>
                    <input
                      aria-label={`${document.name} 版本`}
                      value={document.version}
                      onChange={(event) => updateDocument(document.id, { version: event.target.value })}
                    />
                  </div>
                </div>
                <button
                  className="icon-button subtle"
                  type="button"
                  aria-label={`移除 ${document.name}`}
                  onClick={() => setDocuments((current) => current.filter((item) => item.id !== document.id))}
                >
                  <Trash2 size={15} />
                </button>
              </article>
            ))}
            {!documents.length && (
              <div className="empty-documents">
                <FileCode2 size={18} />
                <span>还没有添加规格文档</span>
              </div>
            )}
          </div>
        </section>

        <footer className="source-footer">
          <ShieldCheck size={16} />
          <span>原始文档只读，Agent 仅生成候选结果</span>
        </footer>
      </aside>

      <main className="conversation-panel">
        <div className="conversation-heading">
          <div>
            <p className="eyebrow">任务对话</p>
            <h1>{moduleName || "未命名模块"}</h1>
          </div>
          {run
            ? <span className="task-id">RUN · {run.run_id.slice(0, 8)} · {run.run_type}</span>
            : task && <span className="task-id">TASK · {task.task_id.slice(0, 8)}</span>}
        </div>

        <div className="message-stream">
          {messages.map((message) => (
            <article className={`message ${message.role}`} key={message.id}>
              <div className="message-avatar">
                {message.role === "assistant" ? <Bot size={17} /> : <UserRound size={17} />}
              </div>
              <div className="message-body">
                <div className="message-meta">
                  <strong>{message.role === "assistant" ? "ChipFlow" : "你"}</strong>
                  {message.route && (
                    <span className={`message-route route-${message.route}`}>
                      {workflowLabels[message.route]}
                    </span>
                  )}
                  <time>{message.time}</time>
                </div>
                <p>{message.content}</p>
              </div>
            </article>
          ))}
          {busy && (
            <article className="message assistant">
              <div className="message-avatar"><Bot size={17} /></div>
              <div className="message-body thinking">
                <LoaderCircle className="spin" size={16} />
                <span>编排 Agent 正在判断任务并处理请求…</span>
              </div>
            </article>
          )}

          {task?.status === "waiting_for_user" && (
            <section className="decision-card">
              <div className="decision-title">
                <AlertTriangle size={19} />
                <div>
                  <strong>需要真实架构结论</strong>
                  <span>内部讨论已达到上限，系统不会自行修改规格。</span>
                </div>
              </div>
              <div className="issue-list">
                {task.issues.map((issue) => (
                  <div className="issue-row" key={issue.issue_id}>
                    <span>{issue.issue_id}</span>
                    <p>{issue.title}</p>
                  </div>
                ))}
              </div>
              <textarea
                value={resolution}
                onChange={(event) => setResolution(event.target.value)}
                placeholder="填写真实架构师给出的结论…"
              />
              <input
                value={evidence}
                onChange={(event) => setEvidence(event.target.value)}
                placeholder="问题单编号，例如 ISSUE-123（可选）"
              />
              <div className="decision-actions">
                <button type="button" className="primary-button" disabled={busy} onClick={() => void handleResolution("provide_resolution")}>
                  提交正式结论
                </button>
                <button type="button" className="secondary-button" disabled={busy} onClick={() => void handleResolution("accept_assumption")}>
                  按临时假设继续
                </button>
                <button type="button" className="text-button danger" disabled={busy} onClick={() => void handleResolution("cancel")}>
                  取消任务
                </button>
              </div>
            </section>
          )}

          {run?.run_type === "rtl_implementation" && run.status === "waiting_for_approval" && (
            <section className="decision-card">
              <div className="decision-title">
                <ShieldCheck size={19} />
                <div>
                  <strong>RTL Candidate 等待审批</strong>
                  <span>审批前不会写入正式 Workspace 文件。</span>
                </div>
              </div>
              {Boolean((run.result.patch as CandidatePatch | undefined)?.diff) && (
                <pre className="tool-log">{(run.result.patch as CandidatePatch).diff}</pre>
              )}
              <textarea
                value={resolution}
                onChange={(event) => setResolution(event.target.value)}
                placeholder="拒绝原因或自定义修改要求（可选）"
              />
              <div className="decision-actions">
                <button type="button" className="primary-button" disabled={busy} onClick={() => void handleRunDecision("approve")}>确认并应用</button>
                <button type="button" className="secondary-button" disabled={busy} onClick={() => void handleRunDecision("reject")}>不修改</button>
                <button type="button" className="secondary-button" disabled={busy} onClick={() => void handleRunDecision("custom")}>自定义修改</button>
              </div>
            </section>
          )}
        </div>

        {error && (
          <div className="error-banner">
            <AlertTriangle size={17} />
            <span>{error}</span>
            <button className="diagnostic-link" type="button" onClick={() => void openDiagnostics()}>运行自检</button>
            <button className="dismiss-error" type="button" aria-label="关闭错误" onClick={() => setError(null)}><X size={15} /></button>
          </div>
        )}

        <form className="composer" onSubmit={submitMessage}>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            placeholder="例如：请根据上传的资料完成 dma_ctrl 模块 DS，并重点检查接口歧义"
          />
          <div className="composer-footer">
            <button className="attach-button" type="button" onClick={() => fileInputRef.current?.click()}>
              <Paperclip size={16} /> 添加文档
            </button>
            <span>Enter 发送 · Shift + Enter 换行</span>
            <button className="send-button" type="submit" disabled={!readyToSend} aria-label="发送">
              <Send size={17} />
            </button>
          </div>
        </form>
      </main>

      <aside className="inspector-panel">
        <div className="inspector-heading">
          <div>
            <p className="eyebrow">交付物</p>
            <h2>任务结果</h2>
          </div>
          {task?.status === "delivered" && <CheckCircle2 className="success-icon" size={21} />}
        </div>

        <nav className="inspector-tabs" aria-label="结果分类">
          <button className={inspectorTab === "ds" ? "active" : ""} onClick={() => setInspectorTab("ds")}>
            DS
          </button>
          <button className={inspectorTab === "findings" ? "active" : ""} onClick={() => setInspectorTab("findings")}>
            评审 {task ? <span>{task.findings.length}</span> : null}
          </button>
          <button className={inspectorTab === "activity" ? "active" : ""} onClick={() => setInspectorTab("activity")}>
            过程
          </button>
        </nav>

        <div className="inspector-content">
          {!task && !run && (
            <div className="empty-state">
              <MessageSquareText size={28} />
              <strong>尚无任务结果</strong>
              <p>上传文档并在对话框中发起任务后，DS 和评审意见会显示在这里。</p>
            </div>
          )}

          {run && (
            <section className="run-summary-card">
              <strong>{run.run_type} · {statusLabel}</strong>
              <p>{run.pending_user_request?.message as string || run.route_reason}</p>
              <code>{run.run_id}</code>
            </section>
          )}

          {task && inspectorTab === "ds" && (
            task.ds_markdown ? (
              <article className="markdown-document"><ReactMarkdown>{task.ds_markdown}</ReactMarkdown></article>
            ) : (
              <div className="empty-state compact"><Clock3 size={25} /><p>DS 尚未生成。</p></div>
            )
          )}

          {task && inspectorTab === "findings" && (
            <div className="finding-list">
              {task.findings.map((finding) => (
                <article className="finding-card" key={finding.finding_id}>
                  <div className="finding-topline">
                    <span className={`severity severity-${finding.severity}`}>{finding.severity}</span>
                    <code>{finding.finding_id}</code>
                  </div>
                  <strong>{finding.title}</strong>
                  <p>{finding.description}</p>
                  <footer>{finding.reviewer}</footer>
                </article>
              ))}
              {!task.findings.length && <div className="empty-state compact"><p>暂无评审意见。</p></div>}
            </div>
          )}

          {task && inspectorTab === "activity" && (
            <div className="timeline">
              {task.quality_warnings.map((warning) => (
                <div className="timeline-item warning" key={warning}>
                  <AlertTriangle size={14} /><p>{warning}</p>
                </div>
              ))}
              {task.events.map((event, index) => (
                <div className="timeline-item" key={`${event.event_type}-${index}`}>
                  <span className="timeline-dot" />
                  <div>
                    <strong>{event.agent_id || event.event_type}</strong>
                    <p>{event.message}</p>
                  </div>
                  <ChevronRight size={14} />
                </div>
              ))}
            </div>
          )}
        </div>
      </aside>
      {settingsOpen && (
        <ModelSettingsModal
          settings={modelSettings}
          onClose={() => setSettingsOpen(false)}
          onSaved={setModelSettings}
        />
      )}
      {diagnosticsOpen && (
        <DiagnosticsModal
          report={diagnosticsReport}
          loading={diagnosticsLoading}
          error={diagnosticsError}
          onRetry={() => void openDiagnostics()}
          onClose={() => setDiagnosticsOpen(false)}
        />
      )}
      {engineeringOpen && (
        <EngineeringModal
          projectId={projectId}
          moduleName={moduleName}
          dsMarkdown={task?.ds_markdown ?? null}
          documents={documents.map(({ kind, name, version, content }) => ({ kind, name, version, content }))}
          onClose={() => setEngineeringOpen(false)}
        />
      )}
    </div>
  );
}

export default App;

