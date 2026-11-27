// SPDX-License-Identifier: Apache-2.0
/**
 * A small QR code encoder (ISO/IEC 18004), with no dependency: what My account › Devices draws a pairing link
 * with (ADR 0228 §4). Byte mode only, error correction level M by default, versions 1 to 40, the mask chosen by
 * the standard's penalty rules. A link of a hundred characters is a version 6 or 7 symbol.
 *
 * `encodeQr` returns the symbol as rows of booleans (`true` is a dark module), without the quiet zone;
 * `qrPath` turns it into the `d` of one SVG path, a square per dark module, offset by the quiet zone.
 */

export type ErrorCorrection = "L" | "M" | "Q" | "H";

/** The two bits the format information carries for each level. */
const FORMAT_BITS: Record<ErrorCorrection, number> = { L: 1, M: 0, Q: 3, H: 2 };

/** Error correction codewords per block, by level and version (index 0 unused). */
const ECC_PER_BLOCK: Record<ErrorCorrection, readonly number[]> = {
  L: [0, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
  M: [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28],
  Q: [0, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
  H: [0, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
};

/** Error correction blocks, by level and version (index 0 unused). */
const BLOCKS: Record<ErrorCorrection, readonly number[]> = {
  L: [0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25],
  M: [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49],
  Q: [0, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68],
  H: [0, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81],
};

/** The quiet zone the standard asks around a symbol, in modules. */
export const QUIET_ZONE = 4;

/** The highest version, and the number of masks. */
const LAST_VERSION = 40;
const MASK_COUNT = 8;

/** One value of a table or a list, which is there by construction: an index out of range is a bug, said so. */
function at(values: readonly number[], index: number): number {
  const value = values[index];
  if (value === undefined) throw new RangeError(`no value at ${index}`);
  return value;
}

/** The modules of a version that carry data and error correction: all but the function patterns. */
function rawDataModules(version: number): number {
  let result = (16 * version + 128) * version + 64;
  if (version >= 2) {
    const alignments = Math.floor(version / 7) + 2;
    result -= (25 * alignments - 10) * alignments - 55;
    if (version >= 7) result -= 36;
  }
  return result;
}

function eccPerBlock(version: number, level: ErrorCorrection): number {
  return at(ECC_PER_BLOCK[level], version);
}

function blockCount(version: number, level: ErrorCorrection): number {
  return at(BLOCKS[level], version);
}

function dataCodewords(version: number, level: ErrorCorrection): number {
  return Math.floor(rawDataModules(version) / 8) - eccPerBlock(version, level) * blockCount(version, level);
}

/** The bytes a version holds in byte mode: its data bits less the mode and the character count. */
function byteCapacity(version: number, level: ErrorCorrection): number {
  return Math.floor((dataCodewords(version, level) * 8 - 4 - (version <= 9 ? 8 : 16)) / 8);
}

/** The centres of the alignment patterns, on each axis. */
function alignmentPositions(version: number): number[] {
  if (version === 1) return [];
  const count = Math.floor(version / 7) + 2;
  const step = Math.floor((version * 8 + count * 3 + 5) / (count * 4 - 4)) * 2;
  const positions = [6];
  for (let position = version * 4 + 10; positions.length < count; position -= step) positions.splice(1, 0, position);
  return positions;
}

/** Multiplication in GF(2^8) modulo x^8 + x^4 + x^3 + x^2 + 1. */
function multiply(x: number, y: number): number {
  let z = 0;
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((y >>> i) & 1) * x;
  }
  return z & 0xff;
}

/** The Reed-Solomon generator polynomial of a degree, highest coefficient (always 1) left out. */
function generator(degree: number): number[] {
  const result = new Array<number>(degree).fill(0);
  result[degree - 1] = 1;
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < degree; j++) {
      const next = j + 1 < degree ? at(result, j + 1) : 0;
      result[j] = multiply(at(result, j), root) ^ next;
    }
    root = multiply(root, 0x02);
  }
  return result;
}

function remainder(data: readonly number[], divisor: readonly number[]): number[] {
  const result = new Array<number>(divisor.length).fill(0);
  for (const byte of data) {
    const factor = byte ^ (result.shift() ?? 0);
    result.push(0);
    divisor.forEach((coefficient, i) => {
      result[i] = at(result, i) ^ multiply(coefficient, factor);
    });
  }
  return result;
}

/** The data codewords of `bytes` in byte mode, terminated and padded to the version's capacity. */
function dataCodewordsOf(bytes: Uint8Array, version: number, level: ErrorCorrection): number[] {
  const bits: number[] = [];
  const append = (value: number, length: number) => {
    for (let i = length - 1; i >= 0; i--) bits.push((value >>> i) & 1);
  };
  append(0b0100, 4);
  append(bytes.length, version <= 9 ? 8 : 16);
  for (const byte of bytes) append(byte, 8);
  const capacity = dataCodewords(version, level) * 8;
  append(0, Math.min(4, capacity - bits.length));
  append(0, (8 - (bits.length % 8)) % 8);
  for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) append(pad, 8);
  const codewords: number[] = [];
  for (let i = 0; i < bits.length; i += 8) codewords.push(bits.slice(i, i + 8).reduce((byte, bit) => (byte << 1) | bit, 0));
  return codewords;
}

