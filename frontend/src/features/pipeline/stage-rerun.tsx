import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

import type { StageRun } from "./pipeline-api";

interface StageRerunProps {
    stageLabel: string;
    run: StageRun | undefined;
    /** The caller's role may start a run (designer, admin). Others see the run state only. */
    canRerun: boolean;
    /** Why no run can start here (not configured); the button stays visible and says so. */
    disabledReason: string | null;
    /** A run of this board is queued or running: the workflow takes one per board. */
    boardBusy: boolean;
    starting: boolean;
    onRerun: () => void;
}

/** The redo control of one stage row: the latest run's state with a link to it, and the "Переробити" button. */
export function StageRerun({ stageLabel, run, canRerun, disabledReason, boardBusy, starting, onRerun }: StageRerunProps) {
    const going = run?.state === "queued" || run?.state === "running";
    const button = canRerun && !going ? (
        <Button
            type="button"
            variant="outline"
            size="sm"
            aria-label={`Переробити: ${stageLabel}`}
            disabled={Boolean(disabledReason) || boardBusy || starting}
            onClick={onRerun}
        >
            Переробити
        </Button>
    ) : null;

    return (
        <span className="flex items-center justify-end gap-3">
            {run?.url ? (
                <a className="text-xs underline-offset-2 hover:underline" href={run.url} target="_blank" rel="noreferrer">
                    {run.text}
                </a>
            ) : (
                run && <span className="text-xs">{run.text}</span>
            )}
            {button && disabledReason ? (
                <Tooltip>
                    <TooltipTrigger asChild>
                        {/* a disabled button takes no pointer or focus, so the wrapper carries the reason */}
                        <span tabIndex={0} className="inline-flex cursor-not-allowed" aria-label={disabledReason}>
                            {button}
                        </span>
                    </TooltipTrigger>
                    <TooltipContent>{disabledReason}</TooltipContent>
                </Tooltip>
            ) : (
                button
            )}
        </span>
    );
}
