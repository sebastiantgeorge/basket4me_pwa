import frappe
from frappe import _
from frappe.utils import nowdate, flt, cint
from frappe.model.mapper import get_mapped_doc
import json


# ---------------------------------------------------------------------------
# Helper: centralised error response
# ---------------------------------------------------------------------------

def _error(message: str, field: str = None, http_status: int = 400):
    frappe.local.response["http_status_code"] = http_status
    return {
        "success": False,
        "message": message,
        "field": field,
    }


def _success(message: str, data: dict = None):
    return {
        "success": True,
        "message": message,
        "data": data or {},
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_required_fields(doc_data: dict) -> list:
    """Return a list of validation error dicts for missing required fields."""
    errors = []
    required = {
        "customer": _("Customer is required"),
        "delivery_date": _("Delivery Date is required"),
        "items": _("At least one item is required"),
    }
    for field, msg in required.items():
        if not doc_data.get(field):
            errors.append({"field": field, "message": msg})
    return errors


def _validate_items(items: list) -> list:
    """Validate each item row."""
    errors = []
    for idx, item in enumerate(items):
        row_no = idx + 1
        if not item.get("item_code"):
            errors.append({
                "field": f"items[{idx}].item_code",
                "message": _("Row {0}: Item Code is required").format(row_no),
            })
        if flt(item.get("qty", 0)) <= 0:
            errors.append({
                "field": f"items[{idx}].qty",
                "message": _("Row {0}: Quantity must be greater than zero").format(row_no),
            })
        if flt(item.get("rate", 0)) < 0:
            errors.append({
                "field": f"items[{idx}].rate",
                "message": _("Row {0}: Rate cannot be negative").format(row_no),
            })
    return errors


def _validate_dates(doc_data: dict) -> list:
    errors = []
    delivery_date = doc_data.get("delivery_date")
    transaction_date = doc_data.get("transaction_date") or nowdate()

    if delivery_date and delivery_date < transaction_date:
        errors.append({
            "field": "delivery_date",
            "message": _("Delivery Date cannot be before the Order Date"),
        })
    return errors


def _validate_customer(customer: str) -> list:
    errors = []
    if not frappe.db.exists("Customer", customer):
        errors.append({
            "field": "customer",
            "message": _("Customer '{0}' does not exist").format(customer),
        })
    elif frappe.db.get_value("Customer", customer, "disabled"):
        errors.append({
            "field": "customer",
            "message": _("Customer '{0}' is disabled").format(customer),
        })
    return errors


def _validate_items_exist(items: list) -> list:
    errors = []
    for idx, item in enumerate(items):
        item_code = item.get("item_code")
        if item_code and not frappe.db.exists("Item", item_code):
            errors.append({
                "field": f"items[{idx}].item_code",
                "message": _("Item '{0}' does not exist").format(item_code),
            })
        elif item_code:
            disabled = frappe.db.get_value("Item", item_code, "disabled")
            if disabled:
                errors.append({
                    "field": f"items[{idx}].item_code",
                    "message": _("Item '{0}' is disabled").format(item_code),
                })
    return errors


def _validate_amendment(name: str, doc_data: dict) -> list:
    """Extra checks when editing an existing Sales Order."""
    errors = []
    existing = frappe.get_doc("Sales Order", name)

    if existing.docstatus == 2:
        errors.append({
            "field": "name",
            "message": _("Cannot edit a cancelled Sales Order"),
        })
    elif existing.docstatus == 1:
        errors.append({
            "field": "name",
            "message": _(
                "Sales Order is submitted. Cancel it before editing, "
                "or create an amendment."
            ),
        })
    elif existing.customer != doc_data.get("customer"):
        errors.append({
            "field": "customer",
            "message": _("Customer cannot be changed on an existing Sales Order"),
        })
    return errors


# ---------------------------------------------------------------------------
# Main whitelisted endpoint
# ---------------------------------------------------------------------------

@frappe.whitelist()
def save_sales_order(data):
    """
    Create or update a Sales Order (draft only).

    Args:
        data (str | dict): JSON string or dict with Sales Order fields.

    Payload structure
    -----------------
    {
        "name": "SAL-ORD-0001",          # omit or null to create new
        "customer": "CUST-001",
        "transaction_date": "2024-01-15", # defaults to today
        "delivery_date": "2024-01-30",
        "currency": "INR",
        "selling_price_list": "Standard Selling",
        "items": [
            {
                "item_code": "ITEM-001",
                "item_name": "Widget",    # optional – fetched if omitted
                "qty": 5,
                "rate": 100.00,
                "warehouse": "Stores - XX"
            }
        ],
        "taxes_and_charges": "GST 18% - XX",  # optional
        "additional_discount_percentage": 0,
        "notes": "Urgent delivery"
    }

    Returns
    -------
    {
        "success": true,
        "message": "Sales Order saved successfully",
        "data": { "name": "SAL-ORD-0001", "grand_total": 500.0, ... }
    }
    """

    # --- 1. Parse payload ---------------------------------------------------
    if isinstance(data, str):
        try:
            doc_data = json.loads(data)
        except (ValueError, TypeError):
            return _error(_("Invalid JSON payload"), http_status=400)
    elif isinstance(data, dict):
        doc_data = data
    else:
        return _error(_("Unsupported data format"), http_status=400)

    # --- 2. Permission check ------------------------------------------------
    if not frappe.has_permission("Sales Order", "write"):
        frappe.local.response["http_status_code"] = 403
        return _error(_("You do not have permission to create/edit Sales Orders"), http_status=403)

    # --- 3. Required-field validation ----------------------------------------
    validation_errors = _validate_required_fields(doc_data)
    if validation_errors:
        frappe.local.response["http_status_code"] = 422
        return {
            "success": False,
            "message": _("Validation failed"),
            "errors": validation_errors,
        }

    # --- 4. Field-level validations -----------------------------------------
    all_errors = []
    all_errors += _validate_customer(doc_data["customer"])
    all_errors += _validate_dates(doc_data)
    all_errors += _validate_items(doc_data["items"])
    all_errors += _validate_items_exist(doc_data["items"])

    name = doc_data.get("name")
    is_edit = bool(name and frappe.db.exists("Sales Order", name))

    if is_edit:
        all_errors += _validate_amendment(name, doc_data)

    if all_errors:
        frappe.local.response["http_status_code"] = 422
        return {
            "success": False,
            "message": _("Validation failed"),
            "errors": all_errors,
        }

    # --- 5. Build / update document -----------------------------------------
    try:
        if is_edit:
            so = frappe.get_doc("Sales Order", name)
            so.flags.ignore_permissions = False

            # Update allowed header fields
            allowed_header_fields = [
                "delivery_date", "transaction_date", "currency",
                "selling_price_list", "taxes_and_charges",
                "additional_discount_percentage", "notes",
                "po_no", "po_date",
            ]
            for field in allowed_header_fields:
                if field in doc_data:
                    so.set(field, doc_data[field])

            # Replace items
            so.set("items", [])
            for item_row in doc_data["items"]:
                so.append("items", _build_item_row(item_row))

            action = "updated"
        else:
            so = frappe.new_doc("Sales Order")
            so.company = (
                doc_data.get("company")
                or frappe.defaults.get_user_default("Company")
                or frappe.db.get_single_value("Global Defaults", "default_company")
            )
            so.customer = doc_data["customer"]
            so.transaction_date = doc_data.get("transaction_date") or nowdate()
            so.delivery_date = doc_data["delivery_date"]
            so.currency = doc_data.get("currency") or frappe.db.get_value(
                "Customer", doc_data["customer"], "default_currency"
            ) or frappe.db.get_single_value("Global Defaults", "default_currency")
            so.selling_price_list = doc_data.get("selling_price_list") or frappe.db.get_value(
                "Selling Settings", None, "selling_price_list"
            )
            so.taxes_and_charges = doc_data.get("taxes_and_charges")
            so.additional_discount_percentage = flt(
                doc_data.get("additional_discount_percentage", 0)
            )
            so.po_no = doc_data.get("po_no")
            so.po_date = doc_data.get("po_date")
            so.notes = doc_data.get("notes")

            for item_row in doc_data["items"]:
                so.append("items", _build_item_row(item_row))

            action = "created"

        so.save(ignore_permissions=False)
        frappe.db.commit()

        return _success(
            _("Sales Order {0} successfully").format(_(action)),
            data={
                "name": so.name,
                "grand_total": so.grand_total,
                "net_total": so.net_total,
                "status": so.status,
                "docstatus": so.docstatus,
                "transaction_date": str(so.transaction_date),
                "delivery_date": str(so.delivery_date),
                "customer": so.customer,
                "customer_name": so.customer_name,
                "currency": so.currency,
                "items": [
                    {
                        "item_code": r.item_code,
                        "item_name": r.item_name,
                        "qty": r.qty,
                        "rate": r.rate,
                        "amount": r.amount,
                        "warehouse": r.warehouse,
                    }
                    for r in so.items
                ],
            },
        )

    except frappe.exceptions.ValidationError as exc:
        frappe.db.rollback()
        return _error(str(exc), http_status=422)
    except frappe.exceptions.PermissionError as exc:
        frappe.db.rollback()
        return _error(str(exc), http_status=403)
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "save_sales_order API Error")
        return _error(
            _("An unexpected error occurred. Please contact your administrator."),
            http_status=500,
        )


