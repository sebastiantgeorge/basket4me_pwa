import frappe

def get_sales_person(user):
    user_name = frappe.db.get_value('User', user, 'name')
    sales_person = frappe.db.get_value(
        'Sales Person', {'custom_user': user_name})
    return sales_person


def _get_user_companies(user):
    try:
        from basket4me_pwa.api import _get_user_companies as _api_get_user_companies
        return _api_get_user_companies(user) or []
    except Exception:
        return []


def _company_in_clause(companies):
    return "(" + ",".join("'" + c.replace("'", "''") + "'" for c in companies) + ")"


@frappe.whitelist()
def get_permission_query_conditions_for_customer(user):
    view_all_role = frappe.get_value("Basket4Me Settings", None, 'view_all_transaction_role')

    if not user:
        user = frappe.session.user

    if user == 'Administrator':
        return ""

    user_roles = frappe.get_roles(user)

    if view_all_role and view_all_role in user_roles:
        return ""

    # Customer is not company-scoped here (customers can transact with any
    # company). Multi-company filter applied on transactional doctypes instead.
    override_enabled = False
    try:
        override_enabled = bool(frappe.db.get_single_value("Basket4Me Settings", "override_sales_team_in_customer"))
    except Exception:
        pass

    if override_enabled:
        return ""

    sales_person = get_sales_person(user)
    if sales_person:
        return f"""
            `tabCustomer`.name IN (
                SELECT parent FROM `tabSales Team`
                WHERE sales_person = '{sales_person}' AND parenttype = 'Customer'
            )
        """
    return ""


@frappe.whitelist()
def get_permission_query_conditions_for_payment_entry(user):
    view_all_role = frappe.get_value("Basket4Me Settings", None, 'view_all_transaction_role')

    if not user:
        user = frappe.session.user

    if user == 'Administrator':
        return ""

    user_roles = frappe.get_roles(user)

    if view_all_role and view_all_role in user_roles:
        return ""

    override_enabled = False
    try:
        override_enabled = bool(frappe.db.get_single_value("Basket4Me Settings", "override_sales_team_in_customer"))
    except Exception:
        pass

    parts = []

    # Multi-company scoping for Payment Entry
    companies = _get_user_companies(user)
    if companies:
        parts.append(f"`tabPayment Entry`.company IN {_company_in_clause(companies)}")

    if override_enabled:
        return " AND ".join(parts) if parts else ""

    sales_person = get_sales_person(user)
    if sales_person:
        parts.append(f"`tabPayment Entry`.custom_sales_person = '{sales_person}'")

    if not parts:
        return ""

    return "(" + ") AND (".join(parts) + ")"
