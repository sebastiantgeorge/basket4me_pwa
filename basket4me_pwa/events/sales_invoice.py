import frappe
from frappe.utils import nowdate, flt
from basket4me_pwa.api import get_basket4me_settings

def calculate_item_discounts(doc, method):
    """
    Trigger ERPNext's built-in calculation for discounts when invoices are created via API.
    This ensures discount_amount is calculated from discount_percentage automatically.
    """
    # Only trigger recalculation for mobile app invoices that have discount percentages
    if doc.custom_mobile_app:
        has_discount = any(flt(item.discount_percentage) for item in doc.items)
        if has_discount:
            doc.run_method("calculate_taxes_and_totals")


def validate_free_item_rates_delivery_note(doc, method):
    """
    Server-side validation to ensure free items in Delivery Note always have rate = 0
    This validation runs every time a Delivery Note is saved/validated
    """
    for item in doc.items:
        if hasattr(item, 'is_free_item') and item.is_free_item:
            rate_fields = [
                'rate', 'stock_uom_rate', 'price_list_rate',
                'base_rate', 'base_price_list_rate',
            ]

            amount_fields = [
                'amount', 'net_amount', 'base_amount',
                'base_net_amount', 'stock_uom_amount',
            ]

            for field in rate_fields:
                if hasattr(item, field):
                    current_value = getattr(item, field, 0)
                    if current_value != 0:
                        frappe.log_error(
                            f"DN: {field}={current_value}→0 for free item {item.item_code} in {doc.name or 'New'}",
                            f"Free Item DN Fix"
                        )
                        setattr(item, field, 0)

            for field in amount_fields:
                if hasattr(item, field):
                    setattr(item, field, 0)

            if hasattr(item, 'discount_amount') and item.discount_amount > 0:
                item.discount_percentage = 0
                item.discount_amount = 0

            if hasattr(item, 'base_discount_amount') and item.base_discount_amount > 0:
                item.base_discount_amount = 0

def validate_free_item_rates(doc, method):
    """
    Server-side validation to ensure free items always have rate = 0
    This validation runs every time a Sales Invoice is saved/validated
    """
    for item in doc.items:
        if hasattr(item, 'is_free_item') and item.is_free_item:
            rate_fields = [
                'rate', 'stock_uom_rate', 'price_list_rate',
                'base_rate', 'base_price_list_rate',
            ]

            amount_fields = [
                'amount', 'net_amount', 'base_amount',
                'base_net_amount', 'stock_uom_amount',
            ]

            for field in rate_fields:
                if hasattr(item, field):
                    current_value = getattr(item, field, 0)
                    if current_value != 0:
                        frappe.log_error(
                            f"SI: {field}={current_value}→0 for free item {item.item_code} in {doc.name or 'New'}",
                            f"Free Item SI Fix"
                        )
                        setattr(item, field, 0)

            for field in amount_fields:
                if hasattr(item, field):
                    setattr(item, field, 0)

            if hasattr(item, 'discount_amount') and item.discount_amount > 0:
                item.discount_percentage = 0
                item.discount_amount = 0

            if hasattr(item, 'base_discount_amount') and item.base_discount_amount > 0:
                item.base_discount_amount = 0

def create_delivery_note(doc, method):
    if not doc.custom_mobile_app:
        return

    settings = get_basket4me_settings()
    if settings.ignore_create_delivery_note:
        return

    # When auto_create_delivery_note is enabled, the explicit code path in
    # submit_sales_invoice (api.py) creates the DN. Bail here to avoid
    # creating a duplicate Delivery Note.
    if getattr(settings, "auto_create_delivery_note", 0):
        return

    try:
        _create_delivery_note_impl(doc, method)
    except Exception as e:
        frappe.log_error(
            f"Failed to create Delivery Note for SI {doc.name}: {str(e)}\n{frappe.get_traceback()}",
            "Create Delivery Note Error",
        )
        frappe.msgprint(f"Warning: Could not create Delivery Note: {str(e)}")


