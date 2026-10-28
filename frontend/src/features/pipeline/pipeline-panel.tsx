import { useEffect, useRef } from "react";

import { cn } from "@/lib/utils";

import { pipelineStages, stoppedStageNumber, type PipelineCell, type ProjectPipeline } from "./pipeline-api";

const STATE_TEXT_CLASS: Record<PipelineCell["state"], string> = {
    ok: "text-success",
    warn: "text-warning",
    bad: "text-destructive",
    human: "text-primary",
    none: "text-muted-foreground",
    na: "text-muted-foreground",
};

interface CheckRowProps {
    id?: string;
    label: string;
    cell: PipelineCell;
    emphasised?: boolean;
    focused?: boolean;
}

function CheckRow({ id, label, cell, emphasised, focused }: CheckRowProps) {
    return (
        <li
            id={id}
            className={cn(
                "grid grid-cols-[minmax(8rem,14rem)_minmax(5rem,8rem)_1fr] gap-3 px-3 py-2 text-sm",
                emphasised && "bg-destructive/10",
                focused && "ring-2 ring-inset ring-ring",
            )}
        >
            <span className={cn(emphasised && "font-semibold")}>{label}</span>
            <span className={cn("font-medium", STATE_TEXT_CLASS[cell.state])}>{cell.text}</span>
            <span className="text-muted-foreground">{cell.why}</span>
        </li>
    );
}

interface PipelinePanelProps {
    pipeline: ProjectPipeline;
    /** Stage number a header mark was clicked on: scrolled into view. */
    focusStage: number | null;
}

/** The Pipeline section of a project: why each stage stands where it does, the checks beside the process, and the sign-off. */
export function PipelinePanel({ pipeline, focusStage }: PipelinePanelProps) {
    const listRef = useRef<HTMLOListElement>(null);
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
