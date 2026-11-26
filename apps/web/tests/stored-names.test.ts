import { beforeEach, describe, expect, it } from "vitest";
import { SCRIPT_DES_PREFERENCES, THEME_KEY } from "@/lib/preferences";
import { MIGRATION_SCRIPT, migrateStorage, storedName } from "@/lib/stored-names";

/** A browser that used the console before 0.30.0 (ADR 0226, R9c): every key under the former prefix. */
function seed(storage: Storage): void {
  storage.setItem("choregos.theme", "light");
  storage.setItem("choregos.draft.p.w", "draft");
  storage.setItem("choregos.org", "acme");
  storage.setItem("tranibor.org", "globex"); // a choice already made under the new name wins
  storage.setItem("other", "kept");
}

function entries(storage: Storage): Record<string, string | null> {
  return Object.fromEntries(Array.from({ length: storage.length }, (_, i) => storage.key(i)!).sort().map((k) => [k, storage.getItem(k)]));
}

const MOVED = { other: "kept", "tranibor.draft.p.w": "draft", "tranibor.org": "globex", "tranibor.theme": "light" };

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("the console's stored names", () => {
  it("writes the new prefix", () => {
    expect(storedName("theme")).toBe("tranibor.theme");
    expect(THEME_KEY).toBe("tranibor.theme");
  });

  it("moves every former key to its new name, the new one kept when both exist", () => {
    seed(window.localStorage);
    migrateStorage(window.localStorage);
    expect(entries(window.localStorage)).toEqual(MOVED);
    migrateStorage(window.localStorage); // twice: nothing more moves
    expect(entries(window.localStorage)).toEqual(MOVED);
  });

  it("the head script applies the same rule, to both storages, before the first paint", () => {
    seed(window.localStorage);
    seed(window.sessionStorage);
    new Function(MIGRATION_SCRIPT)();
    expect(entries(window.localStorage)).toEqual(MOVED);
    expect(entries(window.sessionStorage)).toEqual(MOVED);
  });

  it("a theme chosen before the upgrade still applies on the first paint", () => {
    window.localStorage.setItem("choregos.theme", "light");
    new Function(SCRIPT_DES_PREFERENCES)();
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(window.localStorage.getItem("tranibor.theme")).toBe("light");
    expect(window.localStorage.getItem("choregos.theme")).toBeNull();
  });
});
