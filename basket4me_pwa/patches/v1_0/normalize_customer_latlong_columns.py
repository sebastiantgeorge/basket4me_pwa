"""Normalize existing Customer.custom_latitude / custom_longitude data
BEFORE the fixture-driven ALTER TABLE fires.

Two things break the ALTER without this:
  1. Existing rows hold values with > 6 decimal places (e.g.
     9.70982084879203) — MariaDB strict mode refuses to truncate.
  2. Some rows have NULL — the new fixture says NOT NULL DEFAULT 0.0,
     and strict mode refuses the implicit NULL → 0 conversion.

Both surface as the same error:
  MySQLdb.DataError: (1265, "Data truncated for column 'custom_latitude' at row 1")

This patch:
  - Sets NULL values to 0
  - Rounds all values to 6 decimals
Then the post_model_sync fixture import's ALTER MODIFY decimal(21,6) NOT
NULL DEFAULT 0.0 succeeds.

Runs in pre_model_sync (before fixture import). Idempotent: re-running
is a no-op once data is normalized. has_column-guarded so first-time
installs (no column yet) skip cleanly.
"""

import frappe


def execute():
    for col in ("custom_latitude", "custom_longitude"):
        if not frappe.db.has_column("Customer", col):
            continue
        try:
            # COALESCE handles NULL → 0; ROUND handles >6-decimal precision.
            # Run unconditionally on every row so the column is uniformly
            # populated before the NOT NULL constraint is applied.
            frappe.db.sql(
                f"UPDATE `tabCustomer` SET `{col}` = COALESCE(ROUND(`{col}`, 6), 0)"
            )
            frappe.db.commit()
            print(f"Normalized `tabCustomer`.{col} (NULL→0, rounded to 6 decimals).")
        except Exception as e:
            print(f"Skipped normalizing {col}: {e}")
