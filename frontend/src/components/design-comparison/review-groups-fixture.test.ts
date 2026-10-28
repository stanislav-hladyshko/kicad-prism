/**
 * Review groups are one computation with one fixture (ADR §3.8).
 *
 * `fixtures/review-groups/input.json` is a synthetic comparison result; it is
 * read here and by the backend's `test_design_compare_review.py`. `expected.json`
 * is the review queue both must reproduce, with no comments attached.
 * Regenerate it with
 * `UPDATE_REVIEW_FIXTURE=1 npx vitest run src/components/design-comparison/review-groups-fixture`
 * after an intended change.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { createGroupingContext, groupChanges } from "./comparison-review-groups";
import {
    prepareChangesForReview,
    semanticNetRenames,
} from "./comparison-review-noise";
import {
    groupSummary,
    queueSection,
    reviewImpactForGroup,
} from "./comparison-review-queue";
import type { DesignCompareResultA1 } from "./design-compare-result";

const FIXTURE_DIR = resolve(__dirname, "../../../../fixtures/review-groups");

const input = JSON.parse(
    readFileSync(resolve(FIXTURE_DIR, "input.json"), "utf8"),
) as DesignCompareResultA1;

/**
 * The queue the workspace builds for one domain: the prepared changes, their
 * grouping context and the BOM, in the review order `groupChanges` returns.
 */
function reviewQueue(
    changes: DesignCompareResultA1["schematic"]["changes"],
    bom: DesignCompareResultA1["bom"],
) {
    return groupChanges(
        changes,
        [],
        createGroupingContext(changes, bom),
    ).map((group) => ({
        id: group.id,
        kind: group.kind,
        title: group.label,
        summary: groupSummary(group),
        impact: reviewImpactForGroup(group),
        section: queueSection(group),
        changeIds: group.changes.map((change) => change.id).sort(),
    }));
}

const schematicReview = prepareChangesForReview(input.schematic.changes);
// The board's rewritten net references follow the semantic rename the
// schematic declares, exactly as the workspace passes it.
const pcbReview = prepareChangesForReview(input.pcb.changes, {
    netRenames: semanticNetRenames(input.schematic.changes),
});

const actual = {
    sch: reviewQueue(schematicReview.changes, input.bom),
    pcb: reviewQueue(pcbReview.changes, input.bom),
};

describe("review group fixture", () => {
    it("matches the shared fixture for both domains", () => {
        const expectedPath = resolve(FIXTURE_DIR, "expected.json");
        if (process.env.UPDATE_REVIEW_FIXTURE === "1") {
            writeFileSync(expectedPath, `${JSON.stringify(actual, null, 2)}\n`);
        }
        expect(actual).toEqual(JSON.parse(readFileSync(expectedPath, "utf8")));
    });
});
