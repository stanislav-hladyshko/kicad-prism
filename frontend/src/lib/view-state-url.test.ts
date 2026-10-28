import { describe, expect, it } from "vitest";

import { buildViewStateParams, parseViewState, type ViewState } from "./view-state-url";

const parse = (query: string) => parseViewState(new URLSearchParams(query));

describe("view state URL", () => {
    it("round trips a full state", () => {
        const state: ViewState = {
            section: "visualizers",
            tab: "pcb",
            commit: "0123456789abcdef",
            sel: ["R1", "U3"],
            nets: ["GND", "/Power/VBUS"],
            page: "Subsheets/power.kicad_sch",
            bbox: [1.5, -2, 30, 40],
            insets: true,
        };

        expect(parse(buildViewStateParams(state).toString())).toEqual(state);
    });

    it("encodes each list item before joining, so a comma stays in the item", () => {
        const params = buildViewStateParams({ nets: ["A,B", "/x/y"] });

        expect(params.get("nets")).toBe("A%2CB,%2Fx%2Fy");
        expect(parse(params.toString()).nets).toEqual(["A,B", "/x/y"]);
    });

    it("builds only the view keys it has", () => {
        const params = buildViewStateParams({ tab: "sch" });

        expect([...params.keys()]).toEqual(["tab"]);
    });

    it("ignores absent, empty and malformed values", () => {
        expect(parse("")).toEqual({});
        expect(parse("sel=&nets=&page=&insets=0")).toEqual({});
        expect(parse("insets=true")).toEqual({});
        expect(parse("bbox=1,2,3")).toEqual({});
        expect(parse("bbox=1,2,0,4")).toEqual({});
        expect(parse("bbox=1,2,3,nope")).toEqual({});
        expect(parse("nets=GND,%zz")).toEqual({});
    });

    it("accepts negative and fractional bbox numbers", () => {
        expect(parse("bbox=-1.25,2.5,3,4.75").bbox).toEqual([-1.25, 2.5, 3, 4.75]);
    });
});
