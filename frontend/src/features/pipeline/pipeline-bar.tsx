import { cn } from "@/lib/utils";

import {
    pipelineStages,
    pipelineSummary,
    stoppedStageNumber,
    type PipelineCell,
    type ProjectPipeline,
} from "./pipeline-api";

// Passed stages stay quiet and the stage that stops the board is the loudest mark (GOV.UK task list, Pajamas mini graph).
export const STAGE_SEGMENT_CLASS: Record<PipelineCell["state"], string> = {
    ok: "bg-success/40",
    warn: "bg-warning/70",
    bad: "bg-destructive",
    human: "bg-primary/50",
    none: "bg-muted-foreground/20",
    na: "bg-muted-foreground/20",
};

interface PipelineBarProps {
    pipeline: ProjectPipeline;
    /** Header size: segments become buttons that open the stage in the Pipeline section. */
    onSelectStage?: (stageNumber: number) => void;
    className?: string;
}

/** Mini pipeline graph: one mark per process stage and a text summary, so the state never rests on colour alone. */
export function PipelineBar({ pipeline, onSelectStage, className }: PipelineBarProps) {
    const stages = pipelineStages(pipeline);
    if (stages.length === 0) return null;
    const stopped = stoppedStageNumber(pipeline.row);
    const summary = pipelineSummary(pipeline, stages.length);
    const interactive = Boolean(onSelectStage);

    return (
        <div className={cn("flex min-w-0 items-center gap-2", className)}>
            <div
                className={cn("flex shrink-0 items-center", interactive ? "gap-0.5" : "gap-px")}
                role={interactive ? "group" : "img"}
                aria-label={interactive ? "Pipeline stages" : `Pipeline: ${summary}`}
            >
                {stages.map((stage) => {
                    const label = `${stage.number}. ${stage.name}: ${stage.cell.text}`;
                    const isStop = stage.number === stopped;
                    const segmentClass = cn(
                        "block rounded-sm",
                        STAGE_SEGMENT_CLASS[stage.cell.state],
                        interactive ? "w-3" : "w-2.5",
                        isStop ? (interactive ? "h-4" : "h-3") : (interactive ? "h-2.5" : "h-1.5"),
                    );
                    return interactive ? (
                        <button
                            key={stage.number}
                            type="button"
                            className="flex h-6 items-center rounded-sm px-px outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            title={`${label}. ${stage.cell.why}`}
                            aria-label={label}
                            onClick={(event) => {
                                event.stopPropagation();
                                onSelectStage?.(stage.number);
                            }}
                        >
                            <span className={segmentClass} />
                        </button>
                    ) : (
                        <span key={stage.number} className={segmentClass} title={label} />
                    );
                })}
            </div>
            <span
                className={cn("truncate text-xs", stopped ? "font-medium text-foreground" : "text-muted-foreground")}
                title={summary}
                aria-hidden={!interactive}
            >
                {summary}
            </span>
        </div>
    );
}
