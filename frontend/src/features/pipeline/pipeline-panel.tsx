import { useEffect, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { invalidateServerState } from "@/lib/server-state";

import { cn } from "@/lib/utils";

import {
    pipelineStages,
    RERUNNABLE_STAGES,
    stageRerunsKey,
    startStageRerun,
    stoppedStageNumber,
    useStageReruns,
    type PipelineCell,
    type ProjectPipeline,
    type StageRun,
} from "./pipeline-api";
import { STAGE_SEGMENT_CLASS } from "./pipeline-bar";
import { StageRerun } from "./stage-rerun";

interface CheckRowProps {
    id?: string;
    label: string;
    cell: PipelineCell;
    emphasised?: boolean;
    focused?: boolean;
    /** The stage list has a fourth column for the redo control. */
    action?: ReactNode;
}

function CheckRow({ id, label, cell, emphasised, focused, action }: CheckRowProps) {
    return (
        <li
            id={id}
            className={cn(
                "grid gap-3 px-3 py-2 text-sm",
                action === undefined
                    ? "grid-cols-[minmax(8rem,14rem)_minmax(5rem,8rem)_1fr]"
                    : "grid-cols-[minmax(8rem,14rem)_minmax(5rem,8rem)_1fr_auto] items-center",
                emphasised && "bg-destructive/10",
                focused && "ring-2 ring-inset ring-ring",
            )}
        >
            <span className={cn(emphasised && "font-semibold")}>{label}</span>
            {/* The word stays in the foreground colour: the state tokens are too dark for text on the dark theme
                (destructive measured 2:1). The colour rides on the same mark as in the header graph. */}
            <span className="flex items-center gap-2 font-medium">
                <span className={cn("h-2.5 w-2.5 shrink-0 rounded-sm", STAGE_SEGMENT_CLASS[cell.state])} aria-hidden />
                {cell.text}
            </span>
            <span className="text-muted-foreground">{cell.why}</span>
            {action}
        </li>
    );
}

interface PipelinePanelProps {
    pipeline: ProjectPipeline;
    /** Stage number a header mark was clicked on: scrolled into view. */
    focusStage: number | null;
    /** Prism project of the board: reads and starts stage redo runs. Without it the panel only reads the file. */
    projectId?: string;
    canRerun?: boolean;
}

const QUEUED: StageRun = { state: "queued", text: "у черзі", url: "" };

/** The Pipeline section of a project: why each stage stands where it does, the checks beside the process, and the sign-off. */
export function PipelinePanel({ pipeline, focusStage, projectId, canRerun = false }: PipelinePanelProps) {
    const listRef = useRef<HTMLOListElement>(null);
    const reruns = useStageReruns(projectId ?? null);
    // the dispatched run shows up in GitHub's list a few seconds later; until then the row says it is queued
    const [dispatched, setDispatched] = useState<number | null>(null);
    const [starting, setStarting] = useState<number | null>(null);
    const runs = reruns.data?.stages ?? {};
    const isGoing = (run: StageRun | undefined) => run?.state === "queued" || run?.state === "running";
    // an older finished run of the stage is not the one just dispatched: it keeps "у черзі" until the new one is listed
    const runOf = (stage: number): StageRun | undefined =>
        dispatched === stage && !isGoing(runs[String(stage)]) ? QUEUED : runs[String(stage)];
    const boardBusy = dispatched !== null || Object.values(runs).some(isGoing);
    const disabledReason = reruns.data && !reruns.data.configured ? reruns.data.reason ?? "Запуск агента не налаштовано" : null;

    useEffect(() => {
        const listed = dispatched === null ? undefined : reruns.data?.stages[String(dispatched)];
        if (listed?.state === "queued" || listed?.state === "running") setDispatched(null);
    }, [dispatched, reruns.data]);

    async function rerun(stage: number) {
        if (!projectId) return;
        setStarting(stage);
        try {
            await startStageRerun(projectId, stage);
            setDispatched(stage);
            void invalidateServerState(stageRerunsKey(projectId));
        } catch (error) {
            toast.error(`Не вдалось запустити: ${error instanceof Error ? error.message : String(error)}`);
        } finally {
            setStarting(null);
        }
    }
    const stages = pipelineStages(pipeline);
    const stopped = stoppedStageNumber(pipeline.row);
    const stageColumns = new Set(stages.map((stage) => `${stage.number}. ${stage.name}`));
    const otherChecks = pipeline.columns.flatMap((column, index) => {
        const cell = pipeline.row.cells[index];
        return !stageColumns.has(column) && cell ? [{ column, cell }] : [];
    });

    useEffect(() => {
        if (focusStage === null) return;
        listRef.current?.querySelector(`#pipeline-stage-${focusStage}`)?.scrollIntoView({ block: "center" });
    }, [focusStage]);

    return (
        <div className="space-y-6">
            <div className="space-y-1">
                <h2 className="text-2xl font-bold">Pipeline</h2>
                <p className="text-sm text-muted-foreground">
                    {pipeline.row.kind}, last gate run {pipeline.asOf || "-"}
                </p>
            </div>
            <ol ref={listRef} className="divide-y rounded-lg border">
                {stages.map((stage) => (
                    <CheckRow
                        key={stage.number}
                        id={`pipeline-stage-${stage.number}`}
                        label={`${stage.number}. ${stage.name}`}
                        cell={stage.cell}
                        emphasised={stage.number === stopped}
                        focused={stage.number === focusStage}
                        action={projectId ? (
                            RERUNNABLE_STAGES.has(stage.number) ? (
                                <StageRerun
                                    stageLabel={`${stage.number}. ${stage.name}`}
                                    run={runOf(stage.number)}
                                    canRerun={canRerun}
                                    disabledReason={disabledReason}
                                    boardBusy={boardBusy}
                                    starting={starting === stage.number}
                                    onRerun={() => void rerun(stage.number)}
                                />
                            ) : <span />
                        ) : undefined}
                    />
                ))}
            </ol>
            <ul className="divide-y rounded-lg border">
                {otherChecks.map(({ column, cell }) => (
                    <CheckRow key={column} label={column} cell={cell} />
                ))}
                <CheckRow label="Sign-off" cell={pipeline.row.human} />
            </ul>
        </div>
    );
}
