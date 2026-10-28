import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CommentCard } from "./comment-card";
import type { Comment } from "@/types/comments";

const base: Comment = {
    id: "c1",
    author: "Ana",
    timestamp: "2026-08-23T00:00:00Z",
    status: "OPEN",
    context: "PCB",
    location: { x: 1, y: 2, layer: "F.Cu" },
    content: "Check this footprint.",
    replies: [],
    commentClass: "question",
    severity: "minor",
    mentions: [],
};

function renderCard(comment: Comment) {
    render(
        <CommentCard
            projectId="p1"
            comment={comment}
            screenPosition={{ x: 10, y: 10 }}
            canModify
            onClose={vi.fn()}
            onResolve={vi.fn()}
            onReply={vi.fn().mockResolvedValue(undefined)}
            onDelete={vi.fn().mockResolvedValue(undefined)}
        />,
    );
}

afterEach(cleanup);

describe("CommentCard author kinds", () => {
    it("marks an agent-authored reply with its client", () => {
        renderCard({
            ...base,
            replies: [
                {
                    id: "r1",
                    author: "prism-bot",
                    authorKind: "agent",
                    authorClientId: "claude-code",
                    timestamp: "2026-08-23T00:01:00Z",
                    content: "Fixed in the next revision.",
                },
            ],
        });

        expect(screen.getByText("Agent").getAttribute("title")).toBe("via claude-code");
        expect(screen.getByText("prism-bot")).toBeTruthy();
    });

    it("leaves a person's thread unmarked", () => {
        renderCard({
            ...base,
            replies: [
                {
                    id: "r1",
                    author: "Bo",
                    authorKind: "user",
                    timestamp: "2026-08-23T00:01:00Z",
                    content: "Looks right.",
                },
            ],
        });

        expect(screen.queryByText("Agent")).toBeNull();
        expect(screen.queryByText("Service")).toBeNull();
    });
});
