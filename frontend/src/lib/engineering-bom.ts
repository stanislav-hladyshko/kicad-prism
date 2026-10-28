/**
 * The engineering BOM's computation (ADR scripting-and-mcp §3.8).
 *
 * A BOM line is one value: every component that shares a value becomes a line
 * carrying its references, quantity and common fields. The table renders this
 * model; the server computes the same one in
 * `backend/app/services/design_query/bom.py`. `fixtures/bom/` is the shared
 * fixture both sides are held to, so an API client and the UI agree on what a
 * line is.
 */

import {
    filterComponentsForAssembly,
    type BomAssemblyFilter,
} from "@/components/design-variants/assembly-filter";
import type { SemanticComponent } from "@/types/prism-selection";

export const BOM_REQUIRED_COLUMNS = [
    "Reference",
    "Qty",
    "Value",
    "DNP",
    "In BOM",
    "Description",
    "Datasheet",
    "Manufacturer",
    "Manufacturer Part Number",
    "Vendor",
    "Vendor Part Number",
    "Footprint",
    "Mass (g)",
    "RQjC (C/W)",
    "RQjC_top (C/W)",
    "Temp_max (C)",
    "Temp_min (C)",
    "Power Dissipation (W)",
    "Rate",
] as const;

export interface BomLine {
    key: string;
    quantity: number;
    references: string[];
    fields: Record<string, string>;
}

export interface Bom {
    columns: string[];
    lines: BomLine[];
}

/** Natural order: numeric runs compare as numbers, so R2 precedes R10. */
export const naturalReferenceSort = (left: SemanticComponent, right: SemanticComponent) =>
    left.reference.localeCompare(right.reference, undefined, { numeric: true, sensitivity: "base" });

const fieldText = (value: string | number | boolean | null | undefined): string => {
    if (value === null || value === undefined) return "";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    return String(value);
};

export const componentFields = (component: SemanticComponent): Record<string, string> => ({
    Value: component.value || "",
    Footprint: component.footprint || "",
    ...Object.fromEntries(
        Object.entries(component.fields || {}).map(([key, value]) => [key, fieldText(value)]),
    ),
});

export const isInternalField = (field: string) => {
    const normalized = field.toLocaleLowerCase();
    return normalized.startsWith("_") || normalized.startsWith("kicad_");
};

export const commonFields = (components: SemanticComponent[]): Record<string, string> => {
    const rows = components.map(componentFields);
    const keys = new Set(rows.flatMap((row) => Object.keys(row)));
    return Object.fromEntries([...keys].map((key) => {
        const values = [...new Set(rows.map((row) => row[key] || ""))];
        return [key, values.length === 1 ? values[0] : "Mixed"];
    }));
};

/** Required columns, then the components' own fields in name order. */
export const bomColumns = (components: readonly SemanticComponent[]): string[] => {
    const required = new Set<string>(BOM_REQUIRED_COLUMNS);
    const extras = new Set<string>();
    for (const component of components) {
        for (const field of Object.keys(component.fields || {})) {
            if (!required.has(field) && !isInternalField(field)) extras.add(field);
        }
    }
    return [...BOM_REQUIRED_COLUMNS, ...[...extras].sort((left, right) => left.localeCompare(right))];
};

export const buildBom = (
    components: readonly SemanticComponent[],
    assemblyFilter: BomAssemblyFilter,
): Bom => {
    const filteredComponents = filterComponentsForAssembly(components, assemblyFilter);
    const byValue = new Map<string, SemanticComponent[]>();
    for (const component of filteredComponents) {
        const fields = componentFields(component);
        const valueKey = fields.Value.trim().toLocaleLowerCase();
        const group = byValue.get(valueKey) || [];
        group.push(component);
        byValue.set(valueKey, group);
    }
    const lines = [...byValue.entries()]
        .map(([key, group]): BomLine => {
            const sorted = [...group].sort(naturalReferenceSort);
            return {
                key: `value:${key}`,
                quantity: sorted.length,
                references: sorted.map((component) => component.reference),
                fields: commonFields(sorted),
            };
        })
        .sort((left, right) =>
            (left.fields.Value || "").localeCompare(right.fields.Value || "", undefined, {
                numeric: true,
                sensitivity: "base",
            }),
        );
    return { columns: bomColumns(components), lines };
};
