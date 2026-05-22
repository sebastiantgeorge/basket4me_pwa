import frappe

def get_sales_person(user):
    user_name = frappe.db.get_value('User', user, 'name')
    sales_person = frappe.db.get_value('Sales Person', {'custom_user': user_name})
    return sales_person


def _get_user_companies(user):
    """Local import to avoid circular dep at module load."""
    try:
        from basket4me_pwa.api import get_user_companies
        return get_user_companies(user) or []
    except Exception:
        return []


def _company_in_clause(companies):
    """SQL fragment '(\'A\',\'B\')' with escaped quotes."""
    return "(" + ",".join("'" + c.replace("'", "''") + "'" for c in companies) + ")"


@frappe.whitelist()
def get_permission_query_conditions_for_invoice(user=None):
    view_all_role = frappe.get_value("Basket4Me Settings", None, 'view_all_transaction_role')

    if not user:
        user = frappe.session.user

    if user == 'Administrator':
        return ""

    user_roles = frappe.get_roles(user)

    if view_all_role and view_all_role in user_roles:
        return ""

    # Check if sales team override is enabled
    override_enabled = False
    try:
        override_enabled = bool(frappe.db.get_single_value("Basket4Me Settings", "override_sales_team_in_customer"))
    except Exception:
        pass

    parts = []

    # Multi-company scoping: restrict to companies the user is configured for.
    companies = _get_user_companies(user)
    if companies:
        parts.append(f"`tabSales Invoice`.company IN {_company_in_clause(companies)}")

    if override_enabled:
        # Sales-team check skipped; multi-company filter (if any) still applies
        return " AND ".join(parts) if parts else ""

    sales_person = get_sales_person(user)
    if sales_person:
        parts.append(f"""
            `tabSales Invoice`.name IN (
                SELECT parent FROM `tabSales Team`
                WHERE sales_person = '{sales_person}'
            )
        """)

    if not parts:
        return " "

    return "(" + ") AND (".join(parts) + ")"
