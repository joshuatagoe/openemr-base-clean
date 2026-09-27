// In-memory, expiring key-value store for pending logins and sessions.
// Single-process only: a restart signs everyone out (acceptable for a thin BFF
// with 1 h sessions; a shared store would be needed to run several replicas).

export interface Expiring {
  expiresAt: number;
}

export class ExpiringStore<T extends Expiring> {
  private readonly items = new Map<string, T>();

  constructor(
    private readonly clock: () => number,
    private readonly maxEntries: number,
  ) {}

  get(id: string): T | undefined {
    const item = this.items.get(id);
    if (!item) return undefined;
    if (item.expiresAt <= this.clock()) {
      this.items.delete(id);
      return undefined;
    }
    return item;
  }

  /** Returns and removes the item (single use). */
  take(id: string): T | undefined {
    const item = this.get(id);
    this.items.delete(id);
    return item;
  }

  set(id: string, item: T): void {
    if (this.items.size >= this.maxEntries) this.sweep();
    if (this.items.size >= this.maxEntries) {
      // Still full of live entries: drop the oldest insertion.
      const oldest = this.items.keys().next();
      if (!oldest.done) this.items.delete(oldest.value);
    }
    this.items.set(id, item);
  }

  delete(id: string): void {
    this.items.delete(id);
  }

  sweep(): void {
    const now = this.clock();
    for (const [id, item] of this.items) if (item.expiresAt <= now) this.items.delete(id);
  }

  get size(): number {
    return this.items.size;
  }
}

export interface PendingLogin extends Expiring {
  state: string;
  codeVerifier: string;
  nonce: string;
}

export interface Session extends Expiring {
  accessToken: string;
  displayName: string;
  fhirUser: string | undefined;
  scope: string;
}
