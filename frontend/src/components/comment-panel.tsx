import { useState } from "react";
import {
    CheckCircle,
    ChevronDown,
    Images,
    ChevronRight,
    Circle,
    MessageSquare,
    Reply as ReplyIcon,
    Send,
    Trash2,
    X,
} from "lucide-react";
import { commentClassLabel, type Comment } from "@/types/comments";
import { CommentAuthorKindBadge } from "@/components/comment-author-kind-badge";
import { CommentSeverityBadge } from "@/components/comment-severity-badge";
import { ReplyTrackerState } from "@/features/tracker-integration/reply-tracker-state";
import { TrackerIssueAction } from "@/features/tracker-integration/tracker-issue-action";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { EcadCommentAnchorResolution } from "@/types/ecad-viewer";
import { SnipGallery } from "@/features/rich-comments/snip-gallery";
import { ThreadMessage } from "@/features/rich-comments/thread-message";
import { useUnreadThreads } from "@/features/rich-comments/unread";
import { RichComposer } from "@/features/rich-comments/rich-composer";
import { useReplyBox } from "@/features/rich-comments/use-reply-box";

interface CommentPanelProps {
    projectId: string;
    comments: Comment[];
    onClose: () => void;
    onResolve: (commentId: string, resolved: boolean) => void;
    onReply: (commentId: string, content: string) => Promise<void>;
    onDelete: (commentId: string) => Promise<void>;
    onCommentClick: (comment: Comment) => void;
    canModify: boolean;
    highlightedId?: string | null;
    embedded?: boolean;
    anchorStatuses?: Record<string, EcadCommentAnchorResolution>;
    onReattach?: (comment: Comment) => Promise<void>;
    onPromote?: (commentId: string) => Promise<void>;
    onRetrySync?: (commentId: string) => Promise<void>;
    onShareReply?: (commentId: string, replyId: string) => Promise<void>;
}

export function CommentPanel({
    projectId,
    comments,
    onClose,
    onResolve,
    onReply,
    onDelete,
    onCommentClick,
    canModify,
    highlightedId = null,
    embedded = false,
    anchorStatuses = {},
    onReattach,
    onPromote,
    onRetrySync,
    onShareReply,
}: CommentPanelProps) {
    const [filter, setFilter] = useState<"ALL" | "OPEN" | "RESOLVED">("ALL");
    const [view, setView] = useState<"threads" | "snips">("threads");
    const { isUnread, markSeen } = useUnreadThreads(projectId);
    const openThread = (comment: Comment) => {
        markSeen(comment);
        onCommentClick(comment);
    };

    const filteredComments = comments.filter((c) => {
        if (filter === "ALL") return true;
        return c.status === filter;
    });

    return (
        <div
            className={cn(
                "flex h-full flex-col bg-background",
                embedded ? "w-full" : "z-50 w-80 border-l shadow-xl",
            )}
        >
            {!embedded && (
            <div className="flex items-center justify-between border-b p-4">
                <div className="flex items-center gap-2">
                    <MessageSquare className="h-5 w-5" />
                    <h2 className="font-semibold">Comments</h2>
                    <Badge variant="secondary" className="bg-muted text-muted-foreground">
                        {comments.length}
                    </Badge>
                </div>
                <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close comments panel">
                    <X className="h-4 w-4" />
                </Button>
            </div>
            )}

            <div className="flex gap-2 border-b bg-muted/30 p-2">
                {(["ALL", "OPEN", "RESOLVED"] as const).map((value) => (
                    <button
                        key={value}
                        type="button"
                        onClick={() => {
                            setFilter(value);
                            setView("threads");
                        }}
                        className={`rounded-full px-3 py-1 text-xs transition-colors ${
                            view === "threads" && filter === value
                                ? "bg-primary font-medium text-primary-foreground"
                                : "bg-transparent text-muted-foreground hover:bg-muted"
                        }`}
                    >
                        {value === "ALL" ? "All" : value === "OPEN" ? "Open" : "Resolved"}
                    </button>
                ))}
                <button
                    type="button"
                    onClick={() => setView(view === "snips" ? "threads" : "snips")}
                    aria-pressed={view === "snips"}
                    className={`ml-auto inline-flex items-center gap-1 rounded-full px-3 py-1 text-xs transition-colors ${
                        view === "snips"
                            ? "bg-primary font-medium text-primary-foreground"
                            : "bg-transparent text-muted-foreground hover:bg-muted"
                    }`}
                >
                    <Images className="h-3 w-3" />
                    Snips
                </button>
            </div>

            <ScrollArea className="flex-1 p-4">
                <div className="space-y-6">
                    {view === "snips" ? (
                        <SnipGallery
                            projectId={projectId}
                            comments={comments}
                            onOpenThread={(comment) => {
                                setView("threads");
                                openThread(comment);
                            }}
                        />
                    ) : filteredComments.length === 0 ? (
                        <div className="py-8 text-center text-sm text-muted-foreground">
                            No comments found.
                        </div>
                    ) : (
                        (["SCH", "PCB"] as const).map((context) => {
                            const group = filteredComments.filter((c) => c.context === context);
                            if (!group.length) return null;
                            return (
                                <div key={context} className="space-y-3">
                                    <div className="rounded bg-muted/30 px-1 py-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                                        {context === "SCH" ? "Schematic" : "PCB Layout"}
                                    </div>
                                    <div className="space-y-3">
                                        {group.map((comment) => (
                                            <PanelCommentCard
                                                key={comment.id}
                                                projectId={projectId}
                                                comment={comment}
                                                highlighted={highlightedId === comment.id}
                                                unread={isUnread(comment)}
                                                onSeen={() => markSeen(comment)}
                                                onResolve={onResolve}
                                                onReply={onReply}
                                                onDelete={onDelete}
                                                onClick={() => openThread(comment)}
                                                canModify={canModify}
                                                anchorStatus={anchorStatuses[comment.id]}
                                                onReattach={onReattach}
                                                onPromote={onPromote}
                                                onRetrySync={onRetrySync}
                                                onShareReply={onShareReply}
                                            />
                                        ))}
                                    </div>
                                </div>
                            );
                        })
                    )}
                </div>
            </ScrollArea>
        </div>
    );
}

