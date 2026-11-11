import { isBusy } from "./api";
import type { TaskActions, TaskDetail, TaskSummary } from "./types";
import { Badge, Cover } from "./ui";
import CollaborationWorkspace from "./CollaborationWorkspace";
import "./collaboration.css";

interface Props extends TaskActions {
  tasks: TaskSummary[];
  total: number;
  hasMore: boolean;
  detail: TaskDetail | null;
  selected: string;
  query: string;
  filter: string;
  onSelect: (id: string) => void;
  onQuery: (value: string) => void;
  onFilter: (value: string) => void;
  onMore: () => void;
}
export default function Workspace(props: Props) {
  const { detail, tasks } = props;
  return (
    <div className="workspace collaboration-workspace">
      <section className="task-column" aria-label="任务列表">
        <div className="section-label">
          你的创作
          <span className="count">
            {props.filter === "attention" ? tasks.length : props.total}
          </span>
        </div>
        <input
          className="task-search"
          aria-label="搜索创作任务"
          placeholder="搜索一篇创作…"
          value={props.query}
          onChange={(e) => props.onQuery(e.target.value)}
        />
        <div className="filter-tabs">
          {[
            ["all", "全部"],
            ["attention", "待我处理"],
            ["published", "已发布"],
          ].map(([value, label]) => (
            <button
              key={value}
              className={props.filter === value ? "active" : ""}
              onClick={() => props.onFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="task-list">
          {tasks.map((item, index) => (
            <button
              key={item.task_id}
              className={`task-card ${item.task_id === props.selected ? "active" : ""}`}
              onClick={() => props.onSelect(item.task_id)}
              aria-pressed={item.task_id === props.selected}
            >
              <div className="task-card-top">
                <span className="task-number">
                  NOTE {String(index + 1).padStart(2, "0")}
                </span>
                <Badge
                  status={item.status}
                  busy={isBusy(item.job)}
                  mock={item.is_mock_publish}
                />
              </div>
              <h3>{item.title}</h3>
              <small>
                v{item.current_version} · {item.profile_name}
              </small>
            </button>
          ))}
          {tasks.length === 0 && (
            <div className="empty">
              还没有符合条件的创作。<p>从右上角的新建创作开始。</p>
            </div>
          )}
        </div>
        {props.hasMore && (
          <button className="button small load-more" onClick={props.onMore}>
            加载更多
          </button>
        )}
      </section>
      {detail ? (
        <CollaborationWorkspace
          key={detail.task.task_id}
          {...props}
          detail={detail}
        />
      ) : (
        <article className="editor-panel welcome-panel">
          <p className="eyebrow">A PLACE FOR YOUR IDEAS</p>
          <h2>
            {props.loading
              ? "正在载入你的创作…"
              : "把今天的一个想法，\n留在这里。"}
          </h2>
          <p>
            从本次目标出发，结合你的画像与素材，
            <br />
            一步步走到愿意发布的内容。
          </p>
          <Cover title="每个想法，都有自己的方向。" />
        </article>
      )}
    </div>
  );
}
