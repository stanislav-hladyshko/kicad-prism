/**
 * The URL grammar for a project view: the selected parts, highlighted nets,
 * schematic sheet, framed region and inset mode. One module owns the spelling
 * so a link Prism writes and a link Prism reads cannot drift apart.
 *
 * List items are encoded individually and joined with a literal comma, so a
 * net or reference that itself contains a comma survives. The joined value is
 * then encoded once more by URLSearchParams when it reaches the URL, which is
 * why `parseViewState` decodes each item after splitting.
 */

export type ViewStateBBox = [number, number, number, number];

export interface ViewState {
    section?: string;
    tab?: string;
    commit?: string;
    /** Component references or element uuids; the first is the focused one. */
    sel?: string[];
    /** Board net names. */
    nets?: string[];
    /** A schematic sheet path. */
    page?: string;
    /** Board millimetres: x, y, width, height. */
    bbox?: ViewStateBBox;
    insets?: boolean;
}

export const VIEW_STATE_KEYS = [
    "section",
    "tab",
    "commit",
    "sel",
    "nets",
    "page",
    "bbox",
    "insets",
] as const;

type ViewStateKey = (typeof VIEW_STATE_KEYS)[number];

const textParam = (params: URLSearchParams, key: ViewStateKey): string | undefined => {
    const value = params.get(key);
    return value ? value : undefined;
};

/** An item that cannot be decoded makes the whole parameter unusable. */
function parseList(raw: string | null): string[] | undefined {
    if (raw === null) return undefined;
    const items: string[] = [];
    for (const part of raw.split(",")) {
        try {
            const value = decodeURIComponent(part).trim();
            if (value) items.push(value);
        } catch {
            return undefined;
        }
    }
    return items.length ? items : undefined;
}

function parseBBox(raw: string | null): ViewStateBBox | undefined {
    if (raw === null) return undefined;
    const values = raw.split(",").map((part) => Number(part.trim()));
    if (values.length !== 4 || values.some((value) => !Number.isFinite(value))) return undefined;
    const [x, y, width, height] = values;
    if (width <= 0 || height <= 0) return undefined;
    return [x, y, width, height];
}

/** Only the listed view-state parameters; absent or unusable values are left out. */
export function parseViewState(searchParams: URLSearchParams): ViewState {
    const state: ViewState = {};
    const section = textParam(searchParams, "section");
    if (section) state.section = section;
    const tab = textParam(searchParams, "tab");
    if (tab) state.tab = tab;
    const commit = textParam(searchParams, "commit");
    if (commit) state.commit = commit;
    const sel = parseList(searchParams.get("sel"));
    if (sel) state.sel = sel;
    const nets = parseList(searchParams.get("nets"));
    if (nets) state.nets = nets;
    const page = textParam(searchParams, "page");
    if (page) state.page = page;
    const bbox = parseBBox(searchParams.get("bbox"));
    if (bbox) state.bbox = bbox;
    if (searchParams.get("insets") === "1") state.insets = true;
    return state;
}

const encodeList = (items: readonly string[]): string =>
    items.map((item) => encodeURIComponent(item)).join(",");

/** The canonical query parameters for one view; empty values are omitted. */
export function buildViewStateParams(state: ViewState): URLSearchParams {
    const params = new URLSearchParams();
    if (state.section) params.set("section", state.section);
    if (state.tab) params.set("tab", state.tab);
    if (state.commit) params.set("commit", state.commit);
    if (state.sel?.length) params.set("sel", encodeList(state.sel));
    if (state.nets?.length) params.set("nets", encodeList(state.nets));
    if (state.page) params.set("page", state.page);
    if (state.bbox) params.set("bbox", state.bbox.join(","));
    if (state.insets) params.set("insets", "1");
    return params;
}
