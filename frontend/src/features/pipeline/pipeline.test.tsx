import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { pipelineStages, pipelineSummary, type PipelineCell, type ProjectPipeline } from "./pipeline-api";
import { PipelineBar } from "./pipeline-bar";
import { PipelinePanel } from "./pipeline-panel";

const passed: PipelineCell = { state: "ok", text: "так", why: "verify.json: drc_clean PASS" };

function boardPipeline(stop: string, sixth: PipelineCell): ProjectPipeline {
    return {
        asOf: "2026-10-26",
        columns: ["1. Вимоги", "2. Datasheet", "3. Схема", "4. ERC", "5. Паритет", "6. Розміщення", "Еталон"],
        row: {
            id: "G4", dir: "examples/golden/G4", project_id: "prj_g4", board: "USB-UART", kind: "клас 4",
            stop,
            cells: [passed, passed, passed, passed, passed, sixth, { state: "ok", text: "так", why: "compare.json" }],
            human: { state: "none", text: "-", why: "релізу в Prism ще немає" },
        },
    };
}

const stopped = boardPipeline("6. Розміщення", { state: "bad", text: "немає", why: "у verify.json немає placement_rationale" });

describe("pipeline stages", () => {
    it("reads only the numbered columns as process stages", () => {
        expect(pipelineStages(stopped).map((stage) => stage.number)).toEqual([1, 2, 3, 4, 5, 6]);
    });

    it("sums a stopped board as its stage out of all, and keeps the repository's word otherwise", () => {
        expect(pipelineSummary(stopped, 6)).toBe("6/6 · Розміщення");
        expect(pipelineSummary(boardPipeline("не почато", passed), 6)).toBe("не почато");
    });
});

describe("PipelineBar", () => {
    it("states the stop in text on a card, not only in colour", () => {
        render(<PipelineBar pipeline={stopped} />);
        expect(screen.getByRole("img", { name: "Pipeline: 6/6 · Розміщення" })).toBeInTheDocument();
        expect(screen.getByText("6/6 · Розміщення")).toBeInTheDocument();
        expect(screen.getByTitle("6. Розміщення: немає")).toHaveClass("bg-destructive");
    });

    it("opens the clicked stage in the header", () => {
        const onSelectStage = vi.fn();
        render(<PipelineBar pipeline={stopped} onSelectStage={onSelectStage} />);
        const stages = screen.getByRole("group", { name: "Pipeline stages" });
        expect(within(stages).getAllByRole("button")).toHaveLength(6);
        fireEvent.click(within(stages).getByRole("button", { name: "6. Розміщення: немає" }));
        expect(onSelectStage).toHaveBeenCalledWith(6);
    });

    it("draws nothing when the file has no numbered stages", () => {
        const { container } = render(<PipelineBar pipeline={{ ...stopped, columns: ["Еталон"] }} />);
        expect(container).toBeEmptyDOMElement();
    });
});

describe("PipelinePanel", () => {
    it("gives every stage its reason, then the checks beside the process and the sign-off", () => {
        render(<PipelinePanel pipeline={stopped} focusStage={null} />);
        const stage = screen.getByText("6. Розміщення").closest("li");
        expect(stage).toHaveClass("bg-destructive/10");
        expect(within(stage as HTMLElement).getByText("у verify.json немає placement_rationale")).toBeInTheDocument();
        expect(screen.getByText("Еталон").closest("li")).toHaveTextContent("compare.json");
        expect(screen.getByText("Sign-off").closest("li")).toHaveTextContent("релізу в Prism ще немає");
    });

    it("scrolls to and marks the stage a header mark was clicked on", () => {
        // jsdom has no layout, so no scrollIntoView
        const scrollIntoView = vi.fn();
        Element.prototype.scrollIntoView = scrollIntoView;
        render(<PipelinePanel pipeline={stopped} focusStage={3} />);
        expect(scrollIntoView).toHaveBeenCalledTimes(1);
        expect(screen.getByText("3. Схема").closest("li")).toHaveClass("ring-2");
        expect(screen.getByText("6. Розміщення").closest("li")).not.toHaveClass("ring-2");
    });
});
