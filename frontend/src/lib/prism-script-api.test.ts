import { afterEach, describe, expect, it, vi } from "vitest";

import { registerProjectViewerState } from "./project-viewer-state";
import { createPrismScriptApi, installPrismScriptApi } from "./prism-script-api";
import { parseViewState } from "./view-state-url";
import type { ECadViewerElement } from "@/types/ecad-viewer";

const element = (state: Partial<ECadViewerElement>) => state as ECadViewerElement;

afterEach(() => {
    delete window.prism;
});

describe("window.prism", () => {
    it("installs on window and removes only itself", () => {
        const remove = installPrismScriptApi("p1", {
            search: () => new URLSearchParams(),
            pathname: () => "/project/p1",
            navigate: vi.fn(),
        });

        expect(window.prism?.version).toBe("v1");
        expect(window.prism?.projectId).toBe("p1");
        remove();
        expect(window.prism).toBeUndefined();
    });

    it("reads the section, tab and commit from the URL and the rest from the viewer", () => {
        const unregister = registerProjectViewerState(() => ({
            tab: "sch",
            selection: { kind: "component", reference: "R1", sourceContext: "SCH" },
            highlightedNets: [],
            sch: element({ insetMode: true }),
            pcb: element({ getHighlightedNets: () => [{ name: "GND", netCode: 1 }] }),
        }));

        const api = createPrismScriptApi("p1", {
            search: () => new URLSearchParams("section=visualizers&tab=sch&commit=abc"),
            pathname: () => "/project/p1",
            navigate: vi.fn(),
        });

        expect(api.getState()).toEqual({
            section: "visualizers",
            tab: "sch",
            commit: "abc",
            sel: ["R1"],
            nets: ["GND"],
            insets: true,
        });
        unregister();
    });

    it("omits a net selection, cleared nets and off insets", () => {
        const unregister = registerProjectViewerState(() => ({
            tab: "pcb",
            selection: { kind: "net", netName: "GND", sourceContext: "PCB" },
            highlightedNets: [],
            sch: element({ insetMode: true }),
            pcb: element({ insetMode: false, getHighlightedNets: () => [] }),
        }));

        const api = createPrismScriptApi("p1", {
            search: () => new URLSearchParams("section=visualizers&tab=pcb&sel=R1&nets=GND&insets=1"),
            pathname: () => "/project/p1",
            navigate: vi.fn(),
        });

        expect(api.getState()).toEqual({ section: "visualizers", tab: "pcb" });
        unregister();
    });

    it("builds an absolute link, keeping unrelated query keys", () => {
        const api = createPrismScriptApi("p1", {
            search: () => new URLSearchParams("branch=feature/x&section=history&tab=sch"),
            pathname: () => "/project/p1",
            navigate: vi.fn(),
        });

        const link = api.link({ section: "visualizers", sel: ["R1", "U3"], bbox: [1, 2, 3, 4], insets: true });

        expect(link).toBe(`${window.location.origin}/project/p1?branch=feature%2Fx&section=visualizers&tab=sch&sel=R1%2CU3&bbox=1%2C2%2C3%2C4&insets=1`);
        expect(parseViewState(new URL(link, window.location.origin).searchParams).sel).toEqual(["R1", "U3"]);
    });

    it("opens state with a push navigation on the project path", async () => {
        const navigate = vi.fn();
        const api = createPrismScriptApi("p1", {
            search: () => new URLSearchParams("section=visualizers&branch=main"),
            pathname: () => "/project/p1",
            navigate,
        });

        await api.open({ tab: "pcb", nets: ["GND"] });

        expect(navigate).toHaveBeenCalledWith(
            "/project/p1?branch=main&section=visualizers&tab=pcb&nets=GND",
        );
    });
});
