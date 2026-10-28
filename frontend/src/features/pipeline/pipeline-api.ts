import { useMemo } from "react";

import { fetchJson } from "@/lib/api";
import { useServerState } from "@/lib/server-state";

/** One stage of one board, as the repository's `.prism-pipeline.json` states it (backend pipeline_service). */
export interface PipelineCell {
    state: "ok" | "warn" | "bad" | "human" | "none" | "na";
    text: string;
    why: string;
}

export interface PipelineRow {
    id: string;
    dir: string | null;
    project_id: string | null;
    board: string;
    kind: string;
    stop: string;
    cells: PipelineCell[];
    human: PipelineCell;
}

export interface Pipeline {
    repo_id: string;
    repo_name: string;
    as_of: string;
    columns: string[];
    rows: PipelineRow[];
}

/** A board's row with the stage names it is read against. */
export interface ProjectPipeline {
    row: PipelineRow;
    columns: string[];
    asOf: string;
}

export interface PipelineStage {
    number: number;
    name: string;
    cell: PipelineCell;
}

// Process stages are the numbered columns ("6. Placement"); the rest ("Reference") are checks beside the process.
const STAGE_COLUMN = /^(\d+)\.\s*(.+)$/;

export function pipelineStages(pipeline: ProjectPipeline): PipelineStage[] {
    return pipeline.columns.flatMap((column, index) => {
        const match = STAGE_COLUMN.exec(column);
        const cell = pipeline.row.cells[index];
        return match && cell ? [{ number: Number(match[1]), name: match[2], cell }] : [];
    });
}

/** "6/10 · Placement" when the board stops at a stage, otherwise the repository's own word ("not started"). */
export function pipelineSummary(pipeline: ProjectPipeline, stageCount: number): string {
    const match = STAGE_COLUMN.exec(pipeline.row.stop);
    return match ? `${match[1]}/${stageCount} · ${match[2]}` : pipeline.row.stop;
}

export function stoppedStageNumber(row: PipelineRow): number | null {
    const match = STAGE_COLUMN.exec(row.stop);
    return match ? Number(match[1]) : null;
}

const PIPELINE_KEY = ["workspace", "pipeline"] as const;

/** Every board the committed pipeline files cover, by Prism project id. One request serves the gallery and a project page. */
export function useProjectPipelines() {
    const read = useServerState(
        PIPELINE_KEY,
        (signal) => fetchJson<{ pipelines: Pipeline[] }>("/api/workspace/pipeline", { signal }, "Could not load the pipeline"),
    );
    const byProject = useMemo(() => {
        const map = new Map<string, ProjectPipeline>();
        for (const pipeline of read.data?.pipelines ?? []) {
            for (const row of pipeline.rows) {
                if (row.project_id) map.set(row.project_id, { row, columns: pipeline.columns, asOf: pipeline.as_of });
            }
        }
        return map;
    }, [read.data]);
    return { byProject, loading: read.loading, error: read.error };
}

/** The latest run of a stage's redo workflow that still needs attention (backend pipeline_rerun_service). */
export interface StageRun {
    state: "queued" | "running" | "review" | "draft" | "stopped" | "token" | "stand";
    text: string;
    url: string;
}

export interface StageReruns {
    configured: boolean;
    reason: string | null;
    stages: Record<string, StageRun>;
}

/** Stages a repair run may redo; 1-5 change the circuit (docs/product/features/rerun-stage.md in the board repository). */
export const RERUNNABLE_STAGES = new Set([6, 7, 8, 9, 10]);
const RERUN_POLL_MS = 20_000;

export function stageRerunsKey(projectId: string) {
    return ["pipeline", "reruns", projectId] as const;
}

/** Runs of the board's stage workflow, re-read while the page is visible; `null` reads nothing. */
export function useStageReruns(projectId: string | null) {
    return useServerState(
        projectId ? stageRerunsKey(projectId) : null,
        (signal) => fetchJson<StageReruns>(`/api/projects/${encodeURIComponent(projectId ?? "")}/pipeline/reruns`, { signal },
            "Could not load the stage runs"),
        { pollMs: RERUN_POLL_MS, whileVisible: true },
    );
}

export function startStageRerun(projectId: string, stage: number) {
    return fetchJson<{ state: string }>(
        `/api/projects/${encodeURIComponent(projectId)}/pipeline/stages/${stage}/rerun`,
        { method: "POST" },
        "Не вдалось запустити",
    );
}
