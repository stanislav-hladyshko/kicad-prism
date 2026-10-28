import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", () => ({
    toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }),
}));

const fetchApi = vi.fn();
vi.mock("@/lib/api", () => ({
    fetchApi: (...args: unknown[]) => fetchApi(...args),
    readApiError: vi.fn(async () => "boom"),
}));

vi.mock("@/lib/auth", () => ({
    fetchAuthConfig: vi.fn(async () => ({ auth_enabled: true, password_auth_enabled: false })),
    changeOwnPassword: vi.fn(),
}));

vi.mock("@/features/code-hosts/connected-accounts", () => ({
    ConnectedAccounts: () => null,
}));
vi.mock("@/features/code-hosts/code-hosts-settings", () => ({
    CodeHostsSettings: () => null,
}));

import { SettingsDialog } from "./settings-dialog";
import { WebhookSettings } from "./webhook-settings";

const URL = "https://example.test/hooks/prism";
const SECRET = "whsec_abcdefghijklmnopqrstuvwxyz0123456789";
const ROTATED = "whsec_0123456789abcdefghijklmnopqrstuvwxyz";

interface ListedWebhook {
    id: string;
    url: string;
    eventKinds: string[];
    projectIds: string[] | null;
    status: "active" | "failing" | "disabled";
    cursor: number;
    attempts: number;
    failingSince: string | null;
    nextAttemptAt: string | null;
    lastStatus: number | null;
    lastError: string | null;
    lastDeliveryAt: string | null;
    createdBy: string | null;
    createdAt: string;
}

const WEBHOOK: ListedWebhook = {
    id: "wh-1",
    url: URL,
    eventKinds: ["bundle.built", "release.approved"],
    projectIds: null,
    status: "active",
    cursor: 12,
    attempts: 0,
    failingSince: null,
    nextAttemptAt: null,
    lastStatus: 200,
    lastError: null,
    lastDeliveryAt: "2026-10-20T12:00:00+00:00",
    createdBy: "admin@example.test",
    createdAt: "2026-10-01T00:00:00+00:00",
};

const PROJECTS = [
    { id: "project-alpha", name: "Alpha" },
    { id: "project-beta", name: "Beta" },
];

const DELIVERIES = [
    {
        id: 5,
        eventId: 42,
        at: "2026-10-20T12:00:00+00:00",
        statusCode: 200,
        ok: true,
        error: null,
        durationMs: 35,
    },
];

let listed: ListedWebhook[] = [];

function jsonResponse(body: unknown, ok = true) {
    return { ok, json: async () => body } as unknown as Response;
}

function route(input: unknown, init?: RequestInit) {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url === "/api/projects/") return jsonResponse(PROJECTS);
    if (url === "/api/settings/webhooks" && method === "GET") return jsonResponse(listed);
    if (url === "/api/settings/webhooks" && method === "POST") {
        const body = JSON.parse(String(init?.body)) as {
            url: string;
            eventKinds: string[];
            projectIds: string[] | null;
        };
        const created = {
            ...WEBHOOK,
            id: "wh-2",
            url: body.url,
            eventKinds: body.eventKinds,
            projectIds: body.projectIds,
        };
        listed = [...listed, created];
        return jsonResponse({ ...created, secret: SECRET });
    }
    if (url === `/api/settings/webhooks/${WEBHOOK.id}` && method === "PATCH") {
        const body = JSON.parse(String(init?.body)) as Partial<ListedWebhook>;
        const updated = { ...listed[0], ...body };
        listed = [updated];
        return jsonResponse(updated);
    }
    if (url === `/api/settings/webhooks/${WEBHOOK.id}/rotate-secret`) {
        return jsonResponse({ secret: ROTATED });
    }
    if (url === `/api/settings/webhooks/${WEBHOOK.id}/test`) {
        return jsonResponse({ statusCode: 200, ok: true, error: null, durationMs: 42 });
    }
    if (url === `/api/settings/webhooks/${WEBHOOK.id}/deliveries`) {
        return jsonResponse(DELIVERIES);
    }
    if (url === `/api/settings/webhooks/${WEBHOOK.id}` && method === "DELETE") {
        listed = [];
        return jsonResponse(null);
    }
    return jsonResponse({}, false);
}

