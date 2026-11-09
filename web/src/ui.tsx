import { useEffect, useRef, type ReactNode } from "react";

const paths: Record<string, ReactNode> = {
  workspace: (
    <>
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <path d="M14 17h7m-3.5-3.5v7" />
    </>
  ),
  archive: <path d="M4 8h16v12H4zM3 4h18v4H3zm6 8h6" />,
  profile: (
    <>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21v-2a8 8 0 0 1 16 0v2" />
    </>
  ),
  accounts: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="3" />
      <circle cx="9" cy="11" r="2" />
      <path d="M6 16a3 3 0 0 1 6 0m3-6h3m-3 4h3" />
    </>
  ),
  settings: (
    <>
      <path d="M4 7h16M4 17h16" />
      <circle cx="8" cy="7" r="3" />
      <circle cx="16" cy="17" r="3" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  close: <path d="m6 6 12 12M18 6 6 18" />,
  arrow: <path d="M5 12h14m-5-5 5 5-5 5" />,
  refresh: <path d="M3 10a9 9 0 1 1 2 8M3 4v6h6" />,
};
export function Icon({ name }: { name: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] || paths.workspace}
    </svg>
  );
}
export function Heading({
  eyebrow,
  title,
  note,
  action,
}: {
  eyebrow: string;
  title: string;
  note: string;
  action?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="heading-note">{note}</p>
      </div>
      {action}
    </div>
  );
}
export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current!;
    el.showModal();
    return () => el.close();
  }, []);
  return (
    <dialog
      ref={ref}
      aria-labelledby="modal-title"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="modal-heading">
        <h2 id="modal-title">{title}</h2>
        <button className="icon-button" aria-label="关闭弹窗" onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      <div className="modal-content">{children}</div>
    </dialog>
  );
}
export function Cover({ title }: { title: string }) {
  return (
    <div className="cover">
      <div className="cover-top">YOUR OWN VOICE / 创作预览</div>
      <div className="cover-title">{title}</div>
      <svg viewBox="0 0 300 160" fill="none" aria-hidden="true">
        <path
          d="M170 170C70 110 285 50 150-10"
          stroke="#c2ceba"
          strokeWidth="59"
        />
        <path
          d="M225 170C135 100 305 70 215 0"
          stroke="#a8b9a0"
          strokeWidth="2"
          strokeDasharray="4 6"
        />
        <path
          d="M20 145h90M38 131V95h28v36m14 0V72h20v59"
          stroke="#99a98b"
          strokeWidth="1.2"
        />
        <circle cx="198" cy="87" r="4" fill="#b56043" />
      </svg>
      <span className="cover-tag">CREATE & SHARE</span>
    </div>
  );
}
export const states: Record<string, [string, string, number]> = {
  pending: ["等待执行", "neutral", 0],
  ideating: ["待选择方向", "warning", 0],
  researching: ["整理资料中", "neutral", 1],
  researched: ["准备创作", "neutral", 2],
  pending_review: ["待你审核", "warning", 3],
  approved: ["已批准 · 待发布", "", 4],
  submitting: ["正在提交", "neutral", 4],
  submitted: ["已提交 · 待核实", "warning", 4],
  unknown: ["结果待核实", "warning", 4],
  published: ["已确认发布", "", 5],
  failed: ["执行失败", "danger", 0],
  cancelled: ["已取消", "neutral", 0],
};
export function Badge({
  status,
  busy = false,
  mock = false,
}: {
  status: string;
  busy?: boolean;
  mock?: boolean;
}) {
  const [label, tone] = states[status] || [status, "neutral"];
  return (
    <span className={`status ${busy ? "neutral" : tone}`}>
      {busy
        ? "执行中"
        : mock && status === "published"
          ? "本地模拟记录"
          : label}
    </span>
  );
}
export const message = (error: unknown) =>
  error instanceof Error ? error.message : "操作失败，请稍后重试";
export const lines = (value: string) =>
  value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
