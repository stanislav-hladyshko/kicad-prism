import { useCallback, useRef, useState } from "react";
import { CheckCircle2, Download, MessageSquare, Reply, Send, X } from "lucide-react";
import { CommentAuthorKindBadge } from "@/components/comment-author-kind-badge";
import { Button } from "@/components/ui/button";
import { ReplyTrackerState } from "@/features/tracker-integration/reply-tracker-state";
import { TrackerIssueAction } from "@/features/tracker-integration/tracker-issue-action";
import { fetchApi, readApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { Comment, CommentContext } from "@/types/comments";
import { ThreadMessage } from "@/features/rich-comments/thread-message";
import { ThreadUpdateContext } from "@/features/rich-comments/thread-updates";
import { useReplyBox } from "@/features/rich-comments/use-reply-box";
import {
    RichComposer,
    type RichComposerHandle,
    type RichComposerState,
} from "@/features/rich-comments/rich-composer";

const EMPTY_DRAFT: RichComposerState = { markdown: "", uploading: false };

export function discussionReportUrl(projectId: string, base: string, compare: string, domain: CommentContext): string {
    const query = new URLSearchParams({ base, compare, domain });
    return `/api/projects/${encodeURIComponent(projectId)}/comparison-comments/report?${query}`;
}

interface DiscussionAnchor {
    id: string;
    label: string;
    page?: string | null;
}

interface ComparisonDiscussionRailProps {
    projectId: string;
    base: string;
    compare: string;
    domain: CommentContext;
    anchor: DiscussionAnchor | null;
    comments: Comment[];
    canComment: boolean;
    onCommentsChange: (comments: Comment[]) => void;
    onClose: () => void;
    embedded?: boolean;
}

export function ComparisonDiscussionRail({
    projectId,
    base,
    compare,
    domain,
    anchor,
    comments,
    canComment,
    onCommentsChange,
    onClose,
    embedded = false,
}: ComparisonDiscussionRailProps) {
    const [draft, setDraft] = useState<RichComposerState>(EMPTY_DRAFT);
    const draftRef = useRef<RichComposerHandle>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const createThread = async () => {
        if (!draft.markdown || draft.uploading) return;
        setBusy(true);
        setError(null);
        try {
            const response = await fetchApi(`/api/projects/${projectId}/comparison-comments`, {
                method: "POST",
                body: JSON.stringify({
                    baseCommit: base,
                    compareCommit: compare,
                    domain,
                    content: draft.markdown,
                    contentFormat: "md",
                    filePath: anchor?.page ?? undefined,
                    semanticItemId: anchor?.id ?? undefined,
                    semanticItemRef: anchor?.label ?? undefined,
                    anchorKind: anchor ? "group" : "comparison",
                }),
            });
            if (!response.ok) {
                throw new Error(await readApiError(response, "Failed to add discussion"));
            }
            onCommentsChange([...comments, (await response.json()) as Comment]);
            draftRef.current?.clear();
        } catch (caught) {
            setError(caught instanceof Error ? caught.message : "Failed to add discussion");
        } finally {
            setBusy(false);
        }
    };

    const resolveThread = async (comment: Comment) => {
        try {
            const response = await fetchApi(`/api/projects/${projectId}/comments/${comment.id}`, {
                method: "PATCH",
                body: JSON.stringify({
                    status: comment.status === "RESOLVED" ? "OPEN" : "RESOLVED",
                }),
            });
            if (!response.ok) {
                setError(await readApiError(response, "Failed to update discussion"));
                return;
            }
            const updated = (await response.json()) as Comment;
            onCommentsChange(comments.map((item) => item.id === updated.id ? updated : item));
        } catch (caught) {
            setError(caught instanceof Error ? caught.message : "Failed to update discussion");
        }
    };

    /**
     * Issue actions queue forge work; the comment stream refreshes this rail
     * when the worker links the issue, so only a returned thread is applied.
     */
    const trackerAction = async (path: string, fallback: string) => {
        setError(null);
        try {
            const response = await fetchApi(`/api/projects/${projectId}/comments/${path}`, { method: "POST" });
            if (!response.ok) throw new Error(await readApiError(response, fallback));
            const payload = (await response.json()) as Partial<Comment>;
            if (payload.id && Array.isArray(payload.replies)) {
                onCommentsChange(comments.map((item) => item.id === payload.id ? payload as Comment : item));
            }
        } catch (caught) {
            setError(caught instanceof Error ? caught.message : fallback);
        }
    };

    const applyThread = useCallback((updated: Comment) => {
        onCommentsChange(comments.map((item) => item.id === updated.id ? updated : item));
    }, [comments, onCommentsChange]);

    return (
        <ThreadUpdateContext.Provider value={applyThread}>
        <aside
            className={cn(
                "flex h-full flex-col bg-background",
                embedded
                    ? "w-full"
                    : "w-80 shrink-0 border-l max-lg:absolute max-lg:inset-y-0 max-lg:right-0 max-lg:z-30 max-lg:shadow-xl",
            )}
        >
            {!embedded && (
            <div className="flex items-center justify-between border-b px-3 py-2">
                <div className="flex items-center gap-2">
                    <MessageSquare className="h-4 w-4" />
                    <span className="text-sm font-semibold">Comments</span>
                    <span className="rounded-full bg-muted px-1.5 text-[10px]">
                        {comments.filter((comment) => comment.status === "OPEN").length}
                    </span>
                </div>
                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onClose}>
                    <X className="h-3.5 w-3.5" />
                    <span className="sr-only">Close discussion</span>
                </Button>
            </div>
            )}

            <div className="min-h-0 flex-1 space-y-3 overflow-auto p-3">
                {comments.length > 0 && (
                    <a
                        href={discussionReportUrl(projectId, base, compare, domain)}
                        download
                        className="flex items-center justify-end gap-1 text-[11px] text-muted-foreground hover:text-foreground"
                    >
                        <Download className="h-3 w-3" />
                        Download discussion (Markdown + snips)
                    </a>
                )}
                {!comments.length && (
                    <p className="py-8 text-center text-xs text-muted-foreground">
                        No discussion threads for this comparison yet.
                    </p>
                )}
                {comments.map((comment) => (
                    <RailThread
                        key={comment.id}
                        projectId={projectId}
                        comment={comment}
                        canComment={canComment}
                        onThreadChange={applyThread}
                        onResolve={resolveThread}
                        onTrackerAction={trackerAction}
                        onError={setError}
                    />
                ))}
            </div>

            {canComment && (
                <div className="space-y-2 border-t p-3">
                    <div className="text-[10px] text-muted-foreground">
                        {anchor ? `New thread on ${anchor.label}` : "New comparison thread"}
                    </div>
                    <RichComposer
                        ref={draftRef}
                        projectId={projectId}
                        ariaLabel="New discussion"
                        placeholder="Add review context… Paste a snip to attach it"
                        onChange={setDraft}
                        onSubmit={() => void createThread()}
                        disabled={busy}
                        minHeightClassName="min-h-20"
                    />
                    {error && <p className="text-[10px] text-destructive">{error}</p>}
                    <Button
                        size="sm"
                        className="w-full"
                        disabled={busy || !draft.markdown || draft.uploading}
                        onClick={() => void createThread()}
                    >
                        <Send className="mr-2 h-3.5 w-3.5" />
                        Add thread
                    </Button>
                </div>
            )}
        </aside>
        </ThreadUpdateContext.Provider>
    );
}