/** The data split in blocks, each followed by its error correction, interleaved as the symbol reads them. */
function withErrorCorrection(data: readonly number[], version: number, level: ErrorCorrection): number[] {
  const blocks = blockCount(version, level);
  const eccLength = eccPerBlock(version, level);
  const raw = Math.floor(rawDataModules(version) / 8);
  const shortBlocks = blocks - (raw % blocks);
  const shortLength = Math.floor(raw / blocks);
  const divisor = generator(eccLength);
  const written: number[][] = [];
  for (let i = 0, k = 0; i < blocks; i++) {
    const block = data.slice(k, k + shortLength - eccLength + (i < shortBlocks ? 0 : 1));
    k += block.length;
    const ecc = remainder(block, divisor);
    if (i < shortBlocks) block.push(0);
    written.push([...block, ...ecc]);
  }
  const result: number[] = [];
  for (let i = 0; i <= shortLength; i++) {
    written.forEach((block, j) => {
      // The padding byte of a short block is not part of the symbol.
      if (i !== shortLength - eccLength || j >= shortBlocks) result.push(at(block, i));
    });
  }
  return result;
}

/** Whether mask `mask` flips the module at column `x`, row `y`. */
function flips(mask: number, x: number, y: number): boolean {
  switch (mask) {
    case 0:
      return (x + y) % 2 === 0;
    case 1:
      return y % 2 === 0;
    case 2:
      return x % 3 === 0;
    case 3:
      return (x + y) % 3 === 0;
    case 4:
      return (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0;
    case 5:
      return ((x * y) % 2) + ((x * y) % 3) === 0;
    case 6:
      return (((x * y) % 2) + ((x * y) % 3)) % 2 === 0;
    default:
      return (((x + y) % 2) + ((x * y) % 3)) % 2 === 0;
  }
}

/** A symbol being drawn: its modules and which of them the function patterns hold, column `x`, row `y`. */
class QrSymbol {
  readonly version: number;
  readonly level: ErrorCorrection;
  readonly size: number;
  private readonly dark: boolean[];
  private readonly reserved: boolean[];

  constructor(version: number, level: ErrorCorrection) {
    this.version = version;
    this.level = level;
    this.size = version * 4 + 17;
    this.dark = new Array<boolean>(this.size * this.size).fill(false);
    this.reserved = new Array<boolean>(this.size * this.size).fill(false);
    this.drawFunctionPatterns();
  }

  isDark(x: number, y: number): boolean {
    return this.dark[y * this.size + x] === true;
  }

  private isReserved(x: number, y: number): boolean {
    return this.reserved[y * this.size + x] === true;
  }

  private set(x: number, y: number, dark: boolean): void {
    this.dark[y * this.size + x] = dark;
    this.reserved[y * this.size + x] = true;
  }

  rows(): boolean[][] {
    return Array.from({ length: this.size }, (_, y) => Array.from({ length: this.size }, (_, x) => this.isDark(x, y)));
  }

  private drawFunctionPatterns(): void {
    for (let i = 0; i < this.size; i++) {
      this.set(6, i, i % 2 === 0);
      this.set(i, 6, i % 2 === 0);
    }
    for (const [x, y] of [
      [3, 3],
      [this.size - 4, 3],
      [3, this.size - 4],
    ] as const) {
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const distance = Math.max(Math.abs(dx), Math.abs(dy));
          const [xx, yy] = [x + dx, y + dy];
          if (xx >= 0 && xx < this.size && yy >= 0 && yy < this.size) this.set(xx, yy, distance !== 2 && distance !== 4);
        }
      }
    }
    const positions = alignmentPositions(this.version);
    const last = positions.length - 1;
    positions.forEach((x, i) => {
      positions.forEach((y, j) => {
        // The three corners hold the finder patterns.
        if ((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0)) return;
        for (let dy = -2; dy <= 2; dy++) {
          for (let dx = -2; dx <= 2; dx++) this.set(x + dx, y + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
        }
      });
    });
    this.drawFormat(0); // reserves the format modules; drawn again once the mask is chosen
    this.drawVersion();
  }

  drawFormat(mask: number): void {
    const data = (FORMAT_BITS[this.level] << 3) | mask;
    let rem = data;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const bits = ((data << 10) | rem) ^ 0x5412;
    const bit = (i: number) => ((bits >>> i) & 1) === 1;
    for (let i = 0; i <= 5; i++) this.set(8, i, bit(i));
    this.set(8, 7, bit(6));
    this.set(8, 8, bit(7));
    this.set(7, 8, bit(8));
    for (let i = 9; i < 15; i++) this.set(14 - i, 8, bit(i));
    for (let i = 0; i < 8; i++) this.set(this.size - 1 - i, 8, bit(i));
    for (let i = 8; i < 15; i++) this.set(8, this.size - 15 + i, bit(i));
    this.set(8, this.size - 8, true); // the dark module
  }

  private drawVersion(): void {
    if (this.version < 7) return;
    let rem = this.version;
    for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
    const bits = (this.version << 12) | rem;
    for (let i = 0; i < 18; i++) {
      const dark = ((bits >>> i) & 1) === 1;
      const a = this.size - 11 + (i % 3);
      const b = Math.floor(i / 3);
      this.set(a, b, dark);
      this.set(b, a, dark);
    }
  }

  drawCodewords(codewords: readonly number[]): void {
    let i = 0;
    for (let right = this.size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5; // the vertical timing pattern
      for (let vertical = 0; vertical < this.size; vertical++) {
        for (let j = 0; j < 2; j++) {
          const x = right - j;
          const upward = ((right + 1) & 2) === 0;
          const y = upward ? this.size - 1 - vertical : vertical;
          if (!this.isReserved(x, y) && i < codewords.length * 8) {
            this.dark[y * this.size + x] = ((at(codewords, i >>> 3) >>> (7 - (i & 7))) & 1) === 1;
            i++;
          }
        }
      }
    }
  }

  applyMask(mask: number): void {
    for (let y = 0; y < this.size; y++) {
      for (let x = 0; x < this.size; x++) {
        if (!this.isReserved(x, y) && flips(mask, x, y)) this.dark[y * this.size + x] = !this.isDark(x, y);
      }
    }
  }

  /** The standard's penalty (N1 = 3, N2 = 3, N3 = 40, N4 = 10): the mask with the lowest wins. */
  penalty(): number {
    const size = this.size;
    let score = 0;
    const lines: boolean[][] = [];
    for (let i = 0; i < size; i++) {
      lines.push(Array.from({ length: size }, (_, x) => this.isDark(x, i)));
      lines.push(Array.from({ length: size }, (_, y) => this.isDark(i, y)));
    }
    const finderLike = [true, false, true, true, true, false, true];
    for (const line of lines) {
      const darkAt = (i: number) => line[i] === true;
      let run = 1;
      for (let i = 1; i <= size; i++) {
        if (i < size && darkAt(i) === darkAt(i - 1)) {
          run++;
          continue;
        }
        if (run >= 5) score += 3 + (run - 5);
        run = 1;
      }
      for (let i = 0; i + 7 <= size; i++) {
        if (!finderLike.every((dark, k) => darkAt(i + k) === dark)) continue;
        // Outside the symbol is light.
        if ([1, 2, 3, 4].every((k) => !darkAt(i - k))) score += 40;
        if ([0, 1, 2, 3].every((k) => !darkAt(i + 7 + k))) score += 40;
      }
    }
    let dark = 0;
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        if (this.isDark(x, y)) dark++;
        if (x + 1 < size && y + 1 < size) {
          const colour = this.isDark(x, y);
          if (colour === this.isDark(x + 1, y) && colour === this.isDark(x, y + 1) && colour === this.isDark(x + 1, y + 1))
            score += 3;
        }
      }
    }
    const total = size * size;
    score += Math.floor(Math.abs(dark * 20 - total * 10) / total) * 10;
    return score;
  }
}

