import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowLeft, ArrowRight, BookOpen, Check, Compass, X } from 'lucide-react';
import type { View } from '../types';
import './guided-tour.css';

const STORAGE_KEY = 'businessmap.onboarding.v1';
type Progress = { status: 'new' | 'paused' | 'done'; step: number };
type Step = { id: string; title: string; view?: View; target: string; description: string; actions: string[]; result: string; tip: string };
const STEPS: Step[] = [
  { id: 'intro', title: '从代码出发，读懂你的业务', target: '.navigation-items', description: '这五个入口串起模型、代码、数据和业务知识。接下来会在真实界面中逐一带你认识。', actions: ['先浏览笔记与图谱，再了解自己的模型、仓库和数据库如何接入。', '点击「去操作」可暂停引导，动手试一试；左下角随时继续。'], result: '没有 API 密钥也可以浏览知识库，体验示例 SQL 查询。', tip: '引导只介绍界面。模型测试、代码分析和 SQL 执行都由你主动点击。' },
  { id: 'knowledge', title: '先读懂一条业务规则', view: 'knowledge', target: '.file-tree, .map-main', description: '业务地图按主题组织业务知识。正文链接把业务对象、规则、流程和数据口径连接起来。', actions: ['在知识目录中打开一篇笔记，例如示例中的「订单流程」。', '点击正文链接继续阅读；右侧「反向链接」显示哪些笔记引用了它。'], result: '沿着链接，你能从业务流程找到对应的规则和数据口径。', tip: '自己的知识库没有示例笔记时，选择任意笔记开始；目录隐藏时可在标题栏展开。' },
  { id: 'graph', title: '在图谱里探索业务关系', view: 'graph', target: '.graph-view:not(.local) .graph-canvas', description: '一个节点对应一篇笔记，连线来自笔记之间的链接。', actions: ['拖动节点观察关联，滚轮缩放，拖动空白处移动视角。', '悬停查看名称与邻居，点击节点打开笔记；点击「关系图谱」可以返回。'], result: '从一条规则出发，找出它关联的流程、数据和其他规则。', tip: '找不到节点时，清除筛选并点击右下角「适应图谱大小」。' },
  { id: 'models', title: '选择你自己的 AI 模型', view: 'settings', target: '[data-tour="model-add"]', description: '可以保存多个平台的模型，并在 AI 问答中选择使用哪一个。', actions: ['点击「添加模型」，填写 API 地址、模型 ID 和 API 密钥，再保存配置。', '点击「测试」验证连接；需要切换默认模型时点击「启用」。'], result: '模型列表显示已保存的服务，测试成功后再用于仓库分析或问答。', tip: '测试会发出真实请求。连接失败时，检查服务地址、密钥权限和模型 ID。没有账号也可点击「下一步」继续了解。' },
  { id: 'repository', title: '把代码整理成业务地图', view: 'repositories', target: '[data-tour="repository-add"]', description: '接入同一业务的一组前后端，共同生成业务地图和分析总结。', actions: ['点击「接入业务项目」，填写项目名称和前后端各自的仓库地址，点击「接入项目与前后端仓库」。', '等两个仓库都显示「已就绪」，点击「绑定项目模型」并确认，再点击「完整分析项目」。'], result: '文档保存后，在知识目录的「业务地图」按主题查阅，在「项目总结」阅读分析总结。', tip: '开始完整分析时，必要代码片段会发送给绑定的模型。请选择允许接收这两个仓库代码的模型。' },
  { id: 'database', title: '查询数据，再整理成报表', view: 'query', target: '[data-tour="database-source"]', description: '选择数据源、执行只读 SQL，结果会显示在同一页的表格中。', actions: ['有示例数据时，选择示例数据库并执行预填 SQL；也可通过「添加数据源」接入自己的数据库。', '看到结果后点击「生成报表」，填写标题并选择柱状图、趋势图或数据表。'], result: '示例 SQL 按渠道汇总订单数量和金额；报表保留这次结果与原 SQL。', tip: '远程数据库使用只读账号。报表是查询快照，需要新数据时重新查询；不要把示例 SQL 直接用于自己的表。' },
  { id: 'chat', title: '让问答帮你解释规则或查数据', view: 'chat', target: '.bm-chat-composer', description: '业务逻辑问题和数据问题，在这里选择不同模式处理。', actions: ['「知识问答」：例如“订单退款需要满足哪些条件？”，回答后查看引用来源。', '「只读问数」：选择模型与数据库，再问“按渠道统计订单数量和金额”，检查执行的 SQL 与结果。'], result: '解释业务规则不需要查询数据库；问数会在只读限制内执行生成的 SQL。', tip: '未配置模型时只能做本地知识检索，不能生成 SQL。回答不完整时，先补充业务笔记或缩小问题范围。' },
  { id: 'mcp', title: '也可以连接其他 AI 工具', view: 'settings', target: '[data-tour="mcp-copy"]', description: '这是可选的一步。MCP 让兼容的 AI 工具调用思径的知识与查询能力。', actions: ['点击「复制 MCP 配置」，粘贴到支持 MCP 的工具配置中。', '连接成功后，让它搜索业务知识、读取表结构或执行只读查询。'], result: '教程到这里就结束了。需要回看时，点击左下角「使用引导」。', tip: '外部工具的配置入口各不相同。仅在桌面会话中保存的数据库密码，独立 MCP 进程无法读取。' },
];