def _create_delivery_note_impl(doc, method):
    allow_negative_stock = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")

    if doc.is_return == 1:
        delivery_note = frappe.new_doc("Delivery Note")
        delivery_note.customer = doc.customer
        delivery_note.posting_date = doc.posting_date
        delivery_note.posting_time = doc.posting_time
        delivery_note.taxes_and_charges = doc.taxes_and_charges
        delivery_note.company = doc.company
        delivery_note.is_return = 1

        orig_si_item_map = {}
        orig_dn_name = None

        if doc.return_against:
            orig_dns = frappe.db.sql("""
                SELECT DISTINCT dni.parent
                FROM `tabDelivery Note Item` dni
                JOIN `tabDelivery Note` dn ON dn.name = dni.parent
                WHERE dni.against_sales_invoice = %s
                AND dn.docstatus IN (0, 1)
                AND dn.is_return = 0
                LIMIT 1
            """, doc.return_against, as_dict=True)
            if orig_dns:
                orig_dn_name = orig_dns[0].parent
                delivery_note.return_against = orig_dn_name

            orig_si_items = frappe.get_all(
                "Sales Invoice Item",
                filters={"parent": doc.return_against},
                fields=["name", "item_code"],
            )
            for osi in orig_si_items:
                orig_si_item_map[osi.item_code] = osi.name

        all_items_sufficient = True

        for item in doc.items:
            available_qty = frappe.db.get_value(
                "Bin", {"item_code": item.item_code, "warehouse": item.warehouse}, "actual_qty"
            ) or 0

            item_rate = 0 if (hasattr(item, 'is_free_item') and item.is_free_item) else item.rate

            if doc.return_against and item.item_code in orig_si_item_map:
                linked_si = doc.return_against
                linked_si_detail = orig_si_item_map[item.item_code]
            else:
                linked_si = item.parent
                linked_si_detail = item.name

            delivery_note_item = {
                "item_code": item.item_code,
                "uom": item.uom,
                "qty": item.qty,
                "rate": item_rate,
                "warehouse": item.warehouse,
                "against_sales_invoice": linked_si,
                "si_detail": linked_si_detail,
                "cost_center": item.cost_center,
            }

            if hasattr(item, 'is_free_item') and item.is_free_item:
                delivery_note_item["is_free_item"] = True

            if orig_dn_name:
                orig_dn_item = frappe.db.get_value(
                    "Delivery Note Item",
                    {"parent": orig_dn_name, "item_code": item.item_code},
                    "name",
                )
                if orig_dn_item:
                    delivery_note_item["dn_detail"] = orig_dn_item

            delivery_note.append("items", delivery_note_item)

            if not allow_negative_stock and abs(item.qty) > available_qty:
                all_items_sufficient = False

        for tax in doc.taxes:
            delivery_note.append("taxes", {
                "charge_type": tax.charge_type,
                "account_head": tax.account_head,
                "description": tax.description,
                "rate": tax.rate,
                "tax_amount": tax.tax_amount,
                "total": tax.total,
                "cost_center": tax.cost_center,
            })

        for sp in doc.sales_team:
            delivery_note.append("sales_team", {
                "sales_person": sp.sales_person,
                "allocated_percentage": sp.allocated_percentage,
            })

        delivery_note.flags.ignore_permissions = True
        delivery_note.save()

        from basket4me_pwa.api import enforce_free_item_rates_delivery_note
        enforce_free_item_rates_delivery_note(delivery_note)

        if all_items_sufficient:
            delivery_note.flags.ignore_permissions = True
            delivery_note.submit()
            frappe.msgprint(f"Return Delivery Note {delivery_note.name} submitted.")
        else:
            frappe.msgprint(f"Return Delivery Note {delivery_note.name} saved as draft due to insufficient stock.")

    else:
        delivery_note = frappe.new_doc("Delivery Note")
        delivery_note.customer = doc.customer
        delivery_note.posting_date = doc.posting_date
        delivery_note.posting_time = doc.posting_time
        delivery_note.taxes_and_charges = doc.taxes_and_charges
        delivery_note.company = doc.company
        all_items_sufficient = True

        for item in doc.items:
            available_qty = frappe.db.get_value("Bin", {"item_code": item.item_code, "warehouse": item.warehouse}, "actual_qty") or 0

            item_rate = 0 if (hasattr(item, 'is_free_item') and item.is_free_item) else item.rate

            delivery_note_item = {
                "item_code": item.item_code,
                "uom": item.uom,
                "qty": item.qty,
                "rate": item_rate,
                "warehouse": item.warehouse,
                "against_sales_invoice": item.parent,
                "si_detail": item.name,
                "cost_center": item.cost_center
            }

            if hasattr(item, 'is_free_item') and item.is_free_item:
                delivery_note_item["is_free_item"] = True

            delivery_note.append("items", delivery_note_item)

            if not allow_negative_stock and item.qty > available_qty:
                all_items_sufficient = False
        for tax in doc.taxes:
            delivery_note.append("taxes", {
                "charge_type": tax.charge_type,
                "account_head": tax.account_head,
                "description": tax.description,
                "rate": tax.rate,
                "tax_amount": tax.tax_amount,
                "total": tax.total,
                "cost_center": tax.cost_center
            })
        for sp in doc.sales_team:
            delivery_note.append("sales_team", {
                    "sales_person": sp.sales_person,
                    "allocated_percentage": sp.allocated_percentage
                })

        delivery_note.flags.ignore_permissions = True
        delivery_note.save()

        from basket4me_pwa.api import enforce_free_item_rates_delivery_note
        enforce_free_item_rates_delivery_note(delivery_note)

        if all_items_sufficient:
            delivery_note.flags.ignore_permissions = True
            delivery_note.submit()
            frappe.msgprint(f"Delivery Note {delivery_note.name} submitted as sufficient stock is available.")
        else:
            frappe.msgprint(f"Delivery Note {delivery_note.name} saved as draft due to insufficient stock.")

