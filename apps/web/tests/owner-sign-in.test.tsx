import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import OwnerSignInPage from "@/app/auth/owner/page";
import LoginPage from "@/app/login/page";
import { ServedThemeProvider } from "@/components/served-theme";
import { api, ApiError, isSignInPage } from "@/lib/api";
import { nextPath, ticketIn } from "@/lib/owner-sign-in";
import { DEFAULT_SERVED_THEME } from "@/lib/served-theme";
import type { AuthMethods } from "@/lib/types";

/**
 * A personal installation's sign-in (ADR 0228 §4, S87-12): the owner key signs its owner in on the computer.
 * `choregos local open` opens `/auth/owner#<ticket>`; the page posts the ticket once and goes on. The sign-in
 * page asks `/auth/methods` and, in the personal posture, shows that way in instead of an identity provider.
 */
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, api: { ...real.api, signInAsOwner: vi.fn(), authMethods: vi.fn() } };
});

const TICKET = "eyJhbGciOiJFZERTQSJ9.eyJhdWQiOiJjaG9yZWdvcy1vd25lciJ9.c2lnbmF0dXJl";

function at(address: string) {
  const url = new URL(address, "http://127.0.0.1:7878");
  const replace = vi.fn();
  vi.stubGlobal("location", {
    pathname: url.pathname,
    search: url.search,
    hash: url.hash,
    origin: url.origin,
    replace,
    assign: vi.fn(),
  });
  const forget = vi.spyOn(window.history, "replaceState").mockImplementation(() => undefined);
  return { replace, forget };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the owner's sign-in page", () => {
  beforeEach(() => {
    vi.mocked(api.signInAsOwner).mockReset().mockResolvedValue(undefined);
  });

  it("posts the ticket of the fragment once, takes it out of the address, and goes on", async () => {
    const { replace, forget } = at(`/auth/owner?next=/inbox#${TICKET}`);
    render(
      <StrictMode>
        <OwnerSignInPage />
      </StrictMode>,
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("http://127.0.0.1:7878/inbox"));
    expect(api.signInAsOwner).toHaveBeenCalledOnce();
    expect(api.signInAsOwner).toHaveBeenCalledWith(TICKET);
    // The address keeps no ticket: neither a reload nor the history holds it.
    expect(forget).toHaveBeenCalledWith(window.history.state, "", "/auth/owner?next=/inbox");
  });

  it("says a used or expired link is one, and does not go on", async () => {
    vi.mocked(api.signInAsOwner).mockRejectedValue(new ApiError(401, { title: "Not authenticated" }));
    const { replace } = at(`/auth/owner#${TICKET}`);
    render(<OwnerSignInPage />);
    expect(await screen.findByText(/has expired or was already used/)).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });

  it("says the owner key signs in on the computer only, through the relay", async () => {
    vi.mocked(api.signInAsOwner).mockRejectedValue(new ApiError(404, { title: "Not Found" }));
    at(`/auth/owner#${TICKET}`);
    render(<OwnerSignInPage />);
    expect(await screen.findByText(/only on the computer the installation runs on/)).toBeTruthy();
  });

  it("posts nothing without a ticket", async () => {
    at("/auth/owner#not-a-ticket");
    render(<OwnerSignInPage />);
    expect(await screen.findByText(/needs the link/)).toBeTruthy();
    expect(api.signInAsOwner).not.toHaveBeenCalled();
  });

  it("goes home when next hides another host in what the URL parser strips", async () => {
    const { replace } = at(`/auth/owner?next=/%09/evil.example#${TICKET}`);
    render(<OwnerSignInPage />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("http://127.0.0.1:7878/"));
  });

  it("goes nowhere but a path of the console", () => {
    expect(nextPath("?next=/p/demo")).toBe("/p/demo");
    expect(nextPath("?next=//evil.example")).toBe("/");
    expect(nextPath("?next=https://evil.example")).toBe("/");
    expect(nextPath("")).toBe("/");
    const origin = "http://127.0.0.1:7878";
    // The URL parser strips a tab, a line feed and a carriage return: each leaves `//x`, another host.
    for (const next of ["/%09/x", "/%0a/x", "/%0d/x", "/\t/x", "/%5C/x", "/\\x"]) {
      expect(nextPath(`?next=${next}`, origin), JSON.stringify(next)).toBe("/");
      expect(nextPath(`?next=${next}`), JSON.stringify(next)).toBe("/");
    }
    // A path that resolves to `//x` here would name a host once the page resolves it again.
    for (const next of ["/.//x", "/%2e//x", "/a/..//x"]) {
      expect(nextPath(`?next=${next}`, origin), JSON.stringify(next)).toBe("/");
    }
    expect(nextPath("?next=javascript:alert(1)", origin)).toBe("/");
    // What lands on this origin keeps its path, query and fragment.
    expect(nextPath("?next=/inbox%3Fq%3D1%23top", origin)).toBe("/inbox?q=1#top");
    expect(nextPath(`?next=${encodeURIComponent(`${origin}/p/demo`)}`, origin)).toBe("/p/demo");
    expect(ticketIn(`#${TICKET}`)).toBe(TICKET);
    expect(ticketIn("#a.b")).toBe("");
    expect(ticketIn("")).toBe("");
  });

  it("is a sign-in page: a 401 elsewhere does not send it to /login", () => {
    expect(isSignInPage("/auth/owner")).toBe(true);
    expect(isSignInPage("/login")).toBe(true);
    expect(isSignInPage("/auth/ownership")).toBe(false);
    expect(isSignInPage("/projects")).toBe(false);
  });
});

