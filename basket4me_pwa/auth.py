import frappe
from frappe.core.doctype.activity_log.activity_log import add_authentication_log


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def user_login(usr, pwd, device_id=None, create_session=False):
    """Login the user.

    Mobile clients authenticate via api_key/api_secret (Authorization: token
    <key>:<secret>) on subsequent requests, so by default we SKIP
    LoginManager.post_login() entirely. That call's on_session_creation /
    on_login hook chain plus clear_active_sessions / boot_cache work has
    been observed to hang past the upstream proxy's 60s timeout on some
    sites (504 Gateway Timeout on btlerp).

    Pass create_session=1 if you actually need a server-side cookie session
    (sid + csrf_token) — it runs the full LoginManager.post_login() flow.
    """
    if not usr or not pwd:
        frappe.local.response["message"] = {
            "success_key": 0,
            "message": "Both User and Password are required!",
        }
        frappe.local.response.http_status_code = 400
        return

    # Determine if `usr` is an email or phone/username
    filter_field = "email" if "@" in usr else ("mobile_no" if usr.isdigit() else "username")

    user = frappe.db.get_value(
        "User", {filter_field: usr},
        ["name", "username", "email", "mobile_no", "api_key", "enabled"],
        as_dict=True,
    )

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

    if not user.enabled:
        frappe.local.response["message"] = {"success_key": 0, "message": "User is disabled."}
        frappe.local.response.http_status_code = 403
        return

    # ── Fast password verification (no session, no hooks) ──
    try:
        from frappe.utils.password import check_password
        check_password(user.name, pwd)
    except frappe.AuthenticationError:
        frappe.clear_messages()
        frappe.local.response["message"] = {"success_key": 0, "message": "Incorrect password!"}
        frappe.local.response.http_status_code = 401
        return
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "user_login check_password error")
        frappe.local.response["message"] = {"success_key": 0, "message": str(e)}
        frappe.local.response.http_status_code = 500
        return

    # ── Generate / fetch API keys (fast path for existing users) ──
    try:
        api_generate = generate_keys(user.name)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "user_login generate_keys error")
        frappe.local.response["message"] = {"success_key": 0, "message": f"Key generation failed: {e}"}
        frappe.local.response.http_status_code = 500
        return

    # Re-fetch api_key (generate_keys may have minted one for first-time users)
    api_key_val = user.api_key or frappe.db.get_value("User", user.name, "api_key") or ""

    # ── Optional session creation (off by default to avoid 504 hangs) ──
    sid = ""
    csrf_token = ""
    if create_session and str(create_session).lower() not in ("0", "false", "no"):
        try:
            login_manager = frappe.auth.LoginManager()
            frappe.form_dict.device = "mobile"
            login_manager.authenticate(user=user.name, pwd=pwd)
            login_manager.post_login()
            sid = frappe.session.sid
            try:
                csrf_token = getattr(frappe.session.data, "csrf_token", "") or ""
                if not csrf_token:
                    csrf_token = frappe.generate_hash()
                    frappe.session.data.csrf_token = csrf_token
                    if hasattr(frappe.local, "session_obj") and frappe.local.session_obj:
                        frappe.local.session_obj.update()
                    frappe.db.commit()
            except Exception:
                csrf_token = csrf_token or ""
        except Exception:
            # Session creation is best-effort. Login already succeeded above.
            frappe.log_error(frappe.get_traceback(), "user_login session creation skipped")

    frappe.response["message"] = {
        "success_key": 1,
        "message": "Authentication success",
        "sid": sid,
        "csrf_token": csrf_token,
        "api_key": api_key_val,
        "api_secret": api_generate,
        "username": user.username,
        "email": user.email,
        "mobile_no": user.mobile_no,
    }

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

def _get_allow_negative_stock(company_name=None):
    """
    Returns True if negative stock is allowed.
    Checks Company-level setting first (custom_allow_negative_stock if exists),
    falls back to global Stock Settings.allow_negative_stock.
    """
    try:
        # Company-level override (if a custom field exists)
        if company_name and frappe.db.has_column("Company", "custom_allow_negative_stock"):
            val = frappe.db.get_value("Company", company_name, "custom_allow_negative_stock")
            if val is not None:
                return bool(val)
        # Global Stock Settings
        return bool(frappe.db.get_single_value("Stock Settings", "allow_negative_stock") or 0)
    except Exception:
        return False


def generate_keys(user):
    """Return an api_secret for `user`. NEVER calls User.save() — that path
    triggers on_update hooks (notifications, workspace validation, etc.)
    which have been observed to hang past the upstream proxy's 60s timeout
    on some sites, returning 504 on login.

    Fast path: user already has api_key + decryptable api_secret → return existing.
    Mint path: write api_key directly via db.set_value, store api_secret via
    set_encrypted_password (Password fields live in the Auth table, not tabUser).
    """
    from frappe.utils.password import get_decrypted_password, set_encrypted_password

    # ── Fast path ──
    existing_api_key = frappe.db.get_value("User", user, "api_key")
    if existing_api_key:
        try:
            existing_secret = get_decrypted_password(
                "User", user, fieldname="api_secret", raise_exception=False
            )
            if existing_secret:
                return existing_secret
        except Exception:
            pass  # mint a new secret below

    # ── Mint path: direct DB writes, no User.save() ──
    api_secret = frappe.generate_hash(length=15)
    api_key = existing_api_key or frappe.generate_hash(length=15)

    try:
        if not existing_api_key:
            frappe.db.set_value("User", user, "api_key", api_key, update_modified=False)
        set_encrypted_password("User", user, api_secret, fieldname="api_secret")
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "generate_keys mint error")
        raise

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

        # Get companies from Basket4Me Settings > Sales Person Details (multi-company).
        # `company_name` (singular) stays for backward compat = default company.
        companies = []
        company_logo = None
        settings = frappe.get_single("Basket4Me Settings")
        for row in settings.get("sales_person_details", []):
            if row.sales_person == user_doc.username and row.company and row.company not in companies:
                companies.append(row.company)

        default_company = companies[0] if companies else None
        company_name = default_company or (
            frappe.defaults.get_user_default("Company", user_id)
            or frappe.defaults.get_global_default("company")
        )

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
            "companies": companies,
            "default_company": default_company,
            "company_logo": company_logo,
            "company_address": company_address,
            "company_gst_no": company_gst_no,
            "company_bank_name": company_bank_name,
            "company_bank_acc_no": company_bank_acc_no,
            "company_bank_ifsc": company_bank_ifsc,
            "company_bank_branch": company_bank_branch,
            "company_default_price_list": company_default_price_list,
            "allow_negative_stock": _get_allow_negative_stock(company_name),
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