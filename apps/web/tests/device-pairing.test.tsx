import { createHash } from "node:crypto";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DevicesPage from "@/app/account/devices/page";
import PairPage from "@/app/pair/page";
import { api, ApiError, isSignInPage } from "@/lib/api";
import { countdown, pairingCodeIn, secondsLeft } from "@/lib/device-pairing";
import { encodeQr, qrPath, versionFor } from "@/lib/qr";

/**
 * A phone paired from the computer (ADR 0228 §4, S87-13). My account › Devices, on the computer, asks for a
 * pairing and shows its link as a QR code; the phone's `/pair` page reads the code from the fragment, takes it
 * out of the address and posts it once. The QR encoder has no dependency: it is held to what a reference
 * encoder (python-qrcode 8, byte mode, same version, level and mask) writes, module for module.
 */
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, api: { ...real.api, createMyDevicePairing: vi.fn(), exchangeDevicePairing: vi.fn() } };
});

const CODE = "dGhpcy1pcy1hLXBhaXJpbmctY29kZS1vZi00My1jaGFy";
const URL_OF_PAIRING = `https://me.relay.example/pair#${CODE}`;

function at(address: string) {
  const url = new URL(address, "https://me.relay.example");
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

function rows(modules: boolean[][]): string[] {
  return modules.map((row) => row.map((dark) => (dark ? "1" : "0")).join(""));
}

function digest(modules: boolean[][]): string {
  return createHash("sha256").update(rows(modules).join("\n")).digest("hex");
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the QR encoder", () => {
  it("writes what a reference encoder writes, for a version 1 symbol", () => {
    // python-qrcode 8.2: QRCode(version=1, error_correction=L, border=0, mask_pattern=0), "hello" in byte mode.
    expect(rows(encodeQr("hello", { level: "L", mask: 0 }))).toEqual([
      "111111100101101111111",
      "100000100111001000001",
      "101110101101101011101",
      "101110100101001011101",
      "101110100010101011101",
      "100000100000101000001",
      "111111101010101111111",
      "000000001101100000000",
      "111011111111011000100",
      "011111011100001000011",
      "011111101000100011111",
      "001100000000001000010",
      "000010110110101010000",
      "000000001101010100111",
      "111111101111011100111",
      "100000101111110110000",
      "101110101111011100011",
      "101110100010001100110",
      "101110101110100010101",
      "100000101100001010010",
      "111111101010101100011",
    ]);
  });

  it.each([
    ["https://me.relay.example/pair#abc", "M", 2, 3, "fa416393f25b27885d5ff8ddbfcd2eb18b00e89b4ab953b2bb6cbf3a5cd0d585"],
    [
      `https://a1b2c3d4e5.relay.choregos.example/pair#${"x".repeat(43)}`,
      "M",
      5,
      6,
      "5d8b6cb41749993091f9b7c5a350acca5384adc111975ef0bbf32b92dfbd9bb2",
    ],
    // Version 13: the version information, three alignment patterns a side, two block sizes interleaved.
    [`https://example.org/${"a".repeat(200)}`, "Q", 7, 13, "c10ef84a5ba847021c296228e207e8355aa8606a5e069a32ddc26c22ed48f55b"],
    // Multi-byte UTF-8 in byte mode.
    ["Δ ✓ 日本", "H", 3, 2, "017a0f9d18495bb4c60bb44523838d9c16e90965c45da02171e19e9c0a3151ac"],
  ] as const)("matches the reference for %s at level %s, mask %i", (text, level, mask, version, expected) => {
    const modules = encodeQr(text, { level, mask });
    expect(modules).toHaveLength(version * 4 + 17);
    expect(digest(modules)).toBe(expected);
  });

  it("picks the smallest version that holds the text, and refuses what none holds", () => {
    expect(versionFor(14, "M")).toBe(1);
    expect(versionFor(15, "M")).toBe(2);
    // A pairing link to a relay host of 40 characters: a version 6 symbol, 41 modules a side.
    expect(versionFor(90, "M")).toBe(6);
    expect(versionFor(2331, "M")).toBe(40);
    expect(() => versionFor(2332, "M")).toThrow(/at most 2331 bytes/);
  });

  it("chooses a mask by itself and draws one square per dark module, inside the quiet zone", () => {
    const modules = encodeQr(URL_OF_PAIRING);
    const dark = modules.flat().filter(Boolean).length;
    const path = qrPath(modules);
    expect(path.match(/M/g)).toHaveLength(dark);
    // The top-left finder pattern starts at the quiet zone.
    expect(path.startsWith("M4 4h1v1h-1z")).toBe(true);
  });
});

