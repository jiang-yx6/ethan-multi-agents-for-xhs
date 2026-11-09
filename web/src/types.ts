export interface Profile {
  name: string;
  positioning: string;
  audience: string;
  style: string;
  interests: string[];
  experiences: string[];
  preferences: string[];
  boundaries: string[];
}
export interface ProfileRecord {
  profile_id: string;
  profile: Profile;
  updated_at?: string;
}
export interface Idea {
  title: string;
  why: string;
  audience: string;
  angle: string;
  profile_match: string;
  content_type: string;
  trend_urls: string[];
  required_materials: string[];
}
export interface Draft {
  title: string;
  body: string;
  tags: string[];
  images: { description: string; source: string }[];
  version: number;
  approved: boolean;
  content_type: string;
  revision_notes: string[];
}
export interface PublishResult {
  published: boolean;
  status: string | null;
  reason: string;
  platform: string | null;
  account_id: string | null;
  url: string;
  verification: string;
  job_id: string | null;
}
export interface Task {
  task_id: string;
  topic: string;
  status: string;
  current_version: number;
  draft: Draft | null;
  profile: Profile;
  request: {
    goal: string;
    topic: string;
    content_type: string;
    use_trends: boolean;
    supplied_materials: string[];
    constraints: string[];
    needs_research: boolean;
  };
  history: { version: number; title: string; approved: boolean }[];
  published: boolean;
  last_error: string | null;
  publish_result: PublishResult | null;
  review: {
    issues: { message: string; severity: string; target: string }[];
  } | null;
  brief: {
    topic: string;
    angles: string[];
    pending_questions: string[];
    sources: { title: string; url: string }[];
    evidence: { excerpt: string; source_url: string; is_mock: boolean }[];
  } | null;
  trends: { title: string; url: string; summary: string }[];
}
export interface Job {
  job_id: string;
  task_id: string | null;
  kind: string;
  status: string;
  error: string | null;
  result: Record<string, unknown> | null;
  updated_at: string;
  created_at: string;
}
export interface TaskSummary {
  task_id: string;
  topic: string;
  title: string;
  status: string;
  current_version: number;
  profile_name: string;
  published: boolean;
  is_mock_publish: boolean;
  job: Job | null;
}
export interface EventItem {
  event_id: number;
  kind: string;
  message: string;
  created_at: string;
  tool?: string;
  skill_id?: string;
}
export interface ImageView {
  index: number;
  description: string;
  url: string;
  available: boolean;
}
export interface TaskDetail {
  task: Task;
  ideas: Idea[];
  images: ImageView[];
  job: Job | null;
  events: EventItem[];
  allowed_actions: string[];
  workflow: Workflow;
}
export interface Workflow {
  thread_id: string;
  checkpoint_id: string | null;
  status:
    | "not_started"
    | "ready"
    | "waiting_input"
    | "failed"
    | "completed"
    | "inconsistent"
    | "queued"
    | "running";
  next_nodes: string[];
  interrupts: {
    id: string;
    node: string;
    kind: string;
    value: { version?: number; hint?: string; task_id?: string; kind?: string };
    actions: string[];
  }[];
  error: string | null;
}
export interface Account {
  platform: string;
  account_id: string;
  label: string;
  user_id: string;
  nickname: string;
  job: Job | null;
}
export interface ModelSettings {
  backend: "llm" | "mock";
  provider: string;
  model: string;
  api_key_env: string;
  credential_source: "api_key" | "environment";
  api_key_configured?: boolean;
  protocol: "openai" | "anthropic" | null;
  base_url: string | null;
  trust_env: boolean;
  credential_configured?: boolean;
  model_configured?: boolean;
}
export interface ImageReference {
  description: string;
  existing_index?: number;
  media_id?: string;
  url?: string;
  name?: string;
}
export interface Health {
  ok: boolean;
  mode: string;
  ideation_backend: string;
}