function PanelCommentCard({
    projectId,
    comment,
    highlighted,
    unread,
    onSeen,
    onResolve,
    onReply,
    onDelete,
    onClick,
    canModify,
    anchorStatus,
    onReattach,
    onPromote,
    onRetrySync,
    onShareReply,
}: {
    projectId: string;
    comment: Comment;
    highlighted: boolean;
    unread: boolean;
    onSeen: () => void;
    onResolve: (id: string, resolved: boolean) => void;
    onReply: (id: string, content: string) => Promise<void>;
    onDelete: (id: string) => Promise<void>;
    onClick: () => void;
    canModify: boolean;
    anchorStatus?: EcadCommentAnchorResolution;
    onReattach?: (comment: Comment) => Promise<void>;
    onPromote?: (commentId: string) => Promise<void>;
    onRetrySync?: (commentId: string) => Promise<void>;
    onShareReply?: (commentId: string, replyId: string) => Promise<void>;
}) {
    const replyBox = useReplyBox();
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [expanded, setExpanded] = useState(true);
    const [confirmDelete, setConfirmDelete] = useState(false);
    const canSendReply = Boolean(replyBox.draft.markdown) && !replyBox.draft.uploading && !isSubmitting;
    const isResolved = comment.status === "RESOLVED";
    const anchorIssue = comment.anchorResolution?.state === "unresolved"
        ? ({
            unpinned: "Not pinned to a commit",
            outside_history: "Outside this revision's history",
            ambiguous_merge: "Two branch attachments conflict",
            coordinate_review: "Area needs review on this revision",
        } as Record<string, string>)[comment.anchorResolution.reason] ?? "Anchor needs review"
        : anchorStatus?.state === "missing" ? "Object is missing on this revision" : null;
    const creationCommit = comment.anchor?.commit;
    const creationUrl = creationCommit ? new URL(window.location.href) : null;
    creationUrl?.searchParams.set("commit", creationCommit ?? "");

    const handleReply = async () => {
        if (!canSendReply) return;
        setIsSubmitting(true);
        try {
            await onReply(comment.id, replyBox.draft.markdown);
            replyBox.close();
            onSeen();
        } finally {
            setIsSubmitting(false);
        }
    };

    const canInteract = canModify && comment.permissions?.canReply !== false;
    const quote = (markdown: string) => {
        setExpanded(true);
        replyBox.quote(markdown);
    };

    return (
        <div
            className={`rounded-lg border bg-card text-card-foreground shadow-sm transition-all ${
                isResolved ? "opacity-70" : ""
            } ${highlighted ? "ring-2 ring-primary" : ""}`}
        >
            <button
                type="button"
                className="block w-full cursor-pointer rounded-t-lg p-3 pb-0 text-left hover:bg-muted/50"
                onClick={onClick}
                aria-label={`Open comment from ${comment.author}`}
            >
                <div className="mb-2 flex items-start justify-between">
                    <div className="flex items-center gap-2">
                        {unread && (
                            <span className="h-2 w-2 shrink-0 rounded-full bg-primary" role="img" aria-label="New activity" />
                        )}
                        <span className="text-sm font-semibold">{comment.author}</span>
                        <CommentAuthorKindBadge
                            authorKind={comment.authorKind}
                            authorClientId={comment.authorClientId}
                            authorClientName={comment.authorClientName}
                        />
                        {comment.elementRef && (
                            <Badge variant="outline" className="h-5 px-1 text-[10px]">
                                {comment.elementRef}
                            </Badge>
                        )}
                    </div>
                    <span className="text-[10px] text-muted-foreground">
                        {new Date(comment.timestamp).toLocaleDateString()}
                    </span>
                </div>

                <div className="mb-2 flex flex-wrap gap-1">
                    <Badge variant="secondary">{commentClassLabel(comment.commentClass ?? "general")}</Badge>
                    <CommentSeverityBadge severity={comment.severity ?? "info"} />
                    {anchorIssue && <Badge variant="outline">{anchorIssue}</Badge>}
                </div>
            </button>

            {/* Outside the open-comment button: rich bodies carry links and image buttons. */}
            <div className="px-3">
                <ThreadMessage
                    projectId={projectId}
                    thread={comment}
                    canInteract={canInteract}
                    onQuote={quote}
                    className="mb-3"
                />

                {comment.mentions && comment.mentions.length > 0 && (
                    <div className="mb-3 flex flex-wrap gap-1">
                        {comment.mentions.map((email) => (
                            <Badge key={email} variant="outline" className="max-w-full truncate text-[10px]">
                                @{email}
                            </Badge>
                        ))}
                    </div>
                )}
            </div>

            {(creationUrl || (anchorIssue && comment.permissions?.canEdit && onReattach)) && (
                <div className="flex flex-wrap items-center gap-2 px-3 pb-2 text-xs">
                    {creationUrl && <a className="text-primary underline" href={creationUrl.toString()}>Creation revision</a>}
                    {anchorIssue && comment.permissions?.canEdit && onReattach && (
                        <button type="button" className="text-primary underline"
                            onClick={() => void onReattach(comment)}>Reattach to selected object</button>
                    )}
                </div>
            )}

            {(comment.tracker?.linkState || comment.permissions?.canPublish) && (
                <div className="px-3 pb-2">
                    <TrackerIssueAction comment={comment} onPromote={onPromote} onRetry={onRetrySync} />
                </div>
            )}

            <div className="flex items-center justify-between px-3 pb-3 pt-2">
                {canModify ? (
                    <>
                        <div className="flex gap-1">
                            <Button
                                variant="ghost"
                                size="sm"
                                className="h-6 px-2 text-xs"
                                onClick={replyBox.toggle}
                            >
                                <ReplyIcon className="mr-1 h-3 w-3" />
                                Reply
                            </Button>
                            {comment.permissions?.canDelete !== false && (
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    className="h-6 px-2 text-xs text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                                    onClick={() => setConfirmDelete(true)}
                                >
                                    <Trash2 className="mr-1 h-3 w-3" />
                                    Delete
                                </Button>
                            )}
                        </div>
                        {comment.permissions?.canResolve !== false && (
                            <Button
                                variant="ghost"
                                size="sm"
                                className={`h-6 px-2 text-xs ${isResolved ? "text-success" : "text-muted-foreground"}`}
                                onClick={() => onResolve(comment.id, !isResolved)}
                            >
                                {isResolved ? (
                                    <>
                                        <CheckCircle className="mr-1 h-3 w-3" />
                                        Resolved
                                    </>
                                ) : (
                                    <>
                                        <Circle className="mr-1 h-3 w-3" />
                                        Resolve
                                    </>
                                )}
                            </Button>
                        )}
                    </>
                ) : (
                    <div className="text-xs text-muted-foreground">Read-only</div>
                )}
            </div>

            {(comment.replies.length > 0 || (replyBox.open && canModify)) && (
                <div className="space-y-3 border-t bg-muted/20 p-3">
                    {comment.replies.length > 0 && (
                        <div className="space-y-3">
                            <button
                                type="button"
                                className="flex select-none items-center gap-1 text-xs text-muted-foreground"
                                onClick={() => setExpanded(!expanded)}
                                aria-expanded={expanded}
                            >
                                {expanded ? (
                                    <ChevronDown className="h-3 w-3" />
                                ) : (
                                    <ChevronRight className="h-3 w-3" />
                                )}
                                {comment.replies.length} replies
                            </button>
                            {expanded &&
                                comment.replies.map((item) => (
                                    <div key={item.id ?? `${item.timestamp}-${item.author}-${item.content}`} className="relative border-l-2 border-muted pl-2 text-sm">
                                        <div className="mb-1 flex items-center justify-between">
                                            <span className="flex items-center gap-1">
                                                <span className="text-xs font-medium">{item.author}</span>
                                                <CommentAuthorKindBadge
                                                    authorKind={item.authorKind}
                                                    authorClientId={item.authorClientId}
                                                    authorClientName={item.authorClientName}
                                                />
                                            </span>
                                            <span className="text-[10px] text-muted-foreground">
                                                {new Date(item.timestamp).toLocaleDateString()}
                                            </span>
                                        </div>
                                        <ThreadMessage
                                            projectId={projectId}
                                            thread={comment}
                                            reply={item}
                                            canInteract={canInteract}
                                            onQuote={quote}
                                            bodyClassName="text-muted-foreground"
                                        />
                                        <ReplyTrackerState
                                            reply={item}
                                            provider={comment.tracker?.provider}
                                            onShare={onShareReply
                                                ? (replyId) => onShareReply(comment.id, replyId)
                                                : undefined}
                                        />
                                    </div>
                                ))}
                        </div>
                    )}

                    {replyBox.open && canModify && (
                        <div className="mt-2 flex items-end gap-2 pt-2">
                            <div className="min-w-0 flex-1 space-y-1 text-xs font-medium">
                                <span>Reply</span>
                                {/* Revealing the reply box is a deliberate request to type in it. */}
                                <RichComposer
                                    ref={replyBox.ref}
                                    projectId={projectId}
                                    ariaLabel="Reply"
                                    autoFocus
                                    placeholder="Write a reply..."
                                    initialMarkdown={replyBox.seed}
                                    onChange={replyBox.setDraft}
                                    onSubmit={() => void handleReply()}
                                    onCancel={replyBox.close}
                                    disabled={isSubmitting}
                                    minHeightClassName="min-h-16"
                                    className="font-normal"
                                />
                            </div>
                            <Button
                                size="icon"
                                className="mb-0.5 h-8 w-8"
                                disabled={!canSendReply}
                                onClick={() => void handleReply()}
                                aria-label="Send reply"
                            >
                                <Send className="h-4 w-4" />
                            </Button>
                        </div>
                    )}
                </div>
            )}

            <ConfirmDialog
                open={confirmDelete}
                onOpenChange={setConfirmDelete}
                title="Delete comment"
                description="This removes the comment and its replies from the review thread. It cannot be undone."
                confirmLabel="Delete comment"
                onConfirm={() => {
                    setConfirmDelete(false);
                    void onDelete(comment.id);
                }}
            />
        </div>
    );
}