def _build_item_row(item_row: dict) -> dict:
    """Normalise an item dict before appending to the doc."""
    item_code = item_row.get("item_code")
    item_name = item_row.get("item_name") or frappe.db.get_value(
        "Item", item_code, "item_name"
    )
    return {
        "item_code": item_code,
        "item_name": item_name,
        "qty": flt(item_row.get("qty", 1)),
        "rate": flt(item_row.get("rate", 0)),
        "warehouse": item_row.get("warehouse") or frappe.db.get_single_value(
            "Stock Settings", "default_warehouse"
        ),
        "description": item_row.get("description") or item_name,
        "uom": item_row.get("uom") or frappe.db.get_value("Item", item_code, "stock_uom"),
        "conversion_factor": flt(item_row.get("conversion_factor", 1)),
    }


# ---------------------------------------------------------------------------
# Bonus: fetch existing Sales Order (for edit pre-fill)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_sales_order(name: str):
    """
    Fetch a Sales Order for editing in the mobile app.

    GET /api/method/your_app.api.sales_order_api.get_sales_order?name=SAL-ORD-0001
    """
    if not frappe.db.exists("Sales Order", name):
        return _error(_("Sales Order '{0}' not found").format(name), http_status=404)

    if not frappe.has_permission("Sales Order", "read", name):
        return _error(_("Permission denied"), http_status=403)

    so = frappe.get_doc("Sales Order", name)
    return _success("OK", data=so.as_dict())


# ---------------------------------------------------------------------------
# Bonus: get customer list for dropdown
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_customers(search: str = "", limit: int = 20):
    """
    /api/method/your_app.api.sales_order_api.get_customers?search=acme&limit=10
    """
    customers = frappe.get_all(
        "Customer",
        filters={
            "disabled": 0,
            "name": ["like", f"%{search}%"] if search else ["!=", ""],
        },
        fields=["name", "customer_name", "customer_group", "territory"],
        limit=cint(limit),
        order_by="customer_name asc",
    )
    return _success("OK", data={"customers": customers})


# ---------------------------------------------------------------------------
# Bonus: get item list for dropdown
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_items(search: str = "", limit: int = 20):
    """
    /api/method/your_app.api.sales_order_api.get_items?search=widget&limit=10
    """
    items = frappe.get_all(
        "Item",
        filters={
            "disabled": 0,
            "is_sales_item": 1,
            "item_name": ["like", f"%{search}%"] if search else ["!=", ""],
        },
        fields=["name", "item_name", "item_group", "stock_uom", "standard_rate"],
        limit=cint(limit),
        order_by="item_name asc",
    )
    return _success("OK", data={"items": items})