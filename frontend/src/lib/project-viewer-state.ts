import type { ECadViewerElement } from "@/types/ecad-viewer";
import type { PrismSelection } from "@/types/prism-selection";

import type { HighlightedNet } from "./net-highlights";

/**
 * What `window.prism` can read from the project visualizer. The visualizer
 * registers one reader while it is mounted; the script API asks it at call
 * time, so a state read after a tab switch or a net highlight is current.
 *
 * The two elements are the same adapter-owned elements the visualizer drives,
 * not copies: `getHighlightedNets` and `insetMode` are read from them exactly
 * as the viewer reports them.
 */
export interface ProjectViewerSnapshot {
    tab: string;
    selection: PrismSelection | null;
    highlightedNets: readonly HighlightedNet[];
    sch: ECadViewerElement | null;
    pcb: ECadViewerElement | null;
}

type Reader = () => ProjectViewerSnapshot;

let reader: Reader | null = null;

/** Installs the project viewer's reader and returns the function that removes it. */
export function registerProjectViewerState(read: Reader): () => void {
    reader = read;
    return () => {
        if (reader === read) reader = null;
    };
}

/** Null when no project visualizer is mounted. */
export function readProjectViewerState(): ProjectViewerSnapshot | null {
    return reader ? reader() : null;
}