describe("My account › Devices, on the computer", () => {
  beforeEach(() => {
    vi.mocked(api.createMyDevicePairing).mockReset();
  });

  it("shows the pairing link as a QR code, and never as text", async () => {
    vi.mocked(api.createMyDevicePairing).mockResolvedValue({
      url: URL_OF_PAIRING,
      expires_at: new Date(Date.now() + 300_000).toISOString(),
    });
    render(<DevicesPage />);
    fireEvent.click(screen.getByRole("button", { name: "Show a pairing code" }));
    const code = await screen.findByRole("img", { name: /Pairing code/ });
    expect(code.querySelector("path")?.getAttribute("d")).toBe(qrPath(encodeQr(URL_OF_PAIRING)));
    expect(screen.getByRole("status").textContent).toMatch(/Valid for 4:5\d, for one phone/);
    expect(document.body.textContent).not.toContain(CODE);
    expect(api.createMyDevicePairing).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Show a new pairing code" })).toBeTruthy();
  });

  it("hides the code once it has expired", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    vi.mocked(api.createMyDevicePairing).mockResolvedValue({
      url: URL_OF_PAIRING,
      expires_at: new Date(Date.now() + 2_000).toISOString(),
    });
    render(<DevicesPage />);
    fireEvent.click(screen.getByRole("button", { name: "Show a pairing code" }));
    await screen.findByRole("img", { name: /Pairing code/ });
    await act(async () => {
      vi.advanceTimersByTime(3_000);
    });
    expect(screen.queryByRole("img", { name: /Pairing code/ })).toBeNull();
    expect(screen.getByText(/has expired/)).toBeTruthy();
  });

  it.each([
    [new ApiError(401, { title: "Authentication too old", errors: [{ error: "step_up_required" }] } as never), /choregos local open/],
    [new ApiError(404, { title: "Not Found" }), /from the computer the installation runs on/],
    [new ApiError(409, { title: "Conflict", detail: "no relay" }), /choregos local relay enable/],
    [new Error("network down"), /network down/],
  ])("says what a refusal means: %s", async (refusal, said) => {
    vi.mocked(api.createMyDevicePairing).mockRejectedValue(refusal);
    render(<DevicesPage />);
    fireEvent.click(screen.getByRole("button", { name: "Show a pairing code" }));
    expect(await screen.findByText(said)).toBeTruthy();
    expect(screen.queryByRole("img", { name: /Pairing code/ })).toBeNull();
  });
});

