import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", () => ({
    toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }),
}));

const fetchApi = vi.fn();
vi.mock("@/lib/api", () => ({
    fetchApi: (...args: unknown[]) => fetchApi(...args),
    readApiError: vi.fn(async () => "boom"),
}));

import { ConnectedMcpClients, type McpClientGrant } from "./mcp-clients";

const CLIENTS: McpClientGrant[] = [
    {
        id: "mcpg_1",
        client_name: "Claude Desktop",
        host: "claude.ai",
        scopes: ["api:read", "comments:write"],
        created_at: "2026-01-01T00:00:00+00:00",
        last_used_at: "2026-02-01T12:00:00+00:00",
    },
];

function jsonResponse(body: unknown, ok = true) {
    return { ok, json: async () => body } as unknown as Response;
}

beforeEach(() => {
    fetchApi.mockReset();
});

afterEach(() => {
    cleanup();
});

describe("ConnectedMcpClients", () => {
    it("lists the user's connected clients", async () => {
        fetchApi.mockResolvedValue(jsonResponse(CLIENTS));
        render(<ConnectedMcpClients />);

        await waitFor(() => expect(fetchApi).toHaveBeenCalledWith("/api/auth/mcp-clients"));
        expect(await screen.findByText("Claude Desktop")).toBeInTheDocument();
        expect(screen.getByText("claude.ai")).toBeInTheDocument();
        expect(screen.getByText("api:read comments:write")).toBeInTheDocument();
    });

    it("shows an empty state when nothing is connected", async () => {
        fetchApi.mockResolvedValue(jsonResponse([]));
        render(<ConnectedMcpClients />);

        expect(await screen.findByText(/No connected MCP clients/i)).toBeInTheDocument();
    });

    it("asks for confirmation before revoking", async () => {
        fetchApi.mockResolvedValue(jsonResponse(CLIENTS));
        render(<ConnectedMcpClients />);

        fireEvent.click(
            await screen.findByRole("button", { name: "Revoke MCP client Claude Desktop" }),
        );

        const dialog = await screen.findByRole("dialog");
        expect(
            within(dialog).getByText(/Claude Desktop stops working on Prism/i),
        ).toBeInTheDocument();
        // The confirm is a hold, so a plain click has not revoked anything yet.
        expect(fetchApi).not.toHaveBeenCalledWith(
            "/api/auth/mcp-clients/mcpg_1",
            expect.objectContaining({ method: "DELETE" }),
        );
    });
});
