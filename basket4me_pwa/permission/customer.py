import frappe

def get_sales_person(user):
    user_name = frappe.db.get_value('User', user, 'name')
    sales_person = frappe.db.get_value(
        'Sales Person', {'custom_user': user_name})
    return sales_person


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

    # Check if sales team override is enabled
    try:
        override_enabled = frappe.db.get_single_value("Basket4Me Settings", "override_sales_team_in_customer")
        if override_enabled:
            return ""  # No restrictions if override is enabled
    except:
        pass

    sales_person = get_sales_person(user)
    if sales_person:
        conditions = f"""
            (`tabCustomer`.name IN (
                SELECT parent
                FROM `tabSales Team`
                WHERE sales_person = '{sales_person}' AND parenttype = 'Customer'
            ))
        """
        return conditions
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

    # Check if sales team override is enabled
    try:
        override_enabled = frappe.db.get_single_value("Basket4Me Settings", "override_sales_team_in_customer")
        if override_enabled:
            return ""  # No restrictions if override is enabled
    except:
        pass

    sales_person = get_sales_person(user)
    if sales_person:
        conditions = f""" `tabPayment Entry`.custom_sales_person = '{sales_person}' """
        return conditions
    return ""
