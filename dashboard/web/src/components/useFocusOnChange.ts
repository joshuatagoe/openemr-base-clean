import { useEffect, type RefObject } from 'react';

/** Moves keyboard / screen-reader focus to `ref` whenever `key` changes (route changes, patient switch). */
export function useFocusOnChange(ref: RefObject<HTMLElement | null>, key: unknown): void {
  useEffect(() => {
    ref.current?.focus();
  }, [ref, key]);
}
