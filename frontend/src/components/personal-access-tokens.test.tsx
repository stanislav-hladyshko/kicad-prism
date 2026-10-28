import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", () => ({
    toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }),
}));

const fetchApi = vi.fn();
vi.mock("@/lib/api", () => ({
    fetchApi: (...args: unknown[]) => fetchApi(...args),
    readApiError: vi.fn(async () => "boom"),
}));

import { PersonalAccessTokens, type PersonalAccessToken } from "./personal-access-tokens";

const TOKEN: PersonalAccessToken = {
    id: "pat_0123456789abcdef",
    user_email: "owner@example.com",
    name: "ci-runner",
    scopes: ["api:read", "design:write"],
    prefix: "prism_pat_ab",
    created_at: "2026-01-01T00:00:00+00:00",
    expires_at: "2026-04-01T00:00:00+00:00",
    last_used_at: "2026-02-01T12:00:00+00:00",
    revoked_at: null,
};

const SECRET = "prism_pat_abcdefghijklmnopqrstuvwxyz0123456789ABCD";

function jsonResponse(body: unknown, ok = true) {
    return { ok, json: async () => body } as unknown as Response;
}

let now = 0;
let frameCallbacks: FrameRequestCallback[] = [];

function advance(ms: number) {
    now += ms;
    act(() => {
        const pending = frameCallbacks;
        frameCallbacks = [];
        pending.forEach((callback) => callback(now));
    });
}

function pointerDown(element: Element) {
    act(() => {
        element.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    });
}

beforeEach(() => {
    fetchApi.mockReset();
    now = 0;
    frameCallbacks = [];
    vi.spyOn(performance, "now").mockImplementation(() => now);
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
        frameCallbacks.push(callback);
        return frameCallbacks.length;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {
        frameCallbacks = [];
    });
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

describe("PersonalAccessTokens", () => {
    it("lists the caller's tokens", async () => {
        fetchApi.mockResolvedValue(jsonResponse([TOKEN]));

        render(<PersonalAccessTokens />);

        expect(await screen.findByText("ci-runner")).toBeInTheDocument();
        expect(screen.getByText(/prism_pat_ab/)).toBeInTheDocument();
        expect(screen.getByText("api:read design:write")).toBeInTheDocument();
        expect(screen.getByRole("button", { name: "Revoke token ci-runner" })).toBeInTheDocument();
    });

    it("shows the new token once after creating it", async () => {
        fetchApi.mockImplementation((url: string, init?: RequestInit) => {
            if (url === "/api/auth/tokens" && init?.method === "POST") {
                return Promise.resolve(jsonResponse({ ...TOKEN, token: SECRET }));
            }
            return Promise.resolve(jsonResponse([]));
        });

        render(<PersonalAccessTokens />);
        await screen.findByText("No personal access tokens.");

        fireEvent.change(screen.getByLabelText("Token name"), { target: { value: "ci-runner" } });
        fireEvent.click(screen.getByRole("button", { name: "Create" }));

        await waitFor(() => expect(screen.getByDisplayValue(SECRET)).toBeInTheDocument());
        expect(screen.getByDisplayValue(SECRET)).toHaveAttribute("readonly");
        expect(fetchApi).toHaveBeenCalledWith(
            "/api/auth/tokens",
            expect.objectContaining({ method: "POST" }),
        );
    });

    it("revokes a token after the hold confirms", async () => {
        fetchApi.mockImplementation((url: string, init?: RequestInit) => {
            if (url === "/api/auth/tokens" && init?.method === "DELETE") {
                return Promise.resolve(jsonResponse(null));
            }
            return Promise.resolve(jsonResponse([TOKEN]));
        });

        render(<PersonalAccessTokens />);
        fireEvent.click(await screen.findByRole("button", { name: "Revoke token ci-runner" }));

        const dialog = await screen.findByRole("dialog");
        pointerDown(within(dialog).getByRole("button", { name: /Hold to revoke/ }));
        advance(1000);

        await waitFor(() =>
            expect(fetchApi).toHaveBeenCalledWith(
                "/api/auth/tokens/pat_0123456789abcdef",
                expect.objectContaining({ method: "DELETE" }),
            ),
        );
    });
});
