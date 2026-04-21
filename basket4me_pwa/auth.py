import frappe
from frappe.core.doctype.activity_log.activity_log import add_authentication_log


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def user_login(usr, pwd, device_id=None):
    if not usr or not pwd:
        frappe.local.response["message"] = {
            "success_key": 0,
            "message": "Both User and Password are required!",
        }
        frappe.local.response.http_status_code = 400
        return

    # Determine if `usr` is an email or phone/username
    filter_field = "email" if "@" in usr else ("mobile_no" if usr.isdigit() else "username")

    user = frappe.db.get_value("User", {filter_field: usr}, ["name", "username", "email", "mobile_no","api_key"], as_dict=True)

    if not user:
        frappe.local.response["message"] = {
            "success_key": 0,
            "message": f"{filter_field.capitalize()} {usr} Does Not Exist!",
        }
        frappe.local.response.http_status_code = 404
        frappe.log_error(
            title="Login Failed", message=f"{filter_field.capitalize()} {usr} Does Not Exist!"
        )
        return

    try:
        login_manager = frappe.auth.LoginManager()
        frappe.form_dict.device = "mobile"
        login_manager.authenticate(user=user.name, pwd=pwd)
        login_manager.post_login()
        # Optionally handle device_id

        # Generate API key/secret
        api_generate = generate_keys(user.name)
        # Get CSRF token from session for POS app to use in POST requests
        csrf_token = ""
        try:
            csrf_token = getattr(frappe.session.data, "csrf_token", "") or ""
            if not csrf_token:
                csrf_token = frappe.generate_hash()
                frappe.session.data.csrf_token = csrf_token
                # Save session via the session object
                if hasattr(frappe.local, "session_obj") and frappe.local.session_obj:
                    frappe.local.session_obj.update()
                frappe.db.commit()
        except Exception:
            csrf_token = csrf_token or ""

        frappe.response["message"] = {
            "success_key": 1,
            "message": "Authentication success",
            "sid": frappe.session.sid,
            "csrf_token": csrf_token,
            "api_key": user.api_key,
            "api_secret": api_generate,
            "username": user.username,
            "email": user.email,
            "mobile_no": user.mobile_no,
        }
    except frappe.exceptions.AuthenticationError:
        frappe.clear_messages()
        frappe.local.response["message"] = {
            "success_key": 0,
            "message": "Incorrect password!",
        }
        frappe.local.response.http_status_code = 401
        return

def set_device_to_mobile():
    # Ensure session exists before modifying
    if hasattr(frappe.local, 'session') and frappe.local.session.data:
        # Set the device in session data to 'mobile'
        frappe.local.session.data['device'] = 'mobile'
        # Commit the session change
        frappe.local.session.save()
    else:
        frappe.throw("Session not found.")

def generate_device_id(user, device_id):
    user_deveice_id = frappe.db.get_value("User Device",{'user':user},'device_id')
    if not user_deveice_id:
        user_details = frappe.new_doc("User Device")
        user_details.device_id = device_id
        user_details.user = user
        user_details.save(ignore_permissions=True)
        frappe.db.commit()
        user_deveice_id = user_details.device_id
    else:
        user_details = frappe.get_doc("User Device",{'user':user})
        user_details.device_id = device_id
        user_details.save(ignore_permissions=True)
        frappe.db.commit()
        user_deveice_id = user_details.device_id
    return user_deveice_id

def generate_keys(user):
    user_details = frappe.get_doc("User", user)
    api_secret = frappe.generate_hash(length=15)

    api_key = frappe.generate_hash(length=15)
    user_details.api_key = api_key if not user_details.api_key else user_details.api_key
    user_details.api_secret = api_secret

    # Bypass link validation - some users have references to deleted Workspaces etc.
    user_details.flags.ignore_links = True
    user_details.flags.ignore_validate = True
    user_details.flags.ignore_permissions = True

    try:
        user_details.save(ignore_permissions=True)
    except frappe.LinkValidationError:
        # Fallback: update api_key/api_secret directly via DB if save fails due to link issues
        frappe.db.set_value("User", user, "api_key", user_details.api_key, update_modified=False)
        frappe.db.set_value("User", user, "api_secret", frappe.utils.password.encrypt(api_secret), update_modified=False)

    frappe.db.commit()
    return api_secret


