import { useEffect, useState } from "react";
import { Activity, Code2, LoaderCircle, Play, Wrench, X } from "lucide-react";
import {
  createRTLCandidate,
  createWorkspace,
  decidePatch,
  listTools,
  listWorkspaces,
} from "./api";
import type {
  CandidatePatch,
  DocumentKind,
  ToolDefinition,
  Workspace,
} from "./types";

interface Props {
  projectId: string;
  moduleName: string;
  dsMarkdown: string | null;
  documents: Array<{ kind: DocumentKind; name: string; version: string; content: string }>;
  onClose: () => void;
}

export function EngineeringModal({ projectId, moduleName, dsMarkdown, documents, onClose }: Props) {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [patch, setPatch] = useState<CandidatePatch | null>(null);
  const [targetPath, setTargetPath] = useState(`rtl/${moduleName}.sv`);
  const [lintFiles, setLintFiles] = useState(`rtl/${moduleName}.sv`);
  const [customDecision, setCustomDecision] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([listWorkspaces(projectId), listTools()])
      .then(([items, definitions]) => {
        setWorkspace(items[0] ?? null);
        setTools(definitions.filter((tool) => tool.enabled));
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

  function runLint() {
    // EDA 占位函数由聊天编排 Agent 通过 Function Call 调用。这个兼容工作台不再绕过
    // 主 Agent 直接启动本地进程，避免开发机器被要求安装 Verilator。
    setError(`请在主聊天窗口输入“执行 ${moduleName} 的 lint”。系统会调用 run_mock_eda，并返回明确的 MOCK 字符串。`);
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
            <div className="engineering-card-title"><Activity size={18} /><div><strong>工程 Tool · Compile / Sim / Lint / CDC / Synthesis / STA</strong><p>当前统一使用 run_mock_eda 验证 Function Call，不启动任何外部进程。</p></div></div>
            <label>Lint 文件（逗号分隔）<input value={lintFiles} onChange={(event) => setLintFiles(event.target.value)} /></label>
            <button className="primary-button" type="button" disabled={busy} onClick={runLint}><Play size={15} /> 查看 Mock Lint 调用方式</button>
            <div className="tool-chips">{tools.map((tool) => <span key={tool.name}>{tool.name} · {tool.mutability}</span>)}</div>
          </section>
          {workspace && <small className="workspace-path">Workspace：{workspace.root_path}</small>}
        </div>
      </section>
    </div>
  );
}
