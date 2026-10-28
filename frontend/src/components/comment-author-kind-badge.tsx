import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { CommentAuthorKind } from "@/types/comments";

const AUTHOR_KIND_LABELS: Partial<Record<CommentAuthorKind, string>> = {
    agent: "Agent",
    service: "Service",
};

interface CommentAuthorKindBadgeProps {
    authorKind?: CommentAuthorKind;
    /** The client's id (an MCP client's metadata URL). */
    authorClientId?: string | null;
    /** The name the client declared ("Claude Code"); preferred over the id in the title. */
    authorClientName?: string | null;
    className?: string;
}

/**
 * Marks a comment or reply written by an AI agent or a service; a person's
 * stay unmarked.
 */
export function CommentAuthorKindBadge({
    authorKind,
    authorClientId,
    authorClientName,
    className,
}: CommentAuthorKindBadgeProps) {
    const label = authorKind ? AUTHOR_KIND_LABELS[authorKind] : undefined;
    if (!label) return null;
    const client = authorClientName || authorClientId;
    return (
        <Badge
            variant={authorKind === "agent" ? "info" : "outline"}
            className={cn("h-5 px-1.5 text-[10px]", className)}
            title={client ? `via ${client}` : undefined}
        >
            {label}
        </Badge>
    );
}
