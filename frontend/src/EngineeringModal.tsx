import { useEffect, useState } from "react";
import { Activity, CheckCircle2, Code2, LoaderCircle, Play, Wrench, X } from "lucide-react";
import {
  createRTLCandidate,
  createWorkspace,
  decideIssue,
  decidePatch,
  getNextIssue,
  getToolRun,
  listTools,
  listWorkspaces,
  startToolRun,
} from "./api";
import type {
  CandidatePatch,
  DocumentKind,
  EngineeringIssue,
  ToolDefinition,
  ToolRun,
  Workspace,
} from "./types";

interface Props {
  projectId: string;
  moduleName: string;
  dsMarkdown: string | null;
  documents: Array<{ kind: DocumentKind; name: string; version: string; content: string }>;
  onClose: () => void;
}

const terminalStatuses = new Set(["completed", "failed", "cancelled", "timed_out"]);

export function EngineeringModal({ projectId, moduleName, dsMarkdown, documents, onClose }: Props) {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [patch, setPatch] = useState<CandidatePatch | null>(null);
  const [run, setRun] = useState<ToolRun | null>(null);
  const [issue, setIssue] = useState<EngineeringIssue | null>(null);
  const [targetPath, setTargetPath] = useState(`rtl/${moduleName}.sv`);
  const [lintFiles, setLintFiles] = useState(`rtl/${moduleName}.sv`);
  const [customDecision, setCustomDecision] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([listWorkspaces(projectId), listTools()])
      .then(([items, definitions]) => {
        setWorkspace(items[0] ?? null);
        setTools(definitions);
      })
      .catch((cause) => setError(cause instanceof Error ? cause.message : "无法读取工程配置"));
  }, [projectId]);

  useEffect(() => setTargetPath(`rtl/${moduleName}.sv`), [moduleName]);

  async function ensureWorkspace(): Promise<Workspace> {
    if (workspace) return workspace;
    const created = await createWorkspace(projectId);
    setWorkspace(created);
    return created;
  }

  async function generateRTL() {
    if (!dsMarkdown) {
      setError("请先在对话中完成 DS，再生成 RTL 候选。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const current = await ensureWorkspace();
      const result = await createRTLCandidate({
        projectId,
        workspaceId: current.workspace_id,
        moduleName,
        dsMarkdown,
        targetPath,
        documents,
      });
      setPatch(result.patch);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "RTL 候选生成失败");
    } finally {
      setBusy(false);
    }
  }

  async function reviewPatch(action: "approve" | "reject" | "custom") {
    if (!patch) return;
    setBusy(true);
    setError(null);
    try {
      setPatch(await decidePatch(patch.patch_id, action, customDecision));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Patch 处理失败");
    } finally {
      setBusy(false);
    }
  }

  async function runLint() {
    setBusy(true);
    setError(null);
    setIssue(null);
    try {
      const current = await ensureWorkspace();
      let currentRun = await startToolRun({
        workspaceId: current.workspace_id,
        toolName: "verilator_lint",
        role: "design",
        arguments: {
          files: lintFiles.split(",").map((value) => value.trim()).filter(Boolean),
          top_module: moduleName,
        },
        userApproved: true,
      });
      setRun(currentRun);
      while (!terminalStatuses.has(currentRun.status)) {
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        currentRun = await getToolRun(currentRun.run_id);
        setRun(currentRun);
      }
      setIssue(await getNextIssue(currentRun.run_id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Lint 执行失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleIssue(action: "approve" | "skip" | "custom" | "explain") {
    if (!issue || !run) return;
    setBusy(true);
    setError(null);
    try {
      await decideIssue(issue.issue_id, action, customDecision);
      setIssue(action === "explain" ? await getNextIssue(run.run_id) : await getNextIssue(run.run_id));
      if (action !== "explain") setCustomDecision("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Issue 处理失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="settings-modal engineering-modal" role="dialog" aria-modal="true" aria-labelledby="engineering-title">
        <header className="modal-header">
          <div><span className="eyebrow"><Wrench size={14} /> Phase 2</span><h2 id="engineering-title">工程工作台</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭工程工作台"><X size={19} /></button>
        </header>
        <div className="engineering-body">
          {error && <div className="error-banner">{error}</div>}
          <section className="engineering-card">
            <div className="engineering-card-title"><Code2 size={18} /><div><strong>Design Agent · RTL 候选</strong><p>只生成候选 Diff，审批前不会写入正式工作文件。</p></div></div>
            <label>目标文件<input value={targetPath} onChange={(event) => setTargetPath(event.target.value)} /></label>
            <button className="primary-button" type="button" disabled={busy || !dsMarkdown} onClick={generateRTL}>
              {busy ? <LoaderCircle className="spin" size={15} /> : <Code2 size={15} />} 生成 RTL 候选
            </button>
            {!dsMarkdown && <small>当前没有已交付 DS。先在左侧对话中要求系统完成 DS。</small>}
            {patch && <div className="patch-review">
              <div className="run-summary"><span>Patch {patch.status}</span><code>{patch.target_path}</code></div>
              <pre>{patch.diff || "文件内容无变化"}</pre>
              {patch.status === "patch_ready" && <div className="engineering-actions">
                <button className="primary-button" type="button" onClick={() => reviewPatch("approve")}>确认并应用</button>
                <button className="secondary-button" type="button" onClick={() => reviewPatch("reject")}>不修改</button>
                <button className="secondary-button" type="button" onClick={() => reviewPatch("custom")}>自定义修改</button>
              </div>}
            </div>}
          </section>

          <section className="engineering-card">
            <div className="engineering-card-title"><Activity size={18} /><div><strong>工程 Tool · Sim / Lint / CDC</strong><p>首版已接入真实 Verilator Lint；其他工具通过相同接口扩展。</p></div></div>
            <label>Lint 文件（逗号分隔）<input value={lintFiles} onChange={(event) => setLintFiles(event.target.value)} /></label>
            <button className="primary-button" type="button" disabled={busy} onClick={runLint}><Play size={15} /> 明确授权并执行 Lint</button>
            <div className="tool-chips">{tools.map((tool) => <span key={tool.name}>{tool.name} · {tool.mutability}</span>)}</div>
            {run && <div className="run-summary"><span>运行状态：{run.status}</span><code>exit {run.exit_code ?? "-"}</code></div>}
            {run && (run.stdout || run.stderr) && <pre className="tool-log">{[run.stdout, run.stderr].filter(Boolean).join("\n")}</pre>}
          </section>

          {run && <section className="engineering-card issue-card">
            <div className="engineering-card-title"><CheckCircle2 size={18} /><div><strong>逐条 Issue 决策</strong><p>系统一次只展示一条错误，当前决策后再进入下一条。</p></div></div>
            {issue ? <>
              <div className="issue-detail">
                <strong>{issue.code || issue.severity} · 置信度 {issue.confidence}/10</strong>
                <p>{issue.message}</p>
                <small>{issue.file ? `${issue.file}:${issue.line ?? ""}` : issue.stage}</small>
                <dl>
                  <dt>根因判断</dt><dd>{issue.root_cause_analysis}</dd>
                  <dt>影响</dt><dd>{issue.impact}</dd>
                  <dt>修改建议</dt><dd>{issue.suggestion}</dd>
                  <dt>建议复验</dt><dd>{issue.verification_scope.join("；")}</dd>
                </dl>
              </div>
              <textarea value={customDecision} onChange={(event) => setCustomDecision(event.target.value)} placeholder="自定义处理方式或补充说明" />
              <div className="engineering-actions">
                <button className="primary-button" type="button" onClick={() => handleIssue("approve")}>按建议处理</button>
                <button className="secondary-button" type="button" onClick={() => handleIssue("skip")}>不修改</button>
                <button className="secondary-button" type="button" onClick={() => handleIssue("custom")}>自定义</button>
                <button className="secondary-button" type="button" onClick={() => handleIssue("explain")}>解释</button>
              </div>
            </> : <p className="empty-note">当前没有待处理 Issue。</p>}
          </section>}
          {workspace && <small className="workspace-path">Workspace：{workspace.root_path}</small>}
        </div>
      </section>
    </div>
  );
}
