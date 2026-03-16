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

    # if not user_details.api_key:
    #     api_key = frappe.generate_hash(length=15)
    #     user_details.api_key = api_key
    api_key = frappe.generate_hash(length=15)
    user_details.api_key = api_key if not user_details.api_key else user_details.api_key
    user_details.api_secret = api_secret
    user_details.save(ignore_permissions=True)
    frappe.db.commit()

    return api_secret


@frappe.whitelist(methods=["GET", "POST"], allow_guest=True)
def logout(usr):
    users_list = ["Administrator"]
    users = frappe.get_all("User")
    for user in users:
        users_list.append(user)

    if usr in users_list:
        try:
            logout_manager = frappe.auth.LoginManager()
            logout_manager.logout(usr)
            frappe.local.response["message"] = {
                "success_key": 1,
                "message": "Logged out successfully!",
            }
            frappe.local.response.http_status_code = 200

        except Exception as e:
            frappe.local.response["message"] = {
                "success_key": 0,
                "message": "Error logging out!",
                "Exception": e,
            }
            frappe.local.response.http_status_code = 400

    else:
        frappe.local.response["message"] = {
            "success_key": 0,
            "message": "User not found!",
        }
        frappe.local.response.http_status_code = 404

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

        # Get default company and its logo
        default_company = frappe.defaults.get_user_default("Company", user_id) or frappe.defaults.get_global_default("company")
        company_name = None
        company_logo = None
        if default_company:
            company_name = default_company
            company_logo = frappe.db.get_value("Company", default_company, "company_logo")
            if company_logo and not company_logo.startswith("http"):
                company_logo = frappe.utils.get_url(company_logo)

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
            "employee_id": frappe.get_value("Employee", {'user_id': user_doc.name}),
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