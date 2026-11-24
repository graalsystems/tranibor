// SPDX-License-Identifier: Apache-2.0
/** What the console shows for a finding's type (ADR 0217): the title its workflow declares, else the name
 * the core or a plugin gives it. The name stays one hover away, since it is what the API and the CLI speak. */
export function findingTypeLabel(finding: { type: string; type_title?: string | null }): string {
  return finding.type_title?.trim() ? finding.type_title : finding.type;
}