def cancel_delivery_note(doc, method):
    """Cancel or delete linked Delivery Notes when a Sales Invoice is cancelled."""
    if not doc.custom_mobile_app:
        return

    settings = get_basket4me_settings()
    if settings.ignore_create_delivery_note:
        return

    linked_dn_items = frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_invoice": doc.name},
        fields=["parent"],
        group_by="parent",
    )

    if not linked_dn_items:
        return

    for row in linked_dn_items:
        dn_name = row.parent
        try:
            dn = frappe.get_doc("Delivery Note", dn_name)

            if dn.docstatus == 1:
                dn.flags.ignore_permissions = True
                dn.cancel()
                frappe.msgprint(f"Delivery Note {dn_name} cancelled.")
            elif dn.docstatus == 0:
                frappe.delete_doc("Delivery Note", dn_name, ignore_permissions=True)
                frappe.msgprint(f"Draft Delivery Note {dn_name} deleted.")
        except Exception as e:
            frappe.log_error(
                f"Failed to cancel/delete DN {dn_name} for SI {doc.name}: {str(e)}",
                "Cancel Delivery Note Error",
            )
            frappe.msgprint(f"Warning: Could not cancel Delivery Note {dn_name}: {str(e)}")


def create_payment_entry(doc, method):
    if not doc.custom_mobile_app:
        return

    if doc.is_pos and doc.payments and len(doc.payments) > 0:
        return

    if doc.custom_payment_type != "Cash":
        frappe.log_error(
            f"Auto-PE skipped for {doc.name}: custom_payment_type='{doc.custom_payment_type}' (not Cash)",
            "Auto PE Skipped",
        )
        return

    try:
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        if not sales_person:
            frappe.throw(f"No Sales Person found for user {frappe.session.user}. Cannot create payment entry.")

        settings = get_basket4me_settings()
        sales_person_details = None

        for detail in settings.sales_person_details:
            if detail.sales_person == sales_person:
                sales_person_details = detail

        if not sales_person_details:
            frappe.throw(f"No Basket4Me Settings found for sales person {sales_person}. Cannot create payment entry.")

        company = frappe.get_doc('Company', frappe.defaults.get_user_default("Company"))
        mode_of_payment = sales_person_details.mode_of_payment
        if not mode_of_payment:
            frappe.throw("Mode of payment not set for sales person.")

        paid_to_doc = frappe.get_doc("Mode of Payment", mode_of_payment)

        if doc.is_return:
            payment_amount = abs(flt(doc.outstanding_amount)) or abs(flt(doc.grand_total))
        else:
            payment_amount = abs(flt(doc.outstanding_amount)) or abs(flt(doc.grand_total))

        if not payment_amount:
            return

        pe = frappe.new_doc("Payment Entry")

        if doc.is_return:
            pe.payment_type = "Pay"
        else:
            pe.payment_type = "Receive"

        pe.party_type = "Customer"
        pe.party = doc.customer
        pe.paid_amount = payment_amount
        pe.received_amount = payment_amount
        pe.reference_no = 12345
        pe.reference_date = doc.posting_date
        pe.mode_of_payment = mode_of_payment
        pe.paid_to = company.default_receivable_account if doc.is_return else paid_to_doc.accounts[0].default_account
        pe.paid_from = paid_to_doc.accounts[0].default_account if doc.is_return else company.default_receivable_account
        pe.target_exchange_rate = 1
        pe.source_exchange_rate = 1
        pe.custom_sales_person = sales_person
        pe.cost_center = sales_person_details.cost_center

        allocated = -payment_amount if doc.is_return else payment_amount
        pe.append("references", {
            "reference_doctype": "Sales Invoice",
            "reference_name": doc.name,
            "allocated_amount": allocated
        })

        pe.flags.ignore_permissions = True
        pe.save()
        pe.submit()

        frappe.msgprint(f"Payment Entry {pe.name} created and submitted.")
    except Exception as e:
        frappe.log_error(
            f"Auto-PE creation failed for SI {doc.name}: {str(e)}\n{frappe.get_traceback()}",
            "Auto PE Creation Error",
        )
        frappe.throw(f"Failed to create Payment Entry: {str(e)}")
