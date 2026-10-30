import { describe, expect, it } from "vitest";
import { isJobKillable, jobKillArgs, isJobListFrame } from "../src/wire/jobs";

describe("jobs wire", () => {
  it("recognizes roster frames", () => {
    expect(isJobListFrame({ type: "rows", jobs: [] })).toBe(true);
    expect(isJobListFrame({ type: "status", job: {} })).toBe(false);
  });

  it("marks only running jobs killable", () => {
    expect(isJobKillable("running")).toBe(true);
    expect(isJobKillable("stopping")).toBe(false);
    expect(isJobKillable("completed")).toBe(false);
    expect(isJobKillable(undefined)).toBe(false);
  });

  it("builds job/kill args", () => {
    expect(jobKillArgs("s1", "job-abc")).toEqual({ sessionId: "s1", jobId: "job-abc" });
  });
});