describe("the owner ticket's exchange", () => {
  it("posts the ticket to /auth/owner and leaves a refusal to the page", async () => {
    const { api: real } = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ title: "Not authenticated", detail: "not valid" }), { status: 401 }),
    );
    vi.stubGlobal("fetch", fetch);
    const { replace } = at("/auth/owner");
    await expect(real.signInAsOwner(TICKET)).rejects.toMatchObject({ status: 401 });
    const [url, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/auth/owner");
    expect(init).toMatchObject({ method: "POST", credentials: "include", body: JSON.stringify({ ticket: TICKET }) });
    expect(replace).not.toHaveBeenCalled();
  });
});

describe("the sign-in page of a personal installation", () => {
  function show(methods: AuthMethods) {
    vi.mocked(api.authMethods).mockReset().mockResolvedValue(methods);
    render(
      <ServedThemeProvider initial={DEFAULT_SERVED_THEME} hostOrg={null}>
        <LoginPage />
      </ServedThemeProvider>,
    );
  }

  it("shows the owner's way in on the computer, and no identity provider", async () => {
    show({ posture: "personal", door: "loopback", methods: ["owner"] });
    expect(await screen.findByText(/it signs you in with/)).toBeTruthy();
    expect(screen.getByText("choregos local open")).toBeTruthy();
    expect(screen.queryByLabelText("Work e-mail")).toBeNull();
  });

  it("says the relay has no way in yet", async () => {
    show({ posture: "personal", door: "relay", methods: [] });
    expect(await screen.findByText(/through the relay is not available yet/)).toBeTruthy();
    expect(screen.queryByLabelText("Work e-mail")).toBeNull();
  });

  it("says an installation without an owner key needs one", async () => {
    show({ posture: "personal", door: "loopback", methods: [] });
    expect(await screen.findByText(/no owner key yet/)).toBeTruthy();
  });

  it("keeps an organisation's identity provider", async () => {
    show({ posture: "organisation", door: null, methods: ["identity_provider"] });
    await waitFor(() => expect(api.authMethods).toHaveBeenCalled());
    expect(screen.getByLabelText("Work e-mail")).toBeTruthy();
    expect(screen.queryByText("choregos local open")).toBeNull();
  });
});
