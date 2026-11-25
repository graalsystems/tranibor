// SPDX-License-Identifier: Apache-2.0
import { Badge } from "@/components/ui";

/** What the badge says on hover: where the row comes from, and what it is not. */
export const IMPORTED_TITLE =
  "Imported from a demonstration history: no agent ran it and nothing measured it; its cost counts in no budget and no agent metric.";

/**
 * The mark of a row a demonstration history import wrote (ADR 0216): a work item, a run, a finding, a
 * request in the inbox. The API serves the flag; wherever such a row is listed or opened, the console
 * says so, so a demonstration is never read as lived. Nothing when the row is not imported.
 */
export function ImportedBadge({ imported, className }: { imported?: boolean | null; className?: string }) {
  if (!imported) return null;
  return (
    <Badge tone="neutral" title={IMPORTED_TITLE} data-testid="imported-badge" className={className}>
      Imported
    </Badge>
  );
}
