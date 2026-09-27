import { useEffect, type RefObject } from 'react';

function inViewport(el: HTMLElement): boolean {
  const r = el.getBoundingClientRect();
  return r.top >= 0 && r.bottom <= window.innerHeight;
}

/**
 * Moves keyboard / screen-reader focus to `ref` whenever `key` changes (route
 * changes, patient switch). Targets are headings with tabIndex -1: styles.css
 * draws no ring on them (they are not controls), and the page does not jump
 * when the heading is already on screen.
 */
export function useFocusOnChange(ref: RefObject<HTMLElement | null>, key: unknown): void {
  useEffect(() => {
    const el = ref.current;
    if (el) el.focus({ preventScroll: inViewport(el) });
  }, [ref, key]);
}