@frappe.whitelist(methods=["GET", "POST"], allow_guest=True)
def logout(usr):
    if not usr:
        frappe.local.response["message"] = {
            "success_key": 0,
            "message": "Username is required!",
        }
        frappe.local.response.http_status_code = 400
        return

    # Find user by email, mobile_no, or username
    filter_field = "email" if "@" in usr else ("mobile_no" if usr.isdigit() else "username")
    user_name = frappe.db.get_value("User", {filter_field: usr}, "name")

    if not user_name:
        frappe.local.response["message"] = {
            "success_key": 0,
            "message": "User not found!",
        }
        frappe.local.response.http_status_code = 404
        return

    try:
        logout_manager = frappe.auth.LoginManager()
        logout_manager.logout()
        frappe.local.response["message"] = {
            "success_key": 1,
            "message": "Logged out successfully!",
        }
        frappe.local.response.http_status_code = 200

    except Exception as e:
        frappe.local.response["message"] = {
            "success_key": 0,
            "message": "Error logging out!",
            "exception": str(e),
        }
        frappe.local.response.http_status_code = 400

@frappe.whitelist(methods=["GET", "POST"], allow_guest=True)
def get_user_details(sid=None, user_id=None):
    try:
        if sid:
            # Validate and retrieve the user session using the sid
           get_cookie_options()

        if not user_id:
            frappe.throw("Either user_id or valid sid is required")

        # Fetch the user details using user_id
        user_doc = frappe.get_doc("User", user_id)

        # Get user profile image (full URL)
        user_image = user_doc.user_image
        if user_image and not user_image.startswith("http"):
            user_image = frappe.utils.get_url(user_image)

        # Get company from Basket4Me Settings > Sales Person Details
        company_name = None
        company_logo = None
        settings = frappe.get_single("Basket4Me Settings")
        for row in settings.get("sales_person_details", []):
            if row.sales_person == user_doc.username:
                company_name = row.company
                break

        if not company_name:
            company_name = frappe.defaults.get_user_default("Company", user_id) or frappe.defaults.get_global_default("company")

        company_default_price_list = None
        company_address = None
        company_gst_no = None
        if company_name:
            company_fields = ["company_logo"]
            if frappe.db.has_column("Company", "default_price_list"):
                company_fields.append("default_price_list")
            # GST number - check common field names
            for gst_field in ["tax_id", "gstin", "custom_gstin"]:
                if frappe.db.has_column("Company", gst_field):
                    company_fields.append(gst_field)
                    break
            company_doc = frappe.db.get_value("Company", company_name, company_fields, as_dict=True)
            if company_doc:
                company_logo = company_doc.get("company_logo")
                company_default_price_list = company_doc.get("default_price_list")
                company_gst_no = company_doc.get("tax_id") or company_doc.get("gstin") or company_doc.get("custom_gstin")
            # Fallback: get from Selling Settings
            if not company_default_price_list:
                try:
                    company_default_price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
                except Exception:
                    pass
            if company_logo and not company_logo.startswith("http"):
                company_logo = frappe.utils.get_url(company_logo)

            # Company address - get primary address
            try:
                addr_name = frappe.db.get_value("Dynamic Link",
                    {"link_doctype": "Company", "link_name": company_name, "parenttype": "Address"},
                    "parent")
                if addr_name:
                    addr = frappe.db.get_value("Address", addr_name,
                        ["address_line1", "address_line2", "city", "state", "pincode", "country"],
                        as_dict=True)
                    if addr:
                        parts = [addr.address_line1, addr.address_line2, addr.city, addr.state, addr.pincode, addr.country]
                        company_address = ", ".join([p for p in parts if p])
            except Exception:
                pass

        # Get doctype permissions for the user
        doctypes_to_check = [
            "Sales Order",
            "Delivery Note",
            "Sales Invoice",
            "Payment Entry",
            "Customer",
            "Item",
            "Material Request",
        ]
        permissions = {}
        for dt in doctypes_to_check:
            try:
                perm = frappe.permissions.get_doc_permissions(frappe.new_doc(dt), user=user_id)
                permissions[dt] = {
                    "read": bool(perm.get("read", 0)),
                    "create": bool(perm.get("create", 0)),
                    "write": bool(perm.get("write", 0)),
                    "submit": bool(perm.get("submit", 0)),
                    "cancel": bool(perm.get("cancel", 0)),
                    "delete": bool(perm.get("delete", 0)),
                    "amend": bool(perm.get("amend", 0)),
                    "print": bool(perm.get("print", 0)),
                }
            except Exception:
                permissions[dt] = {
                    "read": False, "create": False, "write": False,
                    "submit": False, "cancel": False, "delete": False,
                    "amend": False, "print": False,
                }

        # Company bank details
        company_bank_name = None
        company_bank_acc_no = None
        company_bank_ifsc = None
        company_bank_branch = None
        if company_name:
            try:
                default_bank = frappe.db.get_value("Company", company_name, "default_bank_account")
                if default_bank:
                    bank_doc = frappe.db.get_value("Bank Account", default_bank,
                        ["bank", "bank_account_no", "branch_code"], as_dict=True)
                    if bank_doc:
                        company_bank_name = bank_doc.get("bank")
                        company_bank_acc_no = bank_doc.get("bank_account_no")
                        company_bank_ifsc = bank_doc.get("branch_code")
                        # Get branch from Bank if available
                        if company_bank_name:
                            company_bank_branch = frappe.db.get_value("Bank", company_bank_name, "swift_number") or None
            except Exception:
                pass

        user_data = {
            "sid": sid if sid else frappe.session.sid,
            "api_key": user_doc.api_key,
            "api_secret": user_doc.get_password('api_secret'),
            "username": user_doc.username,
            "email": user_doc.email,
            "full_name": user_doc.full_name,
            "mobile_no": user_doc.mobile_no,
            "user_image": user_image,
            "company_name": company_name,
            "company_logo": company_logo,
            "company_address": company_address,
            "company_gst_no": company_gst_no,
            "company_bank_name": company_bank_name,
            "company_bank_acc_no": company_bank_acc_no,
            "company_bank_ifsc": company_bank_ifsc,
            "company_bank_branch": company_bank_branch,
            "company_default_price_list": company_default_price_list,
            "employee_id": frappe.get_value("Employee", {'user_id': user_doc.name}),
            "permissions": permissions,
        }

        frappe.local.response["message"] = {
            "success_key": 1,
            "message": "Fetched User details successfully!",
            "user": user_data,
        }
        frappe.local.response.http_status_code = 200
    except Exception as e:
        frappe.local.response["message"] = {
            "success_key": 0,
            "message": "Error!",
            "exception": str(e),
        }
        frappe.local.response.http_status_code = 404
        frappe.log_error(title="Get User Details Failed.", message=str(e))


def handle_cors():
    frappe.local.response.headers["Access-Control-Allow-Origin"] = "*"
    frappe.local.response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    frappe.local.response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    ("Access-Control-Allow-Headers", "Access-Control-Allow-Headers, Origin,Accept, X-Requested-With, Content-Type, Access-Control-Request-Method, Access-Control-Request-Headers")
    
    # Respond to OPTIONS preflight requests
    if frappe.request.method == "OPTIONS":
        frappe.local.response["status_code"] = 200
        return {}

def get_cookie_options():
	options = {}
	if frappe.session and frappe.session.sid and hasattr(frappe.local, "request"):
		# Use wkhtmltopdf's cookie-jar feature to set cookies and restrict them to host domain
		cookiejar = f"/tmp/{frappe.generate_hash()}.jar"

		# Remove port from request.host
		# https://werkzeug.palletsprojects.com/en/0.16.x/wrappers/#werkzeug.wrappers.BaseRequest.host
		domain = frappe.utils.get_host_name().split(":", 1)[0]
		with open(cookiejar, "w") as f:
			f.write(f"sid={frappe.session.sid}; Domain={domain};\n")

		options["cookie-jar"] = cookiejar

	return options