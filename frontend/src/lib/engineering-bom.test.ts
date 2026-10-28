/**
 * The engineering BOM is one computation with one fixture (ADR §3.8).
 *
 * `fixtures/bom/components.json` is read by this test and by the backend's
 * `test_project_bom.py`; `expected.json` is the output both must reproduce.
 * Regenerate it with `UPDATE_BOM_FIXTURE=1 npx vitest run src/lib/engineering-bom`
 * after an intended change.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { buildBom } from "./engineering-bom";
import type { SemanticComponent } from "@/types/prism-selection";

const FIXTURE_DIR = resolve(__dirname, "../../../fixtures/bom");

const components = JSON.parse(
    readFileSync(resolve(FIXTURE_DIR, "components.json"), "utf8"),
) as SemanticComponent[];

const actual = {
    all: buildBom(components, "all"),
    assembly: buildBom(components, "assembly"),
};

describe("engineering BOM fixture", () => {
    it("matches the shared fixture for every assembly filter", () => {
        const expectedPath = resolve(FIXTURE_DIR, "expected.json");
        if (process.env.UPDATE_BOM_FIXTURE === "1") {
            writeFileSync(expectedPath, `${JSON.stringify(actual, null, 2)}\n`);
        }
        expect(actual).toEqual(JSON.parse(readFileSync(expectedPath, "utf8")));
    });

    it("orders references naturally and keeps internal fields out of the columns", () => {
        expect(actual.all.lines.find((line) => line.key === "value:10k")?.references)
            .toEqual(["R1", "R2", "R3", "R10"]);
        expect(actual.all.columns).toContain("Tolerance");
        expect(actual.all.columns).not.toContain("_internal_note");
        expect(actual.all.columns).not.toContain("kicad_unit");
    });
});
