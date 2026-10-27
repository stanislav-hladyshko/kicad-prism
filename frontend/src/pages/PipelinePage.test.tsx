import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ fetchJson: vi.fn() }));
vi.mock("@/lib/api", () => ({
    fetchJson: (...args: unknown[]) => mocks.fetchJson(...args),
}));

import { PipelinePage, type Pipeline } from "./PipelinePage";

const PIPELINE: Pipeline = {
    repo_id: "repo_1",
    repo_name: "pcb-agent-research",
    as_of: "2026-10-26",
    columns: ["1. Вимоги", "Еталон"],
    rows: [
        {
            id: "G4", dir: "examples/golden/G4", project_id: "prj_g4", board: "USB-UART", kind: "клас 4",
            stop: "6. Розміщення",
            cells: [{ state: "ok", text: "так", why: "verify.json" }, { state: "bad", text: "ні", why: "немає compare.json" }],
            human: { state: "ok", text: "підписано 2026-10-21", why: "Designer і QA" },
        },
        {
            id: "G9", dir: null, project_id: null, board: "HackRF One", kind: "клас 8", stop: "не почато",
            cells: [{ state: "none", text: "-", why: "" }, { state: "none", text: "-", why: "" }],
            human: { state: "none", text: "-", why: "плати ще немає" },
        },
    ],
};

function renderPage() {
    return render(<MemoryRouter><PipelinePage /></MemoryRouter>);
}

beforeEach(() => {
    mocks.fetchJson.mockReset();
});

describe("PipelinePage", () => {
    it("shows every board with its stages and links the imported ones to their project", async () => {
        mocks.fetchJson.mockResolvedValue({ pipelines: [PIPELINE] });
        renderPage();
        expect(await screen.findByRole("link", { name: "G4" })).toHaveAttribute("href", "/project/prj_g4");
        expect(screen.getByRole("rowheader", { name: /G9/ })).not.toContainElement(screen.queryByRole("link", { name: "G9" }));
        expect(screen.getByRole("columnheader", { name: "Еталон" })).toBeInTheDocument();
        expect(screen.getByText("ні")).toHaveAttribute("title", "немає compare.json");
        expect(screen.getByText("підписано 2026-10-21")).toBeInTheDocument();
        expect(mocks.fetchJson).toHaveBeenCalledWith("/api/workspace/pipeline", undefined, "Could not load the pipeline");
    });

    it("says when no repository commits the pipeline file", async () => {
        mocks.fetchJson.mockResolvedValue({ pipelines: [] });
        renderPage();
        expect(await screen.findByText(/No repository commits/)).toBeInTheDocument();
    });

    it("shows the error instead of an empty table", async () => {
        mocks.fetchJson.mockRejectedValue(new Error("Could not load the pipeline"));
        renderPage();
        expect(await screen.findByText("Could not load the pipeline")).toBeInTheDocument();
        expect(screen.queryByRole("table")).not.toBeInTheDocument();
    });
});
