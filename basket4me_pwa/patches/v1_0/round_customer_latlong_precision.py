"""Round existing Customer.custom_latitude / custom_longitude values to 6
decimal places BEFORE the fixture-driven ALTER TABLE fires.

Without this, sync_fixtures crashes with:
  MySQLdb.DataError: (1265, "Data truncated for column 'custom_latitude' at row 1")
when MariaDB strict mode refuses to truncate existing high-precision data
(e.g. 9.70982084879203 → decimal(21,6)).

Runs in pre_model_sync so it executes BEFORE the post-model-sync fixture
import that triggers the schema ALTER.

Safe to run repeatedly: each invocation just rounds to 6 decimals.
"""

import frappe


def execute():
    for col in ("custom_latitude", "custom_longitude"):
        if frappe.db.has_column("Customer", col):
            try:
                frappe.db.sql(
                    f"UPDATE `tabCustomer` SET `{col}` = ROUND(`{col}`, 6) WHERE `{col}` IS NOT NULL"
                )
                frappe.db.commit()
                print(f"Rounded `tabCustomer`.{col} values to 6 decimals.")
            except Exception as e:
                print(f"Skipped rounding {col}: {e}")
