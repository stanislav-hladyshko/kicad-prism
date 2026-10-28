import { readProjectViewerState } from "@/lib/project-viewer-state";
import {
    VIEW_STATE_KEYS,
    buildViewStateParams,
    parseViewState,
    type ViewState,
} from "@/lib/view-state-url";
import type { PrismScriptApi } from "@/types/prism";

/**
 * The page connection `window.prism` needs: the current router search and
 * path, and a push navigation. React Router supplies them from the page.
 */
export interface PrismScriptApiHooks {
    search(): URLSearchParams;
    pathname(): string;
    navigate(url: string): void | Promise<void>;
}

/**
 * The current view. The URL owns the section, tab and commit; the viewer owns
 * the selection, highlighted nets and inset mode where its API exposes them.
 * A missing element or an API the vendored bundle does not have leaves the
 * URL's value alone rather than inventing one.
 */
function currentState(search: URLSearchParams): ViewState {
    const state = parseViewState(search);
    const live = readProjectViewerState();
    if (!live) return state;

    const selection = live.selection;
    if (selection && selection.kind !== "net") {
        state.sel = [selection.reference];
    } else {
        delete state.sel;
    }

    const elementNets = live.pcb?.getHighlightedNets?.();
    const nets = elementNets?.length
        ? elementNets.flatMap((net) => (net.name ? [net.name] : []))
        : live.highlightedNets.map((net) => net.netName);
    if (nets.length) state.nets = nets;
    else delete state.nets;

    const active = live.tab === "sch" ? live.sch : live.tab === "pcb" ? live.pcb : null;
    if (active && typeof active.insetMode === "boolean") {
        if (active.insetMode) state.insets = true;
        else delete state.insets;
    }

    return state;
}

/** Replace the view-state keys, keeping every unrelated query key (branch, variant, …). */
function withViewState(search: URLSearchParams, state: ViewState): URLSearchParams {
    const next = new URLSearchParams(search);
    for (const key of VIEW_STATE_KEYS) next.delete(key);
    buildViewStateParams(state).forEach((value, key) => next.set(key, value));
    return next;
}

export function createPrismScriptApi(
    projectId: string,
    hooks: PrismScriptApiHooks,
): PrismScriptApi {
    const stateOf = (partial: Partial<ViewState> = {}): ViewState => ({
        ...currentState(hooks.search()),
        ...partial,
    });

    return {
        version: "v1",
        projectId,
        getState: () => currentState(hooks.search()),
        open: async (partial) => {
            const params = withViewState(hooks.search(), stateOf(partial)).toString();
            await hooks.navigate(`${hooks.pathname()}${params ? `?${params}` : ""}`);
        },
        link: (partial = {}) => {
            const params = withViewState(hooks.search(), stateOf(partial)).toString();
            return `${window.location.origin}${hooks.pathname()}${params ? `?${params}` : ""}`;
        },
    };
}

/** Installs the API on `window` and returns the function that removes it again. */
export function installPrismScriptApi(
    projectId: string,
    hooks: PrismScriptApiHooks,
): () => void {
    const scriptApi = createPrismScriptApi(projectId, hooks);
    window.prism = scriptApi;
    return () => {
        if (window.prism === scriptApi) delete window.prism;
    };
}