function readProgress(): Progress {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
    if (value && ['paused', 'done'].includes(value.status) && Number.isInteger(value.step) && value.step >= 0 && value.step < STEPS.length) return value;
  } catch { /* A blocked or old local store must not prevent opening the app. */ }
  return { status: 'new', step: 0 };
}
function saveProgress(progress: Progress) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(progress)); } catch { /* Continue in this window. */ }
}
type Rect = { x: number; y: number; width: number; height: number };

export default function GuidedTour({ ready, onNavigate }: { ready: boolean; onNavigate: (view: View) => boolean }) {
  const [progress, setProgress] = useState(readProgress);
  const [open, setOpen] = useState(() => progress.status === 'new');
  const [rect, setRect] = useState<Rect | null>(null);
  const [placement, setPlacement] = useState({ left: 220, top: 100, width: 400, maxHeight: 600 });
  const dialog = useRef<HTMLDialogElement>(null);
  const card = useRef<HTMLElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const launcher = useRef<HTMLButtonElement>(null);
  const target = useRef<HTMLElement | null>(null);
  const step = STEPS[progress.step];
  const shown = open && ready;

  const record = (next: Progress) => { setProgress(next); saveProgress(next); };
  const go = (index: number) => {
    const next = STEPS[index];
    if (next.view && !onNavigate(next.view)) return;
    setRect(null);
    record({ status: 'paused', step: index });
  };
  const close = (practice = false, done = false) => {
    record({ status: done ? 'done' : 'paused', step: progress.step });
    setOpen(false);
    const focusTarget = practice ? target.current : null;
    requestAnimationFrame(() => {
      const controls = focusTarget ? [focusTarget, ...focusTarget.querySelectorAll<HTMLElement>('button,input,select,textarea,a[href],[tabindex],[contenteditable="true"]')] : [];
      const control = controls.find(element => element.tabIndex >= 0 && !element.matches(':disabled,[hidden],[inert]') && element.getClientRects().length > 0);
      (control?.isConnected ? control : launcher.current)?.focus({ preventScroll: true });
    });
  };
  const start = () => {
    const index = progress.status === 'done' ? 0 : progress.step;
    if (STEPS[index].view && !onNavigate(STEPS[index].view!)) return;
    record({ status: 'paused', step: index });
    setOpen(true);
  };

  useEffect(() => {
    if (!shown) return;
    const element = dialog.current!;
    element.showModal();
    // Remember dismissal across restarts even if the window closes mid-tour.
    saveProgress({ status: 'paused', step: progress.step });
    return () => element.close();
  }, [shown]);

  useEffect(() => {
    if (!shown) return;
    heading.current?.focus({ preventScroll: true });
    card.current?.querySelector('.tour-body')?.scrollTo({ top: 0 });
    let frame = 0;
    let observed: HTMLElement | null = null;
    const measure = () => {
      frame = 0;
      const candidate = [...document.querySelectorAll<HTMLElement>(step.target)].find(element => element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0) || null;
      if (candidate !== observed) {
        if (observed) resize.unobserve(observed);
        observed = candidate;
        target.current = candidate;
        if (candidate) resize.observe(candidate);
      }
      const beforeScroll = candidate?.getBoundingClientRect();
      // The same control can move out of view when the window shrinks. Recheck
      // visibility on resize instead of only scrolling when the target changes.
      if (beforeScroll && (beforeScroll.top < 48 || beforeScroll.bottom > innerHeight - 32 || beforeScroll.left < 8 || beforeScroll.right > innerWidth - 8)) candidate?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
      const bounds = candidate?.getBoundingClientRect();
      const x = Math.max(8, (bounds?.left || 0) - 6), y = Math.max(48, (bounds?.top || 0) - 6);
      const next = bounds ? { x, y, width: Math.max(0, Math.min(innerWidth - 8, bounds.right + 6) - x), height: Math.max(0, Math.min(innerHeight - 32, bounds.bottom + 6) - y) } : null;
      const spot = next && next.width > 0 && next.height > 0 ? next : null;
      setRect(previous => JSON.stringify(previous) === JSON.stringify(spot) ? previous : spot);
      const margin = 12, upper = 52, lower = innerHeight - 40, gap = 18;
      let width = Math.min(400, innerWidth - margin * 2), maxHeight = lower - upper;
      const cardHeight = card.current?.offsetHeight || 420;
      let left = (innerWidth - width) / 2, top = (innerHeight - Math.min(cardHeight, maxHeight)) / 2;
      if (spot) {
        const rightSpace = innerWidth - margin - spot.x - spot.width - gap;
        const leftSpace = spot.x - gap - margin;
        const belowSpace = lower - spot.y - spot.height - gap;
        const aboveSpace = spot.y - gap - upper;
        // A narrower card beside the target keeps the actual control visible.
        // For wide rows, fit above/below and scroll only the explanation.
        if (rightSpace >= width || leftSpace >= width || Math.max(rightSpace, leftSpace) >= 300) {
          const right = rightSpace >= width || (leftSpace < width && rightSpace >= leftSpace);
          width = Math.min(width, right ? rightSpace : leftSpace);
          left = right ? spot.x + spot.width + gap : spot.x - width - gap;
          top = spot.y;
        } else if (Math.max(belowSpace, aboveSpace) >= 260) {
          const below = belowSpace >= aboveSpace;
          maxHeight = below ? belowSpace : aboveSpace;
          left = spot.x;
          top = below ? spot.y + spot.height + gap : spot.y - gap - Math.min(cardHeight, maxHeight);
        } else {
          // Very large targets can occupy the whole viewport. Keep the card
          // usable and choose the corner that covers the least of the target.
          const height = Math.min(cardHeight, maxHeight);
          const corners = [{ left: margin, top: upper }, { left: innerWidth - width - margin, top: upper }, { left: margin, top: lower - height }, { left: innerWidth - width - margin, top: lower - height }];
          const overlap = (position: { left: number; top: number }) => Math.max(0, Math.min(position.left + width, spot.x + spot.width) - Math.max(position.left, spot.x)) * Math.max(0, Math.min(position.top + height, spot.y + spot.height) - Math.max(position.top, spot.y));
          const best = corners.reduce((best, next) => overlap(next) < overlap(best) ? next : best);
          left = best.left; top = best.top;
        }
      }
      const position = { left: Math.max(margin, Math.min(innerWidth - width - margin, left)), top: Math.max(upper, Math.min(lower - Math.min(cardHeight, maxHeight), top)), width, maxHeight };
      setPlacement(previous => JSON.stringify(previous) === JSON.stringify(position) ? previous : position);
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(measure); };
    const resize = new ResizeObserver(schedule);
    if (card.current) resize.observe(card.current);
    // Lazy-loaded pages can mount after the step changes. Observe only the app,
    // not the tour itself, so positioning does not create a mutation loop.
    const mutations = new MutationObserver(schedule);
    const app = document.querySelector('.app-shell');
    if (app) mutations.observe(app, { childList: true, subtree: true });
    window.addEventListener('resize', schedule);
    window.addEventListener('scroll', schedule, true);
    schedule();
    return () => { cancelAnimationFrame(frame); resize.disconnect(); mutations.disconnect(); window.removeEventListener('resize', schedule); window.removeEventListener('scroll', schedule, true); };
  }, [shown, progress.step, step.target]);

  return <>
    <button ref={launcher} className="tour-launcher" aria-label="使用引导" onClick={start} disabled={!ready}><Compass size={16}/><span>{progress.status === 'paused' && progress.step > 0 ? '继续引导' : '使用引导'}</span>{progress.status === 'paused' && progress.step > 0 && <small>{progress.step + 1}/{STEPS.length}</small>}</button>
    {shown && createPortal(<dialog ref={dialog} className="guided-tour" aria-label="使用引导" aria-describedby="tour-description" data-tour-step={step.id} onCancel={event => { event.preventDefault(); close(); }}>
      <svg className="tour-shade" aria-hidden="true" width="100%" height="100%"><defs><mask id="tour-spotlight-mask"><rect width="100%" height="100%" fill="white"/>{rect && <rect {...rect} rx="9" fill="black"/>}</mask></defs><rect width="100%" height="100%" fill="var(--overlay)" mask="url(#tour-spotlight-mask)"/>{rect && <rect {...rect} rx="9" className="tour-spotlight" data-tour-spotlight="true"/>}</svg>
      <section ref={card} className="tour-card" style={placement}>
        <div className="tour-topline"><span><BookOpen size={14}/>使用引导 · {progress.step + 1} / {STEPS.length}</span><button className="icon-button" aria-label="关闭引导" onClick={() => close()}><X size={17}/></button></div>
        <div className="tour-progress" aria-hidden="true">{STEPS.map((item, index) => <i key={item.id} className={index <= progress.step ? 'read' : ''}/>)}</div>
        <h2 ref={heading} tabIndex={-1}>{step.title}</h2>
        <div className="tour-body" tabIndex={0} aria-label="引导说明">
        <p id="tour-description" className="tour-description">{step.description}</p>
        <ol className="tour-actions-list">{step.actions.map(action => <li key={action}>{action}</li>)}</ol>
        <p className="tour-result"><Check size={15}/><span><strong>预期效果</strong>{step.result}</span></p>
        <p className="tour-tip">{step.tip}</p>
        {!rect && <p className="tour-missing" role="status">正在定位操作区域；也可以继续阅读或暂时跳过。</p>}
        </div>
        <div className="tour-controls"><button className="button" disabled={progress.step === 0} onClick={() => go(progress.step - 1)}><ArrowLeft size={14}/>上一步</button>{progress.step > 0 && <button className="button" onClick={() => close(true)}>去操作</button>}<button className="button primary" onClick={() => progress.step === STEPS.length - 1 ? close(false, true) : go(progress.step + 1)}>{progress.step === STEPS.length - 1 ? '完成引导' : progress.step === 0 ? '开始引导' : '下一步'}<ArrowRight size={14}/></button></div>
        <div className="tour-footer"><button onClick={() => close()}>暂时跳过</button>{progress.step > 0 ? <button onClick={() => go(0)}>从头开始</button> : <span>可随时从左下角重新打开</span>}</div>
      </section>
    </dialog>, document.body)}
  </>;
}
