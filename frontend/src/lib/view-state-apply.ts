import type { EcadNetRef } from "@/types/ecad-viewer";

import type { ViewState } from "./view-state-url";

/**
 * The part of the `<ecad-viewer>` API that a URL's view state needs. The
 * element is vendored and most of its API is optional, so every call is a
 * feature check and nothing here throws for an older bundle.
 */
export interface ViewStateElement {
    crossProbe?(reference: string): Promise<unknown>;
    focusItem?(uuid: string, options?: { select?: boolean }): Promise<unknown>;
    setHighlightedNets?(nets: readonly EcadNetRef[]): unknown;
    focusBBox?(x: number, y: number, width: number, height: number): Promise<unknown>;
    showPage?(page: string): Promise<void>;
    switchPage?(page: string): void;
    setInsetMode?(on: boolean): void;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const quiet = async (run: () => Promise<unknown> | unknown): Promise<void> => {
    try {
        await run();
    } catch {
        // A value the viewer cannot resolve is ignored, like every other
        // invalid value in this grammar.
    }
};

/**
 * Apply one view to one element. Order matters: the sheet first, then the
 * selected items, then the nets, and the framed region last so it wins the
 * camera. Selected items are applied in reverse so the first listed item is
 * focused last (and therefore is the one the camera settles on).
 */
export async function applyViewState(
    element: ViewStateElement | null,
    state: ViewState,
): Promise<void> {
    if (!element) return;

    if (state.insets) element.setInsetMode?.(true);

    const page = state.page;
    if (page) {
        if (element.showPage) await quiet(() => element.showPage?.(page));
        else element.switchPage?.(page);
    }

    const selected = state.sel ?? [];
    const selectItem = (item: string): Promise<void> => (
        UUID.test(item)
            ? quiet(() => element.focusItem?.(item, { select: true }))
            : quiet(() => element.crossProbe?.(item))
    );
    // Chained rather than awaited in a loop so the order is explicit: the
    // first listed item is selected last and so owns the camera.
    await [...selected].reverse().reduce<Promise<void>>(
        (previous, item) => previous.then(() => selectItem(item)),
        Promise.resolve(),
    );

    const nets = state.nets;
    if (nets?.length) {
        await quiet(() => element.setHighlightedNets?.(nets.map((name) => ({ name }))));
    }

    if (state.bbox) {
        const [x, y, width, height] = state.bbox;
        await quiet(() => element.focusBBox?.(x, y, width, height));
    }
}