beforeEach(() => {
    listed = [WEBHOOK];
    fetchApi.mockReset();
    fetchApi.mockImplementation(async (input: unknown, init?: RequestInit) => route(input, init));
});

describe("WebhookSettings", () => {
    it("lists webhooks", async () => {
        render(<WebhookSettings />);

        expect(await screen.findByText(URL)).toBeInTheDocument();
        expect(screen.getByText("2")).toBeInTheDocument();
        expect(screen.getByText("All")).toBeInTheDocument();
        expect(screen.getByText("active")).toBeInTheDocument();
        expect(screen.getByRole("button", { name: `Test ${URL}` })).toBeInTheDocument();
        expect(screen.getByRole("button", { name: `Delete ${URL}` })).toBeInTheDocument();
    });

    it("shows the signing secret once after creating", async () => {
        render(<WebhookSettings />);
        await screen.findByText(URL);

        fireEvent.click(screen.getByRole("button", { name: "Add webhook" }));
        fireEvent.change(screen.getByLabelText("Webhook URL"), { target: { value: "https://new.test/hook" } });
        fireEvent.click(screen.getByRole("checkbox", { name: "bundle.built" }));
        fireEvent.click(screen.getByRole("button", { name: "Create" }));

        expect(await screen.findByDisplayValue(SECRET)).toHaveAttribute("readonly");
        expect(fetchApi).toHaveBeenCalledWith(
            "/api/settings/webhooks",
            expect.objectContaining({
                method: "POST",
                body: JSON.stringify({
                    url: "https://new.test/hook",
                    eventKinds: ["bundle.built"],
                    projectIds: null,
                }),
            }),
        );

        fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
        expect(screen.queryByDisplayValue(SECRET)).not.toBeInTheDocument();
    });

    it("rotates the signing secret", async () => {
        render(<WebhookSettings />);
        await screen.findByText(URL);

        fireEvent.click(screen.getByRole("button", { name: `Rotate secret for ${URL}` }));

        expect(await screen.findByDisplayValue(ROTATED)).toHaveAttribute("readonly");
        expect(fetchApi).toHaveBeenCalledWith(
            `/api/settings/webhooks/${WEBHOOK.id}/rotate-secret`,
            expect.objectContaining({ method: "POST" }),
        );
    });

    it("toggles a webhook's status", async () => {
        render(<WebhookSettings />);
        await screen.findByText(URL);

        fireEvent.click(screen.getByRole("button", { name: `Disable ${URL}` }));

        await waitFor(() =>
            expect(fetchApi).toHaveBeenCalledWith(
                `/api/settings/webhooks/${WEBHOOK.id}`,
                expect.objectContaining({
                    method: "PATCH",
                    body: JSON.stringify({ status: "disabled" }),
                }),
            ),
        );
        expect(await screen.findByText("disabled")).toBeInTheDocument();
    });

    it("shows the test result inline", async () => {
        render(<WebhookSettings />);
        await screen.findByText(URL);

        fireEvent.click(screen.getByRole("button", { name: `Test ${URL}` }));

        expect(await screen.findByText("200 · 42 ms")).toBeInTheDocument();
    });

    it("expands the latest deliveries", async () => {
        render(<WebhookSettings />);
        await screen.findByText(URL);

        fireEvent.click(screen.getByRole("button", { name: `Deliveries for ${URL}` }));

        expect(await screen.findByText("42")).toBeInTheDocument();
        expect(screen.getByText("35 ms")).toBeInTheDocument();
    });

    it("hides the tab from non-admins", async () => {
        const { unmount } = render(
            <SettingsDialog
                open
                onOpenChange={() => undefined}
                user={{ name: "Viewer", email: "viewer@example.test", role: "viewer" }}
            />,
        );
        const nav = await screen.findByRole("navigation", { name: "Settings sections" });
        expect(within(nav).queryByRole("button", { name: "Webhooks" })).not.toBeInTheDocument();
        unmount();

        render(
            <SettingsDialog
                open
                onOpenChange={() => undefined}
                user={{ name: "Admin", email: "admin@example.test", role: "admin" }}
            />,
        );
        const adminNav = await screen.findByRole("navigation", { name: "Settings sections" });
        expect(within(adminNav).getByRole("button", { name: "Webhooks" })).toBeInTheDocument();
    });
});
