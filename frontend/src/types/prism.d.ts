import type { ViewState } from "@/lib/view-state-url";

/**
 * `window.prism`: the project page's view-state script API (MCP-21).
 *
 *   prism.link({ tab: "pcb", sel: ["R1"], nets: ["GND"] })
 *   await prism.open({ bbox: [10, 20, 30, 40] })
 *   prism.getState()
 *
 * It is installed while a project page is mounted. `getState` reads the URL for
 * the section, tab and commit and the live viewer for the selection,
 * highlighted nets and inset mode, omitting what the viewer cannot report.
 * `open` navigates with the router (a push, so Back returns to the view the
 * caller left); the URL grammar then applies the state to the viewers.
 */
export interface PrismScriptApi {
    readonly version: "v1";
    readonly projectId: string;
    getState(): ViewState;
    open(state: Partial<ViewState>): Promise<void>;
    /** An absolute link; with no argument, the state currently on screen. */
    link(state?: Partial<ViewState>): string;
}

declare global {
    interface Window {
        prism?: PrismScriptApi;
    }
}
