import { describe, expect, it, vi } from "vitest";

import { applyViewState, type ViewStateElement } from "./view-state-apply";
import type { EcadNetRef } from "@/types/ecad-viewer";

const UUID = "9f0c2f6a-1f3e-4a2b-8c4d-0123456789ab";

function fakeElement() {
    const calls: string[] = [];
    const element: ViewStateElement = {
        crossProbe: vi.fn(async (reference: string) => {
            calls.push(`crossProbe:${reference}`);
            return null;
        }),
        focusItem: vi.fn(async (uuid: string) => {
            calls.push(`focusItem:${uuid}`);
            return null;
        }),
        setHighlightedNets: vi.fn((nets: readonly EcadNetRef[]) => {
            calls.push(`nets:${nets.map((net) => net.name).join("|")}`);
            return { applied: [], unresolved: [] };
        }),
        focusBBox: vi.fn(async (x, y, width, height) => {
            calls.push(`bbox:${x},${y},${width},${height}`);
            return null;
        }),
        showPage: vi.fn(async (page: string) => {
            calls.push(`page:${page}`);
        }),
        setInsetMode: vi.fn((on: boolean) => {
            calls.push(`insets:${on}`);
        }),
    };
    return { calls, element };
}

describe("applying a view state to an element", () => {
    it("applies sheet, selection, nets and camera in that order", async () => {
        const { calls, element } = fakeElement();

        await applyViewState(element, {
            insets: true,
            page: "Subsheets/power.kicad_sch",
            sel: ["R1", "U3", UUID],
            nets: ["GND", "/Power/VBUS"],
            bbox: [1, 2, 3, 4],
        });

        // Reversed so the first listed item (R1) gets the camera last.
        expect(calls).toEqual([
            "insets:true",
            "page:Subsheets/power.kicad_sch",
            `focusItem:${UUID}`,
            "crossProbe:U3",
            "crossProbe:R1",
            "nets:GND|/Power/VBUS",
            "bbox:1,2,3,4",
        ]);
    });

    it("is a no-op without an element or without state", async () => {
        const { calls, element } = fakeElement();

        await expect(applyViewState(null, { sel: ["R1"] })).resolves.toBeUndefined();
        await applyViewState(element, {});
        expect(calls).toEqual([]);
    });

    it("ignores calls an older viewer does not implement", async () => {
        await expect(applyViewState({}, {
            page: "root.kicad_sch",
            sel: ["R1", UUID],
            nets: ["GND"],
            bbox: [0, 0, 1, 1],
            insets: true,
        })).resolves.toBeUndefined();
    });

    it("keeps applying after a viewer refuses a value", async () => {
        const { calls, element } = fakeElement();
        element.crossProbe = vi.fn(async () => { throw new Error("not found"); });
        element.focusBBox = vi.fn(async () => { throw new Error("no camera"); });

        await applyViewState(element, { sel: ["R1"], nets: ["GND"], bbox: [0, 0, 1, 1] });

        expect(calls).toEqual(["nets:GND"]);
    });

    it("switches page when the viewer exposes only switchPage", async () => {
        const switchPage = vi.fn();
        await applyViewState({ switchPage }, { page: "Subsheets/power.kicad_sch" });
        expect(switchPage).toHaveBeenCalledWith("Subsheets/power.kicad_sch");
    });
});
