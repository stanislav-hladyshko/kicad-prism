import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", () => ({
    toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }),
}));

const fetchApi = vi.fn();
vi.mock("@/lib/api", () => ({
    fetchApi: (...args: unknown[]) => fetchApi(...args),
    readApiError: vi.fn(async () => "boom"),
}));

import { McpGovernanceSettings } from "./mcp-governance";

const SETTINGS = {
    projectScope: "all",
    projectIds: [],
    disabledTools: [],
    tools: [
        { name: "get_project", title: "Get a project" },
        { name: "get_bom", title: "Get the BOM" },
    ],
};

const PROJECTS = [
    { id: "project-alpha", name: "Alpha" },
    { id: "project-beta", name: "Beta" },
];

const AUDIT = {
    rows: [
        {
            id: 7,
            at: "2026-10-01T10:00:00+00:00",
            user_email: "priya@example.test",
            auth_type: "pat",
            client_id: "pat:tok",
            token_id: "tok",
            method: "tools/call",
            tool: "get_project",
            project_id: "project-alpha",
            revision: "0123456789abcdef0123456789abcdef01234567",
            outcome: "ok",
            code: null,
            duration_ms: 3,
        },
    ],
    next: 42,
};

function jsonResponse(body: unknown, ok = true) {
    return { ok, json: async () => body } as unknown as Response;
}

function route(input: unknown, init?: RequestInit) {
    const url = String(input);
    if (url === "/api/settings/mcp" && init?.method === "PUT") {
        return jsonResponse({
            projectScope: "listed",
            projectIds: ["project-alpha"],
            disabledTools: ["get_project"],
        });
    }
    if (url === "/api/settings/mcp") return jsonResponse(SETTINGS);
    if (url === "/api/projects/") return jsonResponse(PROJECTS);
    if (url.startsWith("/api/admin/mcp-audit")) {
        if (url.includes("before=")) {
            return jsonResponse({ rows: [{ ...AUDIT.rows[0], id: 6 }], next: null });
        }
        return jsonResponse(AUDIT);
    }
    return jsonResponse({}, false);
}

beforeEach(() => {
    fetchApi.mockReset();
    fetchApi.mockImplementation(async (input: unknown, init?: RequestInit) => route(input, init));
});

describe("McpGovernanceSettings", () => {
    it("lists tools and the audit", async () => {
        render(<McpGovernanceSettings />);

        expect(await screen.findByText("Get a project")).toBeInTheDocument();
        expect(screen.getByRole("switch", { name: "get_project" })).toBeChecked();
        expect(await screen.findByText("priya@example.test")).toBeInTheDocument();
        expect(screen.getByText("project-alpha")).toBeInTheDocument();
        expect(screen.getByText("01234567")).toBeInTheDocument();
        expect(screen.getByRole("columnheader", { name: "Token" })).toBeInTheDocument();
        expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
    });

    it("saves a listed scope and disabled tools", async () => {
        render(<McpGovernanceSettings />);
        expect(await screen.findByText("Get a project")).toBeInTheDocument();

        fireEvent.click(screen.getByRole("switch", { name: "get_project" }));
        fireEvent.click(screen.getByLabelText("Project scope"));
        fireEvent.click(await screen.findByRole("option", { name: "Listed projects" }));
        fireEvent.click(await screen.findByRole("checkbox", { name: "Alpha" }));
        fireEvent.click(screen.getByRole("button", { name: "Save" }));

        await waitFor(() =>
            expect(fetchApi).toHaveBeenCalledWith(
                "/api/settings/mcp",
                expect.objectContaining({
                    method: "PUT",
                    body: JSON.stringify({
                        projectScope: "listed",
                        projectIds: ["project-alpha"],
                        disabledTools: ["get_project"],
                    }),
                }),
            ),
        );
    });

    it("loads the next audit page with the cursor and the filters", async () => {
        render(<McpGovernanceSettings />);
        expect(await screen.findByText("priya@example.test")).toBeInTheDocument();

        fireEvent.click(screen.getByRole("button", { name: "Load more" }));
        await waitFor(() =>
            expect(fetchApi).toHaveBeenCalledWith(
                "/api/admin/mcp-audit?limit=100&before=42",
                expect.anything(),
            ),
        );

        fireEvent.change(screen.getByLabelText("Audit client filter"), {
            target: { value: "pat:tok" },
        });
        await waitFor(() =>
            expect(fetchApi).toHaveBeenCalledWith(
                "/api/admin/mcp-audit?limit=100&client_id=pat%3Atok",
                expect.anything(),
            ),
        );

        fireEvent.change(screen.getByLabelText("Audit token filter"), {
            target: { value: "tok" },
        });
        await waitFor(() =>
            expect(fetchApi).toHaveBeenCalledWith(
                "/api/admin/mcp-audit?limit=100&client_id=pat%3Atok&token_id=tok",
                expect.anything(),
            ),
        );

        fireEvent.click(screen.getByLabelText("Audit kind filter"));
        fireEvent.click(await screen.findByRole("option", { name: "Writes" }));
        await waitFor(() =>
            expect(fetchApi).toHaveBeenCalledWith(
                "/api/admin/mcp-audit?limit=100&client_id=pat%3Atok&token_id=tok&kind=write",
                expect.anything(),
            ),
        );
    });
});