interface RailThreadProps {
    projectId: string;
    comment: Comment;
    canComment: boolean;
    onThreadChange: (updated: Comment) => void;
    onResolve: (comment: Comment) => Promise<void>;
    onTrackerAction: (path: string, fallback: string) => Promise<void>;
    onError: (message: string | null) => void;
}

/** One discussion thread with its own reply box. */
function RailThread({
    projectId, comment, canComment, onThreadChange, onResolve, onTrackerAction, onError,
}: RailThreadProps) {
    const replyBox = useReplyBox();
    const reply = replyBox.draft;
    const [busy, setBusy] = useState(false);

    const addReply = async () => {
        if (!reply.markdown || reply.uploading) return;
        setBusy(true);
        try {
            const response = await fetchApi(
                `/api/projects/${projectId}/comments/${comment.id}/replies`,
                {
                    method: "POST",
                    body: JSON.stringify({ content: reply.markdown, contentFormat: "md" }),
                },
            );
            if (!response.ok) {
                throw new Error(await readApiError(response, "Failed to add reply"));
            }
            const payload = (await response.json()) as { comment: Comment };
            onThreadChange(payload.comment);
            replyBox.close();
        } catch (caught) {
            onError(caught instanceof Error ? caught.message : "Failed to add reply");
        } finally {
            setBusy(false);
        }
    };

    return (
            <article
                key={comment.id}
                className={`rounded-md border p-3 text-xs ${
                    comment.status === "RESOLVED" ? "opacity-60" : ""
                }`}
            >
                <div className="flex items-start justify-between gap-2">
                    <div>
                        <div className="flex items-center gap-1 font-medium">
                            <span className="truncate">{comment.author}</span>
                            <CommentAuthorKindBadge
                                authorKind={comment.authorKind}
                                authorClientId={comment.authorClientId}
                                authorClientName={comment.authorClientName}
                            />
                        </div>
                        <div className="mt-0.5 text-[10px] text-muted-foreground">
                            {comment.elementRef || "Whole comparison"}
                        </div>
                    </div>
                    {canComment && comment.permissions?.canResolve !== false && (
                        <Button
                            variant="ghost"
                            size="icon"
                            className="h-6 w-6"
                            onClick={() => void onResolve(comment)}
                            aria-label={
                                comment.status === "RESOLVED"
                                    ? "Reopen discussion"
                                    : "Resolve discussion"
                            }
                        >
                            <CheckCircle2 className="h-3.5 w-3.5" />
                        </Button>
                    )}
                </div>
                <ThreadMessage
                    projectId={projectId}
                    thread={comment}
                    canInteract={canComment}
                    onQuote={replyBox.quote}
                    bodyClassName="text-xs leading-relaxed"
                    className="mt-2"
                />
                <div className="mt-2">
                    <TrackerIssueAction
                        comment={comment}
                        onPromote={(id) => onTrackerAction(`${id}/promote`, "Failed to create issue")}
                        onRetry={(id) => onTrackerAction(`${id}/tracker/retry`, "Failed to retry issue sync")}
                    />
                </div>
                {!!comment.replies.length && (
                    <div className="mt-2 space-y-2 border-l pl-2">
                        {comment.replies.map((item) => (
                            <div key={item.id ?? `${item.timestamp}-${item.author}-${item.content}`}>
                                <span className="flex items-center gap-1">
                                    <span className="font-medium">{item.author}</span>
                                    <CommentAuthorKindBadge
                                        authorKind={item.authorKind}
                                        authorClientId={item.authorClientId}
                                        authorClientName={item.authorClientName}
                                    />
                                </span>
                                <ThreadMessage
                                    projectId={projectId}
                                    thread={comment}
                                    reply={item}
                                    canInteract={canComment}
                                    onQuote={replyBox.quote}
                                    bodyClassName="text-xs"
                                />
                                <ReplyTrackerState
                                    reply={item}
                                    provider={comment.tracker?.provider}
                                    onShare={(replyId) => onTrackerAction(
                                        `${comment.id}/replies/${replyId}/share`, "Failed to share reply",
                                    )}
                                />
                            </div>
                        ))}
                    </div>
                )}
                {canComment && (
                    <div className="mt-2">
                        {replyBox.open ? (
                            <div className="space-y-2">
                                <RichComposer
                                    ref={replyBox.ref}
                                    projectId={projectId}
                                    ariaLabel="Reply"
                                    autoFocus
                                    initialMarkdown={replyBox.seed}
                                    placeholder="Reply…"
                                    onChange={replyBox.setDraft}
                                    onSubmit={() => void addReply()}
                                    onCancel={replyBox.close}
                                    disabled={busy}
                                    minHeightClassName="min-h-16"
                                />
                                <Button
                                    size="sm"
                                    className="h-7"
                                    disabled={busy || !reply.markdown || reply.uploading}
                                    onClick={() => void addReply()}
                                >
                                    <Send className="mr-1.5 h-3 w-3" />
                                    Reply
                                </Button>
                            </div>
                        ) : (
                            <Button
                                variant="ghost"
                                size="sm"
                                className="h-7 px-1.5"
                                onClick={() => replyBox.openWith()}
                            >
                                <Reply className="mr-1.5 h-3 w-3" />
                                Reply
                            </Button>
                        )}
                    </div>
                )}
            </article>
    );
}
