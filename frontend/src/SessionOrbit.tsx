import { Activity, Bot, Boxes, CircuitBoard, Clock3, Command, Cpu, FolderKanban, GitBranch, Home, MessageSquareText, Plus, Radio, Search, ShieldCheck, UserRound } from "lucide-react";
import { PointerEvent, useEffect, useMemo, useRef, useState } from "react";

import type { ConversationView } from "./types";

interface SessionOrbitProps {
  conversations: ConversationView[];
  onOpenConversation: (conversation: ConversationView) => void;
  onCreateConversation: () => void;
}

/**
 * 会话星环首页。
 *
 * 组件只负责展示和选择会话，不创建第二份会话状态。点击节点后仍由 App 调用后端
 * 消息接口恢复历史。旋转使用 requestAnimationFrame 和阻尼实现；鼠标位置决定目标
 * 速度。当前景深最靠前的三张卡片是阅读区，悬停时会立即停止旋转；其余
 * 会话卡片仍属于旋转热区。中心入口、状态框和底部信息区也不触发旋转。
 */
export function SessionOrbit({
  conversations,
  onOpenConversation,
  onCreateConversation,
}: SessionOrbitProps) {
  const stageRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const targetVelocityRef = useRef(0);
  const velocityRef = useRef(0);
  const dragStartRef = useRef<number | null>(null);
  const dragRotationRef = useRef(0);
  // 默认错开半个节点间距，让前排会话左右对称分列，避免某张卡片压在中心轴线上。
  // 默认最多展示 32 个节点，因此半间距为 PI / 32。
  const [rotation, setRotation] = useState(Math.PI / 32);
  const [stageSize, setStageSize] = useState({ width: 1100, height: 650 });
  const [query, setQuery] = useState("");
  const [projectFilter, setProjectFilter] = useState("all");

  const projects = useMemo(
    () => Array.from(new Set(conversations.map((item) => item.project_id))).sort(),
    [conversations],
  );

  const visibleConversations = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return conversations
      .filter((item) => projectFilter === "all" || item.project_id === projectFilter)
      .filter((item) => {
        if (!normalized) return true;
        return [item.title, item.project_id, item.module_name, item.primary_agent]
          .some((value) => value.toLocaleLowerCase().includes(normalized));
      })
      .slice(0, 32);
  }, [conversations, projectFilter, query]);

  const activeCount = useMemo(
    () => conversations.filter((conversation) => conversation.active_run_id).length,
    [conversations],
  );
  const totalTokens = useMemo(
    () => conversations.reduce((total, conversation) => total + conversation.total_tokens, 0),
    [conversations],
  );

  useEffect(() => {
    if (!stageRef.current) return;
    const observer = new ResizeObserver(([entry]) => {
      setStageSize({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(stageRef.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === "k") {
        event.preventDefault();
        searchInputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", focusSearch);
    return () => window.removeEventListener("keydown", focusSearch);
  }, []);

  useEffect(() => {
    let frame = 0;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const animate = () => {
      const target = reduceMotion ? 0 : targetVelocityRef.current;
      // 更快地跟随鼠标目标速度，避免用户已经移到环上但画面迟迟没有响应。
      velocityRef.current += (target - velocityRef.current) * 0.16;
      if (Math.abs(velocityRef.current) > 0.00004) {
        setRotation((current) => current + velocityRef.current);
      }
      frame = window.requestAnimationFrame(animate);
    };
    frame = window.requestAnimationFrame(animate);
    return () => window.cancelAnimationFrame(frame);
  }, []);

  function updatePointerVelocity(event: PointerEvent<HTMLDivElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    event.currentTarget.style.setProperty("--cursor-x", `${event.clientX - rect.left}px`);
    event.currentTarget.style.setProperty("--cursor-y", `${event.clientY - rect.top}px`);
    if (dragStartRef.current !== null) {
      const delta = event.clientX - dragStartRef.current;
      setRotation(dragRotationRef.current + delta * 0.006);
      return;
    }
    if (!pointerIsOnOrbit(event)) {
      // 只有会话椭圆环带是旋转热区。HUD、中心入口、背景和底部信息区都不会
      // 因为鼠标经过而带动星环，避免用户阅读固定信息时画面持续移动。
      targetVelocityRef.current = 0;
      velocityRef.current = 0;
      event.currentTarget.style.cursor = "default";
      return;
    }
    event.currentTarget.style.cursor = "ew-resize";
    const normalized = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    // 只保留很小的中心停止区。前排会话位于中心两侧，旧的 18% 死区会让鼠标
    // 放在前排卡片上时几乎没有速度。
    const deadZone = 0.05;
    const magnitude = Math.max(0, Math.abs(normalized) - deadZone) / (1 - deadZone);
    // 节点坐标使用 x = sin(angle)、y = cos(angle)。在屏幕坐标系中，angle
    // 增大时节点会逆时针运动，因此这里需要反转速度符号：鼠标位于右半环时
    // 给负角速度（顺时针），位于左半环时给正角速度（逆时针）。
    targetVelocityRef.current = -Math.sign(normalized) * magnitude * 0.026;
  }

  function beginDrag(event: PointerEvent<HTMLDivElement>) {
    if ((event.target as HTMLElement).closest("button, input, select")) return;
    if (!pointerIsOnOrbit(event)) return;
    dragStartRef.current = event.clientX;
    dragRotationRef.current = rotation;
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function endDrag(event: PointerEvent<HTMLDivElement>) {
    dragStartRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  // 宽屏舞台要让前排卡片真正铺开。旧值把 16 个节点压在中央 1040px 范围内，
  // 在 1900px 屏幕上会大量重叠；纵向半径也不足以形成参考图中的前后排层次。
  const radiusX = Math.min(stageSize.width * 0.5, 900);
  const radiusY = Math.min(stageSize.height * 0.25, 205);
  const count = Math.max(visibleConversations.length, 1);

  // 统一计算节点布局，避免渲染卡片和判断“最前方三张卡片”时使用两套公式。
  // 相比上一版适当降低谐波系数，让前排卡片间距收紧，同时继续保持后排密集。
  const orbitLayouts = visibleConversations.map((_, index) => {
    const phase = rotation + (index / count) * Math.PI * 2;
    const angle = phase + 0.7 * Math.sin(phase) + 0.1 * Math.sin(phase * 2);
    return { angle, depth: (Math.cos(angle) + 1) / 2 };
  });
  const foregroundIndexes = new Set(
    orbitLayouts
      .map((layout, index) => ({ index, depth: layout.depth }))
      .sort((left, right) => right.depth - left.depth)
      .slice(0, Math.min(3, orbitLayouts.length))
      .map((item) => item.index),
  );

  function pointerIsOnOrbit(event: PointerEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement;
    if (target.closest(".orbit-hud, .orbit-core, .orbit-system-strip, .orbit-help")) {
      return false;
    }
    // 最靠近用户的三张卡片承担主要阅读与点击操作。进入这些卡片时将目标速度
    // 和当前速度都归零，避免惯性继续带着卡片移动。
    if (target.closest('[data-orbit-foreground="true"]')) {
      return false;
    }
    // 前排卡片会向椭圆中心延伸，其中一部分在几何上落入 inner ellipse。
    // 如果只按鼠标坐标判断，鼠标进入这些卡片后会被误认为离开环带并立即停转。
    // 卡片是星环的一部分，因此无论卡片位于前排还是后排，都应保持方向控制。
    if (target.closest(".orbit-node")) {
      return true;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left - rect.width / 2;
    // 会话节点的 CSS top 为 43%，所以交互椭圆也必须使用相同圆心。
    const y = event.clientY - rect.top - rect.height * 0.43;
    const outerX = radiusX + 185;
    const outerY = radiusY + 135;
    const innerX = Math.max(180, radiusX - 205);
    const innerY = Math.max(78, radiusY - 105);
    const withinOuter = (x * x) / (outerX * outerX) + (y * y) / (outerY * outerY) <= 1;
    const outsideInner = (x * x) / (innerX * innerX) + (y * y) / (innerY * innerY) >= 1;
    return withinOuter && outsideInner;
  }

  return (
    <div className="orbit-page">
      <header className="orbit-header">
        <div className="brand-lockup">
          <div className="brand-mark"><CircuitBoard size={22} /></div>
          <div><strong>ChipFlow Agent</strong><span>Session Orbit · 芯片设计协作空间</span></div>
        </div>
        <nav className="orbit-main-nav" aria-label="主导航">
          <button className="active" type="button"><Home size={15} />工作台</button>
          <button type="button" title="会话流视图将在后续接入"><GitBranch size={15} />会话流</button>
          <button type="button" title="智能体管理将在后续接入"><Bot size={15} />智能体</button>
          <button type="button" title="项目管理将在后续接入"><FolderKanban size={15} />项目</button>
          <button type="button" title="资源中心将在后续接入"><Boxes size={15} />资源中心</button>
        </nav>
        <div className="orbit-controls">
          <label className="orbit-search">
            <Search size={14} />
            <input
              ref={searchInputRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索会话、项目或模块"
            />
            <kbd><Command size={10} /> K</kbd>
          </label>
          <select value={projectFilter} onChange={(event) => setProjectFilter(event.target.value)}>
            <option value="all">全部项目</option>
            {projects.map((project) => <option key={project} value={project}>{project}</option>)}
          </select>
        </div>
      </header>

      <main
        className="orbit-stage"
        ref={stageRef}
        onPointerMove={updatePointerVelocity}
        onPointerDown={beginDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onPointerLeave={() => {
          targetVelocityRef.current = 0;
          velocityRef.current = 0;
          dragStartRef.current = null;
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") setRotation((value) => value - 0.22);
          if (event.key === "ArrowRight") setRotation((value) => value + 0.22);
        }}
        tabIndex={0}
        aria-label="会话星环，可使用鼠标移动、拖拽或左右方向键旋转"
      >
        <div className="orbit-stars" />
        <div className="orbit-grid" />
        <div className="orbit-globe-motion" aria-hidden="true">
          <span className="globe-motion-grid" />
          <span className="globe-motion-shine" />
        </div>
        <svg className="orbit-circuit-flow" viewBox="0 0 1664 936" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <filter id="electron-glow" x="-300%" y="-300%" width="700%" height="700%">
              <feGaussianBlur stdDeviation="4" result="blur" />
              <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
            </filter>
          </defs>
          {[
            "M 770 825 L 735 825 L 713 802 L 662 802 L 638 781 L 566 781 L 538 760 L 420 760",
            "M 894 825 L 929 825 L 951 802 L 1002 802 L 1026 781 L 1098 781 L 1126 760 L 1244 760",
            "M 775 842 L 742 842 L 718 861 L 665 861 L 641 881 L 568 881 L 541 902 L 448 902",
            "M 889 842 L 922 842 L 946 861 L 999 861 L 1023 881 L 1096 881 L 1123 902 L 1216 902",
            "M 785 805 L 755 805 L 733 786 L 683 786 L 659 766 L 596 766 L 572 747 L 488 747",
            "M 879 805 L 909 805 L 931 786 L 981 786 L 1005 766 L 1068 766 L 1092 747 L 1176 747",
            "M 802 870 L 802 889 L 781 906 L 781 936",
            "M 862 870 L 862 889 L 883 906 L 883 936",
          ].map((path, index) => (
            <g className={`electron-path electron-path-${index + 1}`} key={path}>
              <path d={path} />
              <circle r="4" filter="url(#electron-glow)">
                <animateMotion
                  path={path}
                  dur={`${3.2 + (index % 4) * 0.65}s`}
                  begin={`${-(index * 0.48)}s`}
                  repeatCount="indefinite"
                />
              </circle>
            </g>
          ))}
        </svg>
        <div className="orbit-cursor-aura" />
        <div className="orbit-scan-beam" />
        <div className="orbit-energy-sweep" />
        <div className="orbit-globe" aria-hidden="true">
          <span className="orbit-globe-latitude latitude-a" />
          <span className="orbit-globe-latitude latitude-b" />
          <span className="orbit-globe-meridian meridian-a" />
          <span className="orbit-globe-meridian meridian-b" />
          <span className="orbit-globe-light" />
        </div>
        <div className="orbit-projector" aria-hidden="true"><i /><i /><i /></div>
        <div className="orbit-stage-platform" aria-hidden="true">
          <span className="platform-ring platform-ring-a" />
          <span className="platform-ring platform-ring-b" />
          <span className="platform-ring platform-ring-c" />
          <span className="platform-energy-slots" />
        </div>
        <div className="orbit-chip-floor" aria-hidden="true">
          <span className="floor-chip"><i /></span>
          <span className="light-tower tower-a"><i /></span>
          <span className="light-tower tower-b"><i /></span>
          <span className="light-tower tower-c"><i /></span>
          <span className="light-tower tower-d"><i /></span>
        </div>
        <div className="orbit-halo orbit-halo-outer" />
        <div className="orbit-halo orbit-halo-inner" />

        <section className="orbit-hud orbit-hud-left" aria-label="系统状态">
          <div className="orbit-hud-title"><Radio size={13} /><span>ChipFlow 网络状态</span></div>
          <div className="orbit-hud-row"><span>链路状态</span><strong className="hud-online">在线</strong></div>
          <div className="orbit-hud-row"><span>活跃运行数</span><strong>{String(activeCount).padStart(2, "0")}</strong></div>
          <div className="orbit-hud-row"><span>会话节点数</span><strong>{String(conversations.length).padStart(2, "0")}</strong></div>
          <div className="orbit-hud-meter"><i style={{ width: `${Math.min(100, 28 + conversations.length * 4)}%` }} /></div>
        </section>

        <section className="orbit-hud orbit-hud-right" aria-label="会话遥测">
          <div className="orbit-hud-title"><Activity size={13} /><span>会话遥测（实时）</span></div>
          <div className="orbit-hud-row"><span>项目数</span><strong>{String(projects.length).padStart(2, "0")}</strong></div>
          <div className="orbit-hud-row"><span>总 Token 数</span><strong>{totalTokens.toLocaleString()}</strong></div>
          <div className="orbit-hud-row"><span>显示上限</span><strong>32</strong></div>
          <div className="orbit-signal"><i /><i /><i /><i /><i /></div>
        </section>

        <button className="orbit-core" type="button" onClick={onCreateConversation}>
          <span className="orbit-reactor-ring orbit-reactor-ring-a" />
          <span className="orbit-reactor-ring orbit-reactor-ring-b" />
          <span className="orbit-core-icon"><Plus size={22} /></span>
          <strong>NEW SESSION</strong>
          <small>启动协作任务</small>
        </button>

        {visibleConversations.map((conversation, index) => {
          const { angle, depth } = orbitLayouts[index];
          // 对像素坐标取整，避免文字所在合成层停在半像素位置产生灰边。
          const x = Math.round(Math.sin(angle) * radiusX);
          const y = Math.round(Math.cos(angle) * radiusY);
          // 不再缩放整个 DOM。浏览器对 scale 后的文字会做纹理插值，造成明显发虚。
          // 直接改变实际宽度表达远近，文字仍由浏览器按最终尺寸重新排版和栅格化。
          const cardWidth = Math.round(205 + depth * 85);
          const active = Boolean(conversation.active_run_id);
          return (
            <button
              className={`orbit-node ${active ? "orbit-node-active" : ""}`}
              key={conversation.session_id}
              type="button"
              data-orbit-foreground={foregroundIndexes.has(index) ? "true" : "false"}
              style={{
                transform: `translate(${x}px, ${y}px)`,
                width: `${cardWidth}px`,
                marginLeft: `${-cardWidth / 2}px`,
                opacity: 1,
                zIndex: Math.round(depth * 100) + 10,
                filter: "none",
              }}
              onClick={() => onOpenConversation(conversation)}
            >
              <span className="orbit-node-glow" />
              <span className="orbit-node-corner corner-a" />
              <span className="orbit-node-corner corner-b" />
              <span className="orbit-node-index">NODE-{String(index + 1).padStart(2, "0")}</span>
              <span className="orbit-node-status">{active ? "任务进行中" : "可继续"}</span>
              <strong>{conversation.title || "未命名会话"}</strong>
              <span className="orbit-node-module">
                {conversation.active_run_id ? `run-${conversation.active_run_id.slice(0, 8)}` : conversation.project_id}
                {" · "}{conversation.module_name || "未指定模块"}
              </span>
              <footer>
                <span><UserRound size={11} />{conversation.primary_agent} Agent</span>
                <span><MessageSquareText size={11} />{conversation.message_count}</span>
              </footer>
              <small><Clock3 size={10} />{new Date(conversation.updated_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</small>
            </button>
          );
        })}

        {!visibleConversations.length && (
          <div className="orbit-empty">
            <MessageSquareText size={24} />
            <strong>{conversations.length ? "没有匹配的会话" : "还没有历史会话"}</strong>
            <span>点击中心节点创建第一个芯片设计会话</span>
          </div>
        )}

        <div className="orbit-help">鼠标进入会话环控制方向和速度 · 移出环带立即停止 · 点击卡片进入会话</div>
        <div className="orbit-system-strip">
          <span><ShieldCheck size={11} /> MEMORY LINKED</span>
          <span><Cpu size={11} /> MULTI-AGENT CORE</span>
          <span><Activity size={11} /> DYNAMIC TOOL ROUTING</span>
        </div>
      </main>
    </div>
  );
}
