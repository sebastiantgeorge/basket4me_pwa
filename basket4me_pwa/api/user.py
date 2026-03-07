import frappe

@frappe.whitelist()
def get_user_profile(user=None):
    if not user:
        user = frappe.session.user

    # Get docs
    user_doc = frappe.get_doc("User", user)

    # Get roles
    roles = frappe.get_roles(user)

    # Get accessible doctypes
    doctypes = []
    for doctype in frappe.get_all("DocType", pluck="name"):
        if frappe.has_permission(doctype, "read", user=user):
            doctypes.append(doctype)

    # Get API key and secret
    api_key = frappe.db.get_value("User", user, "api_key")
    api_secret = None

    if api_key:
        api_secret = frappe.utils.password.get_decrypted_password(
            "User",
            user,
            "api_secret"
        )

    return {
        "user": user,
        "full_name": user_doc.full_name,
        "email": user_doc.email,
        "roles": roles,
        "accessible_doctypes": doctypes,
        "api_key": api_key,
        "api_secret": api_secret
    }

@frappe.whitelist()
def get_customers():
    customers = frappe.get_all(
        "Customer",
        fields=["name", "customer_name", "customer_group"]
    )
    
    return customers