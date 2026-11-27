// SPDX-License-Identifier: Apache-2.0
/**
 * A phone paired from the computer (ADR 0228 §4): what the phone's `/pair` page reads from its address, and how
 * long a pairing link the computer shows has left.
 */

/** A pairing code as the API draws it: base64url, 43 characters for 256 bits; anything else is not one. */
const PAIRING_CODE = /^[A-Za-z0-9_-]{16,256}$/;

/** The code the fragment carries (`#<code>`, as the computer's QR code writes it), or `""`. */
export function pairingCodeIn(hash: string): string {
  const value = hash.startsWith("#") ? hash.slice(1) : hash;
  return PAIRING_CODE.test(value) ? value : "";
}

/** Whole seconds left before `expiresAt`, never below zero. */
export function secondsLeft(expiresAt: string, now: number = Date.now()): number {
  return Math.max(0, Math.floor((Date.parse(expiresAt) - now) / 1000));
}

/** `4:05`: minutes and seconds, as the countdown under the QR code shows them. */
export function countdown(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}
