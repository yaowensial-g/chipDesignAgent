import { AlertTriangle, CheckCircle2, LoaderCircle, RefreshCw, Stethoscope, X, XCircle } from "lucide-react";
import type { DiagnosticReport } from "./types";

interface Props {
  report: DiagnosticReport | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onClose: () => void;
}

const statusText = {
  ok: "全部通过",
  warning: "存在提醒",
  error: "发现错误",
};

export function DiagnosticsModal({ report, loading, error, onRetry, onClose }: Props) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="settings-modal diagnostics-modal" role="dialog" aria-modal="true" aria-labelledby="diagnostics-title">
        <header className="modal-header">
          <div>
            <span className="eyebrow"><Stethoscope size={14} /> 本机诊断</span>
            <h2 id="diagnostics-title">系统自检</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭系统自检"><X size={19} /></button>
        </header>

        <div className="diagnostics-body">
          {loading && (
            <div className="diagnostics-loading"><LoaderCircle className="spin" size={20} />正在检查后端、配置和模型运行时…</div>
          )}

          {!loading && error && (
            <div className="diagnostics-unreachable">
              <XCircle size={22} />
              <div><strong>自检接口无法访问</strong><p>{error}</p><small>请确认后端已重新启动，并且网页地址与启动脚本显示的端口一致。</small></div>
            </div>
          )}

          {!loading && report && (
            <>
              <div className={`diagnostics-summary diagnostic-${report.status}`}>
                <strong>{statusText[report.status]}</strong>
                <span>后端版本 {report.version} · 本次检查不会调用外部模型，也不会产生 API 费用</span>
              </div>
              <div className="diagnostics-list">
                {report.checks.map((check) => (
                  <article className={`diagnostic-item diagnostic-${check.status}`} key={check.check_id}>
                    {check.status === "pass" ? <CheckCircle2 size={18} /> : check.status === "warning" ? <AlertTriangle size={18} /> : <XCircle size={18} />}
                    <div>
                      <strong>{check.label}</strong>
                      <p>{check.detail}</p>
                      {check.action && <small>建议：{check.action}</small>}
                    </div>
                  </article>
                ))}
              </div>
            </>
          )}
        </div>

        <footer className="diagnostics-actions">
          <button className="secondary-button" type="button" onClick={onRetry} disabled={loading}><RefreshCw size={14} />重新检查</button>
          <button className="primary-button" type="button" onClick={onClose}>关闭</button>
        </footer>
      </section>
    </div>
  );
}
