import frappe


def execute():
    """Remove all orphan references to non-existent `custom_unpaid` field
    that cause MySQL Error 1054 'Unknown column custom_unpaid in SELECT'.
    """
    fieldname = "custom_unpaid"

    # 1) Custom Field
    cf_rows = frappe.db.sql(
        """
        SELECT name FROM `tabCustom Field`
        WHERE fieldname=%s
           OR fetch_from LIKE %s
           OR depends_on LIKE %s
        """,
        (fieldname, f"%{fieldname}%", f"%{fieldname}%"),
        as_dict=True,
    )
    for r in cf_rows:
        frappe.delete_doc("Custom Field", r["name"], ignore_permissions=True, force=True)
        print(f"Deleted Custom Field: {r['name']}")

    # 2) Property Setter
    ps_rows = frappe.db.sql(
        """
        SELECT name FROM `tabProperty Setter`
        WHERE field_name=%s OR value LIKE %s
        """,
        (fieldname, f"%{fieldname}%"),
        as_dict=True,
    )
    for r in ps_rows:
        frappe.delete_doc("Property Setter", r["name"], ignore_permissions=True, force=True)
        print(f"Deleted Property Setter: {r['name']}")

    # 3) DocField (orphan child rows on Sales Invoice DocType)
    df_rows = frappe.db.sql(
        """
        SELECT name, parent FROM `tabDocField`
        WHERE fieldname=%s AND parent='Sales Invoice'
        """,
        (fieldname,),
        as_dict=True,
    )
    for r in df_rows:
        frappe.db.delete("DocField", {"name": r["name"]})
        print(f"Deleted DocField row: {r['name']} from {r['parent']}")

    # 4) Server Scripts referencing it (warn only — manual edit)
    ss_rows = frappe.db.sql(
        "SELECT name FROM `tabServer Script` WHERE script LIKE %s",
        (f"%{fieldname}%",),
        as_dict=True,
    )
    for r in ss_rows:
        print(f"WARNING: Server Script '{r['name']}' references {fieldname} — manual fix needed.")

    # 5) Client Scripts referencing it (warn only)
    cs_rows = frappe.db.sql(
        "SELECT name FROM `tabClient Script` WHERE script LIKE %s",
        (f"%{fieldname}%",),
        as_dict=True,
    )
    for r in cs_rows:
        print(f"WARNING: Client Script '{r['name']}' references {fieldname} — manual fix needed.")

    frappe.db.commit()
    frappe.clear_cache(doctype="Sales Invoice")
    print(f"Cleanup of '{fieldname}' references complete.")
