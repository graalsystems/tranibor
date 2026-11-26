// SPDX-License-Identifier: Apache-2.0
/**
 * The names the console keeps in the browser's storage (ADR 0226, round R9c): `tranibor.<name>`, formerly
 * `choregos.<name>`. A browser that used the console before 0.30.0 holds the former keys (the theme, the
 * sidebar, the organisation, a workflow draft, the procedures seen, a frame's token...): they are moved to the
 * new names before the first paint, by the head script (`MIGRATION_SCRIPT`, run by `SCRIPT_DES_PREFERENCES`),
 * so every reader reads the new key only. When both exist, the new one wins and the former is dropped. The
 * former prefix is removed with this module in 0.32.0.
 */

export const STORAGE_PREFIX = "tranibor.";
export const FORMER_STORAGE_PREFIX = "choregos.";

/** `tranibor.<name>`: the key the console writes. */
export function storedName(name: string): string {
  return STORAGE_PREFIX + name;
}

type Store = Pick<Storage, "length" | "key" | "getItem" | "setItem" | "removeItem">;

/** Moves every `choregos.*` entry of `storage` to its `tranibor.*` name, the new one kept when both exist. */
export function migrateStorage(storage: Store): void {
  const former: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key !== null && key.startsWith(FORMER_STORAGE_PREFIX)) former.push(key);
  }
  for (const key of former) {
    const target = STORAGE_PREFIX + key.slice(FORMER_STORAGE_PREFIX.length);
    const value = storage.getItem(key);
    if (storage.getItem(target) === null && value !== null) storage.setItem(target, value);
    storage.removeItem(key);
  }
}

/**
 * The SAME rule as `migrateStorage`, written to run alone before React, on `localStorage` and `sessionStorage`.
 * A refused storage (strict private browsing) is skipped without an error. `tests/stored-names.test.ts` runs it
 * and compares it with `migrateStorage`.
 */
export const MIGRATION_SCRIPT = `(function(){var o=${JSON.stringify(FORMER_STORAGE_PREFIX)},n=${JSON.stringify(
  STORAGE_PREFIX,
)};function m(s){var f=[],i,k,t,v;for(i=0;i<s.length;i++){k=s.key(i);if(k!==null&&k.indexOf(o)===0)f.push(k);}for(i=0;i<f.length;i++){k=f[i];t=n+k.slice(o.length);v=s.getItem(k);if(s.getItem(t)===null&&v!==null)s.setItem(t,v);s.removeItem(k);}}try{m(localStorage);}catch(e){}try{m(sessionStorage);}catch(e){}})();`;
