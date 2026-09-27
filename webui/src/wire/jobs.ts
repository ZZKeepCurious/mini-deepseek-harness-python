// job/list client — per-session job roster stream.
// Wire contract (source: miniharness/web/streams.py `_job_list`, aligned with
// upstream packages/api/job-controller rows.ts):
//   open job/list with payload {args:{sessionId}} → first frame:
//     {type:'rows', jobs:[JobView]} then whole-roster replacement frames on
//     lifecycle commits (registered/progress/stopping/settled/removed).
// JobView shape (source: miniharness/jobs/view.py `build_view`, upstream
// jobs/src/view.ts): `{id, kind, label, status, startedAt, output:{total,
// earliest, spillPaths?}, owner?, outputLimitBytes?, progress?, detail?,
// finishedAt?}`.

export interface JobView {
  id: string;
  kind: string;
  label: string;
  status: string;
  startedAt: number;
  output: {
    total: number;
    earliest: number;
    spillPaths?: string[];
  };
  owner?: string;
  outputLimitBytes?: number;
  progress?: string;
  detail?: string;
  finishedAt?: number;
}

export type JobListFrame = { type: "rows"; jobs: JobView[] };

export function isJobListFrame(x: unknown): x is JobListFrame {
  if (typeof x !== "object" || x === null) return false;
  const f = x as { type?: unknown; jobs?: unknown };
  return f.type === "rows" && Array.isArray(f.jobs);
}