/** The smallest version whose capacity holds `length` bytes in byte mode at `level`; an error past version 40. */
export function versionFor(length: number, level: ErrorCorrection = "M"): number {
  for (let version = 1; version <= LAST_VERSION; version++) {
    if (length <= byteCapacity(version, level)) return version;
  }
  throw new Error(`a QR code holds at most ${byteCapacity(LAST_VERSION, level)} bytes at level ${level}`);
}

/**
 * The QR code of `text` (UTF-8, byte mode): its modules as rows of booleans, `true` dark, without the quiet zone.
 * `mask` forces one of the eight masks; otherwise the one the penalty rules prefer.
 */
export function encodeQr(text: string, options: { level?: ErrorCorrection; mask?: number } = {}): boolean[][] {
  const level = options.level ?? "M";
  const bytes = new TextEncoder().encode(text);
  const version = versionFor(bytes.length, level);
  const codewords = withErrorCorrection(dataCodewordsOf(bytes, version, level), version, level);
  const symbol = new QrSymbol(version, level);
  symbol.drawCodewords(codewords);
  let chosen = options.mask ?? 0;
  if (options.mask === undefined) {
    let best = Number.POSITIVE_INFINITY;
    for (let candidate = 0; candidate < MASK_COUNT; candidate++) {
      symbol.applyMask(candidate);
      symbol.drawFormat(candidate);
      const score = symbol.penalty();
      if (score < best) [best, chosen] = [score, candidate];
      symbol.applyMask(candidate); // a mask is its own inverse
    }
  }
  symbol.applyMask(chosen);
  symbol.drawFormat(chosen);
  return symbol.rows();
}

/** One SVG path drawing the dark modules, each a unit square, offset by the quiet zone. */
export function qrPath(modules: readonly (readonly boolean[])[]): string {
  const parts: string[] = [];
  modules.forEach((row, y) => {
    row.forEach((dark, x) => {
      if (dark) parts.push(`M${x + QUIET_ZONE} ${y + QUIET_ZONE}h1v1h-1z`);
    });
  });
  return parts.join("");
}