describe("the phone's pairing page", () => {
  beforeEach(() => {
    vi.mocked(api.exchangeDevicePairing).mockReset().mockResolvedValue(undefined);
  });

  it("posts the code of the fragment once, takes it out of the address, and goes home", async () => {
    const { replace, forget } = at(`/pair#${CODE}`);
    render(
      <StrictMode>
        <PairPage />
      </StrictMode>,
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("https://me.relay.example/"));
    expect(api.exchangeDevicePairing).toHaveBeenCalledOnce();
    expect(api.exchangeDevicePairing).toHaveBeenCalledWith(CODE);
    expect(forget).toHaveBeenCalledWith(window.history.state, "", "/pair");
  });

  it("says a used or expired code is one, and does not go on", async () => {
    vi.mocked(api.exchangeDevicePairing).mockRejectedValue(new ApiError(401, { title: "Not authenticated" }));
    const { replace } = at(`/pair#${CODE}`);
    render(<PairPage />);
    expect(await screen.findByText(/has expired or was already used/)).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });

  it("says a pairing code is opened through the relay", async () => {
    vi.mocked(api.exchangeDevicePairing).mockRejectedValue(new ApiError(404, { title: "Not Found" }));
    at(`/pair#${CODE}`);
    render(<PairPage />);
    expect(await screen.findByText(/opened on the phone, through the relay/)).toBeTruthy();
  });

  it("posts nothing without a code, and shows what else fails", async () => {
    at("/pair#no");
    render(<PairPage />);
    expect(await screen.findByText(/needs the link/)).toBeTruthy();
    expect(api.exchangeDevicePairing).not.toHaveBeenCalled();
  });

  it("shows an unexpected failure as it is", async () => {
    vi.mocked(api.exchangeDevicePairing).mockRejectedValue(new Error("relay unreachable"));
    at(`/pair#${CODE}`);
    render(<PairPage />);
    expect(await screen.findByText("relay unreachable")).toBeTruthy();
  });

  it("is a sign-in page: a 401 elsewhere does not send it to /login before it has posted", () => {
    expect(isSignInPage("/pair")).toBe(true);
    expect(isSignInPage("/pair/")).toBe(true);
    expect(isSignInPage("/pairs")).toBe(false);
    expect(isSignInPage("/auth/owner")).toBe(true);
  });

  it("reads only a code shaped as the API draws it", () => {
    expect(pairingCodeIn(`#${CODE}`)).toBe(CODE);
    expect(pairingCodeIn(CODE)).toBe(CODE);
    expect(pairingCodeIn("#short")).toBe("");
    expect(pairingCodeIn("#has/a/slash/and-is-long-enough")).toBe("");
    expect(pairingCodeIn("")).toBe("");
    expect(secondsLeft("2026-11-27T10:05:00Z", Date.parse("2026-11-27T10:00:00Z"))).toBe(300);
    expect(secondsLeft("2026-11-27T10:00:00Z", Date.parse("2026-11-27T10:05:00Z"))).toBe(0);
    expect(countdown(299)).toBe("4:59");
    expect(countdown(5)).toBe("0:05");
  });
});

describe("the pairing's two calls", () => {
  it("posts to /me/device-pairings and to /auth/device/session, and leaves a refusal to the page", async () => {
    const { api: real } = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ url: URL_OF_PAIRING, expires_at: "2026-11-27T10:05:00Z" }), { status: 201 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ title: "Authentication too old", errors: [{ error: "step_up_required" }] }), {
          status: 401,
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ title: "Not authenticated" }), { status: 401 }));
    vi.stubGlobal("fetch", fetch);
    const { replace } = at("/account/devices");
    await expect(real.createMyDevicePairing()).resolves.toEqual({
      url: URL_OF_PAIRING,
      expires_at: "2026-11-27T10:05:00Z",
    });
    await expect(real.createMyDevicePairing()).rejects.toMatchObject({ status: 401 });
    await expect(real.exchangeDevicePairing(CODE)).resolves.toBeUndefined();
    await expect(real.exchangeDevicePairing(CODE)).rejects.toMatchObject({ status: 401 });
    const calls = fetch.mock.calls as [string, RequestInit][];
    expect(calls.map(([url, init]) => [url, init.method])).toEqual([
      ["/api/v1/me/device-pairings", "POST"],
      ["/api/v1/me/device-pairings", "POST"],
      ["/api/v1/auth/device/session", "POST"],
      ["/api/v1/auth/device/session", "POST"],
    ]);
    expect(calls[2]?.[1]).toMatchObject({ credentials: "include", body: JSON.stringify({ code: CODE }) });
    // A step-up here is the owner key's to answer: the page is not sent to the identity provider.
    expect(replace).not.toHaveBeenCalled();
  });
});
