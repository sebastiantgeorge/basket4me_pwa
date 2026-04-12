import frappe
import json
import base64
import re
from frappe.utils import nowdate, flt
from frappe.query_builder import DocType
from frappe.query_builder.functions import Sum
from frappe.utils.pdf import get_pdf
from frappe.www.printview import get_html_and_style

def strip_html_tags(text):
    """
    Remove HTML tags from text and return clean plain text.
    
    Args:
        text (str): Text that may contain HTML tags
        
    Returns:
        str: Clean text without HTML tags
    """
    if not text:
        return text
    
    # Remove HTML tags using regex
    clean_text = re.sub(r'<[^>]+>', '', str(text))
    
    # Replace common HTML entities
    html_entities = {
        '&amp;': '&',
        '&lt;': '<',
        '&gt;': '>',
        '&quot;': '"',
        '&#39;': "'",
        '&nbsp;': ' ',
        '&copy;': '©',
        '&reg;': '®',
        '&trade;': '™'
    }
    
    for entity, replacement in html_entities.items():
        clean_text = clean_text.replace(entity, replacement)
    
    # Clean up extra whitespace
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    
    return clean_text

def enforce_free_item_rates(sales_invoice):
    """
    Utility function to enforce that free items always have rate = 0
    This function should be called after any ERPNext calculation methods
    that might update item rates.
    
    Args:
        sales_invoice: Sales Invoice document
    """
    changes_made = False
    
    for invoice_item in sales_invoice.items:
        if hasattr(invoice_item, 'is_free_item') and invoice_item.is_free_item:
            # List of all rate/amount fields that need to be set to 0 for free items
            rate_fields = [
                'rate',           # Main selling rate
                'stock_uom_rate', # Stock UOM rate
                'price_list_rate', # Price list rate
                'base_rate',      # Base currency rate
                'base_price_list_rate', # Base price list rate
            ]
            
            amount_fields = [
                'amount',         # Line amount
                'net_amount',     # Net line amount
                'base_amount',    # Base currency amount
                'base_net_amount', # Base net amount
                'stock_uom_amount', # Stock UOM amount
            ]
            
            # Enforce all rate fields to 0
            for field in rate_fields:
                if hasattr(invoice_item, field):
                    current_value = getattr(invoice_item, field, 0)
                    if current_value != 0:
                        frappe.log_error(
                            f"SI: {field}={current_value}→0 for free item {invoice_item.item_code} in {sales_invoice.name or 'New'}",
                            f"Free Item SI Enforcement"
                        )
                        setattr(invoice_item, field, 0)
                        changes_made = True
            
            # Enforce all amount fields to 0
            for field in amount_fields:
                if hasattr(invoice_item, field):
                    current_value = getattr(invoice_item, field, 0)
                    if current_value != 0:
                        setattr(invoice_item, field, 0)
                        changes_made = True
            
            # Reset discount calculations for free items
            if hasattr(invoice_item, 'discount_amount') and invoice_item.discount_amount > 0:
                invoice_item.discount_percentage = 0
                invoice_item.discount_amount = 0
                changes_made = True
                
            if hasattr(invoice_item, 'base_discount_amount') and invoice_item.base_discount_amount > 0:
                invoice_item.base_discount_amount = 0
                changes_made = True
    
    # If any changes were made, recalculate totals
    if changes_made:
        sales_invoice.run_method("calculate_taxes_and_totals")
    
    return changes_made

def enforce_free_item_rates_delivery_note(delivery_note):
    """
    Utility function to enforce that free items in delivery note always have rate = 0
    This function should be called after delivery note creation
    
    Args:
        delivery_note: Delivery Note document
    """
    changes_made = False
    
    for dn_item in delivery_note.items:
        if hasattr(dn_item, 'is_free_item') and dn_item.is_free_item:
            # List of all rate/amount fields that need to be set to 0 for free items
            rate_fields = [
                'rate',           # Main selling rate
                'stock_uom_rate', # Stock UOM rate
                'price_list_rate', # Price list rate
                'base_rate',      # Base currency rate
                'base_price_list_rate', # Base price list rate
            ]
            
            amount_fields = [
                'amount',         # Line amount
                'net_amount',     # Net line amount
                'base_amount',    # Base currency amount
                'base_net_amount', # Base net amount
                'stock_uom_amount', # Stock UOM amount
            ]
            
            # Enforce all rate fields to 0
            for field in rate_fields:
                if hasattr(dn_item, field):
                    current_value = getattr(dn_item, field, 0)
                    if current_value != 0:
                        frappe.log_error(
                            f"DN: {field}={current_value}→0 for free item {dn_item.item_code} in {delivery_note.name or 'New'}",
                            f"Free Item DN Enforcement"
                        )
                        setattr(dn_item, field, 0)
                        changes_made = True
            
            # Enforce all amount fields to 0
            for field in amount_fields:
                if hasattr(dn_item, field):
                    current_value = getattr(dn_item, field, 0)
                    if current_value != 0:
                        setattr(dn_item, field, 0)
                        changes_made = True
            
            # Reset discount calculations for free items
            if hasattr(dn_item, 'discount_amount') and dn_item.discount_amount > 0:
                dn_item.discount_percentage = 0
                dn_item.discount_amount = 0
                changes_made = True
                
            if hasattr(dn_item, 'base_discount_amount') and dn_item.base_discount_amount > 0:
                dn_item.base_discount_amount = 0
                changes_made = True
    
    # If any changes were made, recalculate totals
    if changes_made:
        delivery_note.run_method("calculate_taxes_and_totals")
    
    return changes_made

def get_effective_price_list(customer=None, sales_person=None):
    """
    Determines which price list to use based on Basket4Me Settings configuration.

    Priority (highest to lowest):
        1. Enable Global Price List  → Uses Selling Settings price list for ALL transactions.
           Overrides customer-based and sales person price lists.
        2. Enable Customer Based Price List → Uses customer's default_price_list.
        3. Sales Person Price List → From Sales Person Details table in Basket4Me Settings.
        4. Selling Settings → System default selling price list.
        5. Hard fallback → "Standard Selling"

    If both "Enable Global Price List" and "Enable Customer Based Price List" are enabled,
    Global Price List takes precedence — a single price list is enforced for all transactions.

    Args:
        customer: Customer ID or Name
        sales_person: Sales Person ID
    Returns:
        Price list name
    """
    try:
        # Get settings
        try:
            settings = get_basket4me_settings()
            enable_global = settings.get("enable_global_price_list")
            enable_customer_based = settings.get("enable_customer_based_price_list")
        except:
            enable_global = False
            enable_customer_based = False
            settings = None

        # ── Priority 1: Global Price List (overrides everything) ──
        if enable_global:
            global_price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
            if global_price_list and global_price_list.strip():
                return global_price_list
            return "Standard Selling"

        # ── Priority 2: Customer-Based Price List ──
        if enable_customer_based and customer:
            try:
                customer_price_list = None

                # Try exact name match first
                customer_data = frappe.db.get_value("Customer", customer, "default_price_list")
                if customer_data:
                    customer_price_list = customer_data
                else:
                    # Try customer_name field match
                    customer_by_name = frappe.db.get_value("Customer", {"customer_name": customer}, "default_price_list")
                    if customer_by_name:
                        customer_price_list = customer_by_name
                    else:
                        # Try fuzzy matching with SQL
                        customer_fuzzy = frappe.db.sql("""
                            SELECT default_price_list
                            FROM `tabCustomer`
                            WHERE name = %s
                               OR customer_name = %s
                               OR TRIM(LOWER(customer_name)) = TRIM(LOWER(%s))
                               OR TRIM(LOWER(name)) = TRIM(LOWER(%s))
                            LIMIT 1
                        """, (customer, customer, customer, customer), as_dict=True)

                        if customer_fuzzy and customer_fuzzy[0].get("default_price_list"):
                            customer_price_list = customer_fuzzy[0].get("default_price_list")

                # Only use customer price list if it's not empty/null
                if customer_price_list and customer_price_list.strip():
                    return customer_price_list

            except Exception as e:
                frappe.log_error(f"Price List - Customer lookup error: {str(e)}", "Price List Error")

        # ── Priority 3: Sales Person Price List ──
        if sales_person and settings:
            try:
                if hasattr(settings, 'sales_person_details'):
                    for detail in settings.sales_person_details:
                        if detail.sales_person == sales_person:
                            if hasattr(detail, 'price_list') and detail.price_list and detail.price_list.strip():
                                return detail.price_list
                            break
            except Exception as e:
                frappe.log_error(f"Price List - Sales person lookup error: {str(e)}", "Price List Error")

        # ── Priority 4: System Selling Settings ──
        try:
            standard_price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
            if standard_price_list and standard_price_list.strip():
                return standard_price_list
            return "Standard Selling"
        except:
            return "Standard Selling"

    except Exception as e:
        frappe.log_error(f"Price List - General error: {str(e)}", "Price List Error")
        return "Standard Selling"


@frappe.whitelist(methods="GET")
def get_customer_list_with_effective_price_list(name=None):
    """
    Enhanced customer list that shows the effective price list for each customer
    """
    try:
        # Get current sales person
        current_user = frappe.session.user
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": current_user}, "name")
        
        # Get base customer filters
        override_enabled = should_override_sales_team()
        
        if override_enabled:
            filters = {}
        else:
            filters = get_customer_filters_for_salesperson(sales_person)
        
        fields = ['name', 'customer_name', 'tax_id', 'custom_vat_registration_number', 'default_price_list']

        # Apply name filter if provided
        if name:
            if 'name' in filters and isinstance(filters['name'], list):
                existing_names = filters['name'][1] if filters['name'][0] == 'in' else []
                filters['name'] = ['in', [n for n in existing_names if name.lower() in n.lower()]]
            else:
                filters['name'] = ['like', f'%{name}%']

        # Get customer list
        if override_enabled:
            customer_list = frappe.get_list("Customer", filters=filters, fields=fields, ignore_permissions=True)
        else:
            customer_list = frappe.db.get_list("Customer", filters=filters, fields=fields)

        # Enhance each customer with effective price list
        for customer in customer_list:
            # Set default values for None fields
            if customer.get('tax_id') is None:
                customer['tax_id'] = ""
            if customer.get('custom_vat_registration_number') is None:
                customer['custom_vat_registration_number'] = ""
            if customer.get('default_price_list') is None:
                customer['default_price_list'] = ""
            
            # Get effective price list for this customer
            effective_price_list = get_effective_price_list(customer=customer['name'], sales_person=sales_person)
            customer['effective_price_list'] = effective_price_list
            
            # Add debug info to show price list source
            settings = get_basket4me_settings()
            customer_based_enabled = settings.get("enable_customer_based_price_list", 0)
            
            if customer_based_enabled and customer.get('default_price_list'):
                customer['price_list_source'] = 'customer_default'
            elif sales_person:
                customer['price_list_source'] = 'sales_person'
            else:
                customer['price_list_source'] = 'system_default'

        if customer_list:
            return response("Enhanced Customer List", customer_list, True, 200)
        else:
            return response("No Customer List", [], True, 200)
            
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


@frappe.whitelist(methods="GET") 
def debug_customer_pricing_detailed(customer=None):
    """
    Comprehensive debugging function for customer price list resolution
    """
    try:
        if not customer:
            return response("Customer parameter required", {}, False, 400)
        
        debug_info = {}
        
        # 1. Check Basket4Me Settings
        try:
            settings = get_basket4me_settings()
            debug_info["basket4me_settings"] = {
                "enable_customer_based_price_list": settings.get("enable_customer_based_price_list"),
                "company": settings.get("company"),
                "exists": True
            }
        except Exception as e:
            debug_info["basket4me_settings"] = {"error": str(e), "exists": False}
        
        # 2. Check current user and sales person
        current_user = frappe.session.user
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": current_user}, "name")
        debug_info["user_info"] = {
            "current_user": current_user,
            "sales_person": sales_person
        }
        
        # 3. Customer lookup attempts
        debug_info["customer_lookups"] = {}
        
        # Direct name lookup
        try:
            customer_direct = frappe.db.get_value("Customer", customer, 
                ["name", "customer_name", "default_price_list"], as_dict=True)
            debug_info["customer_lookups"]["direct_name"] = customer_direct
        except Exception as e:
            debug_info["customer_lookups"]["direct_name"] = {"error": str(e)}
        
        # Customer name field lookup
        try:
            customer_by_name = frappe.db.get_value("Customer", {"customer_name": customer}, 
                ["name", "customer_name", "default_price_list"], as_dict=True)
            debug_info["customer_lookups"]["by_customer_name_field"] = customer_by_name
        except Exception as e:
            debug_info["customer_lookups"]["by_customer_name_field"] = {"error": str(e)}
        
        # Fuzzy SQL lookup
        try:
            customer_fuzzy = frappe.db.sql("""
                SELECT name, customer_name, default_price_list 
                FROM `tabCustomer` 
                WHERE name = %s 
                   OR customer_name = %s 
                   OR TRIM(LOWER(customer_name)) = TRIM(LOWER(%s))
                   OR TRIM(LOWER(name)) = TRIM(LOWER(%s))
                LIMIT 3
            """, (customer, customer, customer, customer), as_dict=True)
            debug_info["customer_lookups"]["fuzzy_sql"] = customer_fuzzy
        except Exception as e:
            debug_info["customer_lookups"]["fuzzy_sql"] = {"error": str(e)}
        
        # 4. Test get_effective_price_list function
        try:
            effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)
            debug_info["effective_price_list_result"] = effective_price_list
        except Exception as e:
            debug_info["effective_price_list_error"] = str(e)
        
        # 5. Sales person price list lookup
        if sales_person and 'basket4me_settings' in debug_info and debug_info['basket4me_settings'].get('exists'):
            try:
                settings = get_basket4me_settings()
                sales_person_price_list = None
                for detail in settings.sales_person_details:
                    if detail.sales_person == sales_person:
                        sales_person_price_list = detail.price_list
                        break
                debug_info["sales_person_price_list"] = sales_person_price_list
            except Exception as e:
                debug_info["sales_person_price_list_error"] = str(e)
        
        # 6. System fallback
        try:
            system_fallback = frappe.db.get_single_value("Selling Settings", "selling_price_list")
            debug_info["system_fallback"] = system_fallback or "Standard Selling"
        except Exception as e:
            debug_info["system_fallback_error"] = str(e)
        
        return response("Detailed Customer Pricing Debug", debug_info, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error in detailed debug: {str(e)}\n{frappe.get_traceback()}", "Debug Error")
        return response(f"Debug error: {str(e)}", {}, False, 417)


def should_override_sales_team():
    """
    Check if sales team override is enabled in Basket4Me Settings or Van Sale Settings
    Returns:
        Boolean: True if override is enabled, False otherwise
    """
    try:
        # First try "Basket4Me Settings" (your custom doctype)
        override_enabled = frappe.db.get_single_value("Basket4Me Settings", "override_sales_team_in_customer")
        if override_enabled:
            return True
    except:
        pass
    
    try:
        # Then try "Van Sale Settings" (standard ERPNext, if it exists)
        override_enabled = frappe.db.get_single_value("Van Sale Settings", "override_sales_team_in_customer")
        if override_enabled:
            return True
    except:
        pass
    
    return False

def get_customer_filters_for_salesperson(sales_person=None):
    """
    Get appropriate filters for customer queries based on override settings
    Args:
        sales_person: Sales Person ID
    Returns:
        Dict: Filters to apply to customer queries
    """
    base_filters = {}
    
    # If override is enabled, return no additional filters (show all customers)
    if should_override_sales_team():
        return base_filters
    
    # If override is disabled, filter by sales team
    if sales_person:
        # Get customers where this salesperson is in the sales team
        customers_with_salesperson = frappe.db.sql("""
            SELECT DISTINCT parent 
            FROM `tabSales Team` 
            WHERE sales_person = %s 
            AND parenttype = 'Customer'
        """, (sales_person,), as_dict=True)
        
        if customers_with_salesperson:
            customer_list = [row.parent for row in customers_with_salesperson]
            base_filters['name'] = ['in', customer_list]
        else:
            # If salesperson has no assigned customers, return empty result
            base_filters['name'] = ['in', []]
    else:
        # If no sales person found for user, but override is disabled, return empty result
        # This prevents unauthorized access to customer data
        base_filters['name'] = ['in', []]
    
    return base_filters

@frappe.whitelist(allow_guest=True)
def response(message, data, success, status_code):
    '''method to generates responses of an API
       args:
            message : response message string
            data : json object of the data
            success : True or False depending on the API response
            status_code : status of the request'''
    frappe.clear_messages()
    frappe.local.response["message"] = message
    frappe.local.response["data"] = data
    frappe.local.response["success"] = success
    frappe.local.response["http_status_code"] = status_code


def get_basket4me_settings():
    """Read Basket4Me Settings bypassing both role and user permissions.
    User Permissions on Company links in child tables (Sales Person Details,
    Mode of Payment Details) block normal users from loading the full doc."""
    _orig = frappe.flags.ignore_permissions
    frappe.flags.ignore_permissions = True
    try:
        return frappe.get_doc("Basket4Me Settings")
    finally:
        frappe.flags.ignore_permissions = _orig
    return

@frappe.whitelist(methods="GET")
def get_sales_metrics(from_date=None, to_date=None, sales_person_filter=None):
    """
    Get sales metrics with optional date range and sales person filtering
    
    Args:
        from_date (str): Start date (YYYY-MM-DD format)
        to_date (str): End date (YYYY-MM-DD format)  
        sales_person_filter (str): Specific sales person name (optional)
    """
    today = nowdate()
    
    # Get the sales person for the logged-in user
    current_sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
    
    if not current_sales_person:
        frappe.throw("No Sales Person linked to the logged-in user.")
    
    # Use provided sales person filter or default to current user's sales person
    target_sales_person = sales_person_filter if sales_person_filter else current_sales_person
    
    # Validate date range
    if from_date and not to_date:
        to_date = today
    if to_date and not from_date:
        from_date = "2020-01-01"  # Default start date if only to_date provided
        
    # Build date filter condition
    date_condition = ""
    date_params = []
    
    if from_date and to_date:
        date_condition = " AND si.posting_date BETWEEN %s AND %s"
        date_params = [from_date, to_date]
    
    # Determine which sales invoices to include based on override setting
    if should_override_sales_team():
        # If override is enabled, include all sales invoices
        # But still filter by sales person in sales team to maintain some association
        filters = f"""
            FROM `tabSales Invoice` AS si
            LEFT JOIN `tabSales Team` AS st ON si.name = st.parent
            WHERE si.docstatus = 1 AND (st.sales_person = %s OR st.sales_person IS NULL){date_condition}
        """
    else:
        # Standard behavior - only invoices where salesperson is in sales team
        filters = f"""
            FROM `tabSales Invoice` AS si
            JOIN `tabSales Team` AS st ON si.name = st.parent
            WHERE si.docstatus = 1 AND st.sales_person = %s{date_condition}
        """
    
    # Prepare parameters for SQL queries
    base_params = [target_sales_person] + date_params
    
    total_sales_amount = frappe.db.sql(f"""
        SELECT SUM(si.grand_total) {filters}
    """, base_params)[0][0] or 0.0

    total_due_amount = frappe.db.sql(f"""
        SELECT SUM(si.outstanding_amount) {filters} AND si.outstanding_amount > 0
    """, base_params)[0][0] or 0.0

    total_sales_count = frappe.db.sql(f"""
        SELECT COUNT(si.name) {filters}
    """, base_params)[0][0] or 0

    # Today-specific metrics (always use today's date regardless of date range)
    if should_override_sales_team():
        today_filters = """
            FROM `tabSales Invoice` AS si
            LEFT JOIN `tabSales Team` AS st ON si.name = st.parent
            WHERE si.docstatus = 1 AND (st.sales_person = %s OR st.sales_person IS NULL) AND si.posting_date = %s
        """
    else:
        today_filters = """
            FROM `tabSales Invoice` AS si
            JOIN `tabSales Team` AS st ON si.name = st.parent
            WHERE si.docstatus = 1 AND st.sales_person = %s AND si.posting_date = %s
        """
    
    today_params = [target_sales_person, today]
    
    today_sales_count = frappe.db.sql(f"""
        SELECT COUNT(si.name) {today_filters}
    """, today_params)[0][0] or 0

    today_sales_amount = frappe.db.sql(f"""
        SELECT SUM(si.grand_total) {today_filters}
    """, today_params)[0][0] or 0.0

    today_cash_sales = frappe.db.sql(f"""
        SELECT SUM(si.grand_total) {today_filters} AND si.custom_payment_type = "Cash"
    """, today_params)[0][0] or 0.0

    today_credit_sales = frappe.db.sql(f"""
        SELECT SUM(si.grand_total) {today_filters} AND si.custom_payment_type != "Cash"
    """, today_params)[0][0] or 0.0

    today_payment_received = frappe.db.sql("""
        SELECT SUM(pe.paid_amount)
        FROM `tabPayment Entry` AS pe
        WHERE pe.docstatus = 1
        AND pe.payment_type = "Receive"                     
        AND pe.posting_date = %s 
        AND pe.custom_sales_person = %s
    """, (today, target_sales_person))[0][0] or 0.0

    today_return_payment = frappe.db.sql("""
        SELECT SUM(pe.paid_amount)
        FROM `tabPayment Entry` AS pe
        WHERE pe.docstatus = 1
        AND pe.payment_type = "Pay"                     
        AND pe.posting_date = %s 
        AND pe.custom_sales_person = %s
    """, (today, target_sales_person))[0][0] or 0.0

    currency_precision = frappe.db.get_single_value("System Settings", "currency_precision")

    return {
        "sales_person": target_sales_person,
        "date_range": {
            "from_date": from_date,
            "to_date": to_date
        },
        "total_sales_amount": total_sales_amount,
        "total_due_amount": total_due_amount,
        "total_sales_count": total_sales_count,
        "today_sales_count": today_sales_count,
        "today_sales_amount": today_sales_amount,
        "today_cash_sales": today_cash_sales,
        "today_credit_sales": today_credit_sales,
        "today_payment_received": today_payment_received,
        "today_return_payment": today_return_payment,
        "currency_precision": currency_precision,
        "override_sales_team_enabled": should_override_sales_team(),
    }


@frappe.whitelist(methods="GET")
def get_daily_sales_report():
    """
    Comprehensive daily sales report with MOP-level breakdown.
    Returns today's sales, collections, outstanding, and per-MOP payment totals.
    """
    try:
        today = nowdate()
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")

        if not sales_person:
            frappe.throw("No Sales Person linked to the logged-in user.")

        override = should_override_sales_team()

        # ── Invoice totals (today) ──────────────────────────────────
        if override:
            inv_filter = """
                FROM `tabSales Invoice` si
                LEFT JOIN `tabSales Team` st ON si.name = st.parent
                WHERE si.docstatus = 1 AND si.is_return = 0
                AND si.posting_date = %s
                AND (st.sales_person = %s OR st.sales_person IS NULL)
            """
        else:
            inv_filter = """
                FROM `tabSales Invoice` si
                JOIN `tabSales Team` st ON si.name = st.parent
                WHERE si.docstatus = 1 AND si.is_return = 0
                AND si.posting_date = %s
                AND st.sales_person = %s
            """

        inv_params = [today, sales_person]

        total_sales = frappe.db.sql(
            f"SELECT COALESCE(SUM(si.grand_total), 0) {inv_filter}", inv_params
        )[0][0] or 0

        total_invoices = frappe.db.sql(
            f"SELECT COUNT(DISTINCT si.name) {inv_filter}", inv_params
        )[0][0] or 0

        cash_sales = frappe.db.sql(
            f"SELECT COALESCE(SUM(si.grand_total), 0) {inv_filter} AND si.custom_payment_type = 'Cash'",
            inv_params
        )[0][0] or 0

        credit_sales = frappe.db.sql(
            f"SELECT COALESCE(SUM(si.grand_total), 0) {inv_filter} AND si.custom_payment_type != 'Cash'",
            inv_params
        )[0][0] or 0

        cash_invoices = frappe.db.sql(
            f"SELECT COUNT(DISTINCT si.name) {inv_filter} AND si.custom_payment_type = 'Cash'",
            inv_params
        )[0][0] or 0

        credit_invoices = frappe.db.sql(
            f"SELECT COUNT(DISTINCT si.name) {inv_filter} AND si.custom_payment_type != 'Cash'",
            inv_params
        )[0][0] or 0

        # ── Payment totals (today) ──────────────────────────────────
        payment_received = frappe.db.sql("""
            SELECT COALESCE(SUM(pe.paid_amount), 0)
            FROM `tabPayment Entry` pe
            WHERE pe.docstatus = 1
            AND pe.payment_type = 'Receive'
            AND pe.posting_date = %s
            AND pe.custom_sales_person = %s
        """, (today, sales_person))[0][0] or 0

        return_payment = frappe.db.sql("""
            SELECT COALESCE(SUM(pe.paid_amount), 0)
            FROM `tabPayment Entry` pe
            WHERE pe.docstatus = 1
            AND pe.payment_type = 'Pay'
            AND pe.posting_date = %s
            AND pe.custom_sales_person = %s
        """, (today, sales_person))[0][0] or 0

        net_collection = flt(payment_received) - flt(return_payment)

        # ── MOP breakdown (today's payments) ────────────────────────
        mop_rows = frappe.db.sql("""
            SELECT
                pe.mode_of_payment,
                COALESCE(mop.type, 'Cash') as mop_type,
                SUM(pe.paid_amount) as amount,
                COUNT(*) as count
            FROM `tabPayment Entry` pe
            LEFT JOIN `tabMode of Payment` mop ON mop.name = pe.mode_of_payment
            WHERE pe.docstatus = 1
            AND pe.payment_type = 'Receive'
            AND pe.posting_date = %s
            AND pe.custom_sales_person = %s
            GROUP BY pe.mode_of_payment, mop.type
            ORDER BY amount DESC
        """, (today, sales_person), as_dict=True)

        mop_breakdown = []
        for row in mop_rows:
            mop_breakdown.append({
                "mode_of_payment": row.mode_of_payment or "Unknown",
                "type": row.mop_type or "Cash",
                "amount": flt(row.amount),
                "count": int(row.count or 0),
            })

        # ── Outstanding amount (all time) ───────────────────────────
        if override:
            out_filter = """
                FROM `tabSales Invoice` si
                LEFT JOIN `tabSales Team` st ON si.name = st.parent
                WHERE si.docstatus = 1 AND si.outstanding_amount > 0
                AND (st.sales_person = %s OR st.sales_person IS NULL)
            """
        else:
            out_filter = """
                FROM `tabSales Invoice` si
                JOIN `tabSales Team` st ON si.name = st.parent
                WHERE si.docstatus = 1 AND si.outstanding_amount > 0
                AND st.sales_person = %s
            """

        outstanding_amount = frappe.db.sql(
            f"SELECT COALESCE(SUM(si.outstanding_amount), 0) {out_filter}",
            [sales_person]
        )[0][0] or 0

        # ── Individual payment entries (today) ──────────────────────
        payment_entries = frappe.db.sql("""
            SELECT pe.name, pe.party, pe.party_name, pe.paid_amount,
                   pe.mode_of_payment, pe.payment_type, pe.posting_date,
                   pe.creation
            FROM `tabPayment Entry` pe
            WHERE pe.docstatus = 1
              AND pe.posting_date = %s
              AND pe.custom_sales_person = %s
            ORDER BY pe.creation DESC
        """, (today, sales_person), as_dict=True)

        for pe in payment_entries:
            pe["posting_date"] = str(pe.get("posting_date", ""))
            pe["creation"] = str(pe.get("creation", ""))

        return {
            "date": today,
            "total_sales": flt(total_sales),
            "cash_sales": flt(cash_sales),
            "credit_sales": flt(credit_sales),
            "total_invoices": int(total_invoices),
            "cash_invoices": int(cash_invoices),
            "credit_invoices": int(credit_invoices),
            "payment_received": flt(payment_received),
            "return_payment": flt(return_payment),
            "net_collection": flt(net_collection),
            "mop_breakdown": mop_breakdown,
            "outstanding_amount": flt(outstanding_amount),
            "payment_entries": payment_entries,
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Daily Sales Report Error")
        frappe.throw(str(e))


@frappe.whitelist(methods="GET")
def get_customer_invoice_aging():
    """
    Customer invoice aging summary — buckets: Current, 1-30, 31-60, 61-90, 90+ days overdue.
    Groups by customer and returns per-customer aging with overall totals.
    """
    try:
        today = nowdate()
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        if not sales_person:
            frappe.throw("No Sales Person linked to the logged-in user.")

        override = should_override_sales_team()

        if override:
            join_clause = "LEFT JOIN `tabSales Team` st ON si.name = st.parent"
            sp_condition = "AND (st.sales_person = %s OR st.sales_person IS NULL)"
        else:
            join_clause = "JOIN `tabSales Team` st ON si.name = st.parent"
            sp_condition = "AND st.sales_person = %s"

        rows = frappe.db.sql(f"""
            SELECT
                si.customer,
                si.customer_name,
                si.name AS invoice,
                si.posting_date,
                si.due_date,
                si.grand_total,
                si.outstanding_amount,
                DATEDIFF(%s, si.due_date) AS overdue_days
            FROM `tabSales Invoice` si
            {join_clause}
            WHERE si.docstatus = 1
              AND si.is_return = 0
              AND si.outstanding_amount > 0
              {sp_condition}
            ORDER BY si.customer_name, overdue_days DESC
        """, (today, sales_person), as_dict=True)

        def bucket_name(days):
            if days <= 0:
                return "current"
            elif days <= 30:
                return "1_30"
            elif days <= 60:
                return "31_60"
            elif days <= 90:
                return "61_90"
            else:
                return "90_plus"

        def sensitivity(bucket):
            return {"current": "low", "1_30": "medium", "31_60": "high", "61_90": "critical", "90_plus": "critical"}[bucket]

        # Aggregate by customer
        customer_map = {}
        totals = {"current": 0, "1_30": 0, "31_60": 0, "61_90": 0, "90_plus": 0, "total": 0}

        for r in rows:
            cust = r.customer
            if cust not in customer_map:
                customer_map[cust] = {
                    "customer": cust,
                    "customer_name": r.customer_name,
                    "current": 0, "1_30": 0, "31_60": 0, "61_90": 0, "90_plus": 0,
                    "total": 0,
                    "invoice_count": 0,
                    "max_overdue_days": 0,
                }
            c = customer_map[cust]
            b = bucket_name(r.overdue_days or 0)
            amt = flt(r.outstanding_amount)
            c[b] += amt
            c["total"] += amt
            c["invoice_count"] += 1
            c["max_overdue_days"] = max(c["max_overdue_days"], r.overdue_days or 0)
            totals[b] += amt
            totals["total"] += amt

        # Build customer list sorted by max overdue (worst first)
        customers = sorted(customer_map.values(), key=lambda x: -x["max_overdue_days"])
        for c in customers:
            c["sensitivity"] = sensitivity(bucket_name(c["max_overdue_days"]))
            c["posting_date"] = None  # not needed at customer level

        return response("Customer invoice aging retrieved", {
            "customers": customers,
            "totals": totals,
            "bucket_labels": {
                "current": "Current",
                "1_30": "1-30 Days",
                "31_60": "31-60 Days",
                "61_90": "61-90 Days",
                "90_plus": "90+ Days",
            },
        }, True, 200)

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Customer Invoice Aging Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_invoice_list(name=None, customer=None, status=None, search=None,
                     from_date=None, to_date=None, route=None,
                     page_number=1, page_size=20):
    """
    List Sales Invoices with filters and pagination.

    Query params:
        name: Filter by exact SI name
        customer: Filter by customer
        status: Filter by status (Draft, Unpaid, Paid, Overdue, Cancelled)
        search: Search by name or customer_name
        from_date / to_date: Date range filter on posting_date
        route: Filter by Customer Route
        page_number / page_size: Pagination
    """
    try:
        _page_size = int(page_size or 20)
        _offset = (int(page_number or 1) - 1) * _page_size

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")

        filters = {"is_return": 0}
        fields = ['name', 'customer', 'customer_name', 'posting_date', 'grand_total', 'outstanding_amount', 'status', 'docstatus', 'creation']

        if name:
            filters['name'] = name

        if customer:
            filters['customer'] = customer

        if status:
            if status == "Draft":
                filters['docstatus'] = 0
            elif status == "Cancelled":
                filters['docstatus'] = 2
            else:
                filters['docstatus'] = 1
                filters['status'] = status

        # Route filter - get customers belonging to this route
        route_customers = None
        if route:
            route_customers = frappe.get_all("Customer", filters={"custom_route": route}, pluck="name")
            if route_customers:
                if not customer:
                    filters["customer"] = ["in", route_customers]
            else:
                return response("Invoice List", {"invoices": [], "total_count": 0, "page_number": int(page_number or 1), "page_size": _page_size}, True, 200)

        # Check if override is enabled
        override_enabled = should_override_sales_team()

        if override_enabled:
            conditions = ["si.is_return = 0"]
            values = []

            if name:
                conditions.append("si.name = %s")
                values.append(name)
            if customer:
                conditions.append("si.customer = %s")
                values.append(customer)
            elif route_customers:
                placeholders = ",".join(["%s"] * len(route_customers))
                conditions.append(f"si.customer IN ({placeholders})")
                values.extend(route_customers)
            if status:
                if status == "Draft":
                    conditions.append("si.docstatus = 0")
                elif status == "Cancelled":
                    conditions.append("si.docstatus = 2")
                else:
                    conditions.append("si.docstatus = 1")
                    conditions.append("si.status = %s")
                    values.append(status)
            if search:
                conditions.append("(si.name LIKE %s OR si.customer_name LIKE %s)")
                values.append(f"%{search}%")
                values.append(f"%{search}%")
            if from_date:
                conditions.append("si.posting_date >= %s")
                values.append(from_date)
            if to_date:
                conditions.append("si.posting_date <= %s")
                values.append(to_date)

            where_clause = " AND ".join(conditions)

            # Get total count
            count_sql = f"SELECT COUNT(*) FROM `tabSales Invoice` si WHERE {where_clause}"
            total_count = frappe.db.sql(count_sql, values)[0][0]

            sql = f"""
                SELECT si.name, si.customer, si.customer_name, si.posting_date, si.grand_total, si.outstanding_amount, si.status, si.docstatus, si.creation,
                       c.mobile_no
                FROM `tabSales Invoice` si
                LEFT JOIN `tabCustomer` c ON si.customer = c.name
                WHERE {where_clause}
                ORDER BY si.creation DESC
                LIMIT %s OFFSET %s
            """
            values.extend([_page_size, _offset])

            invoice_list = frappe.db.sql(sql, values, as_dict=True)
            
        else:
            # If override is disabled, add sales team restriction
            if sales_person:
                # Get invoices where this salesperson is in the sales team
                invoice_names = frappe.db.sql("""
                    SELECT DISTINCT parent 
                    FROM `tabSales Team` 
                    WHERE sales_person = %s 
                    AND parenttype = 'Sales Invoice'
                """, (sales_person,), as_dict=True)
                
                if invoice_names:
                    invoice_list = [row.parent for row in invoice_names]
                    if 'name' in filters:
                        # Intersect with existing name filter
                        if isinstance(filters['name'], str):
                            filters['name'] = filters['name'] if filters['name'] in invoice_list else ''
                        else:
                            filters['name'] = ['in', invoice_list]
                    else:
                        filters['name'] = ['in', invoice_list]
                else:
                    # No invoices for this salesperson
                    filters['name'] = ['in', []]

            # Add date filters
            if from_date and to_date:
                filters["posting_date"] = ["between", [from_date, to_date]]
            elif from_date:
                filters["posting_date"] = [">=", from_date]
            elif to_date:
                filters["posting_date"] = ["<=", to_date]

            total_count = frappe.db.count("Sales Invoice", filters=filters)

            # Use Frappe's get_list with sales team restrictions and pagination
            invoice_list = frappe.db.get_list("Sales Invoice", filters=filters, fields=fields,
                order_by="creation desc", limit_start=_offset, limit_page_length=_page_size)

        # Apply text search filter (LIKE match on name and customer_name)
        if search and invoice_list:
            search_lower = search.lower()
            invoice_list = [inv for inv in invoice_list if search_lower in (inv.get('name') or '').lower() or search_lower in (inv.get('customer_name') or '').lower()]

        # ── Compute return_status for each invoice (batch) ──
        if invoice_list:
            inv_names = [inv.get('name') for inv in invoice_list if inv.get('name')]
            if inv_names:
                # Original stock_qty per (invoice, item_code)
                orig_rows = frappe.db.sql("""
                    SELECT parent, item_code, SUM(stock_qty) as total_stock_qty
                    FROM `tabSales Invoice Item`
                    WHERE parent IN %s
                    GROUP BY parent, item_code
                """, (inv_names,), as_dict=True)

                orig_map = {}  # {invoice: {item_code: stock_qty}}
                for r in orig_rows:
                    orig_map.setdefault(r.parent, {})[r.item_code] = flt(r.total_stock_qty)

                # Returned stock_qty from submitted returns only (docstatus=1)
                ret_rows = frappe.db.sql("""
                    SELECT si.return_against, sii.item_code, SUM(ABS(sii.stock_qty)) as returned_qty
                    FROM `tabSales Invoice` si
                    JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
                    WHERE si.is_return = 1 AND si.docstatus = 1 AND si.return_against IN %s
                    GROUP BY si.return_against, sii.item_code
                """, (inv_names,), as_dict=True)

                ret_map = {}  # {invoice: {item_code: returned_qty}}
                for r in ret_rows:
                    ret_map.setdefault(r.return_against, {})[r.item_code] = flt(r.returned_qty)

                for inv in invoice_list:
                    name = inv.get('name')
                    orig_items = orig_map.get(name, {})
                    ret_items = ret_map.get(name, {})
                    if not ret_items:
                        inv['return_status'] = 'not_returned'
                    else:
                        all_fully = True
                        for ic, orig_qty in orig_items.items():
                            if flt(ret_items.get(ic, 0)) < flt(orig_qty) - 0.01:
                                all_fully = False
                                break
                        inv['return_status'] = 'fully_returned' if all_fully else 'partially_returned'

        # ── Enrich with mobile_no if missing (non-override path) ──
        if invoice_list:
            needs_mobile = any('mobile_no' not in inv for inv in invoice_list)
            if needs_mobile:
                customer_names = list(set(inv.get('customer') for inv in invoice_list if inv.get('customer')))
                mobile_map = {}
                if customer_names:
                    mobiles = frappe.db.sql(
                        "SELECT name, mobile_no FROM `tabCustomer` WHERE name IN %s",
                        (customer_names,), as_dict=True
                    )
                    for m in mobiles:
                        if m.get('mobile_no'):
                            mobile_map[m['name']] = m['mobile_no']
                for inv in invoice_list:
                    inv['mobile_no'] = mobile_map.get(inv.get('customer'), '')

        # ── Enrich with customer_address, route, created_by, items ──
        if invoice_list:
            for inv in invoice_list:
                cust = inv.get('customer')

                # Customer address
                addr_name = frappe.db.get_value("Dynamic Link",
                    {"link_doctype": "Customer", "link_name": cust, "parenttype": "Address"},
                    "parent") if cust else None
                if addr_name:
                    addr = frappe.db.get_value("Address", addr_name,
                        ["address_line1", "address_line2", "city", "state", "pincode", "country"],
                        as_dict=True)
                    if addr:
                        parts = [addr.address_line1, addr.address_line2, addr.city, addr.state, addr.pincode, addr.country]
                        inv["customer_address"] = ", ".join([p for p in parts if p])
                    else:
                        inv["customer_address"] = None
                else:
                    inv["customer_address"] = None

                # Route
                inv["route"] = frappe.db.get_value("Customer", cust, "custom_route") if cust else None

                # Created by
                owner = inv.get("owner") or frappe.db.get_value("Sales Invoice", inv["name"], "owner")
                inv["created_by"] = frappe.db.get_value("User", owner, "full_name") or owner if owner else None

                # Items array
                inv["items"] = frappe.get_all(
                    "Sales Invoice Item",
                    filters={"parent": inv["name"]},
                    fields=[
                        "item_code", "item_name", "qty", "uom", "stock_uom",
                        "rate", "amount", "price_list_rate",
                        "discount_percentage", "discount_amount",
                        "conversion_factor"
                    ]
                )

        return response("Invoice List", {
            "invoices": invoice_list or [],
            "total_count": total_count if 'total_count' in dir() else len(invoice_list or []),
            "page_number": int(page_number or 1),
            "page_size": _page_size,
        }, True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_invoice_detail(name=None):
    """Get full detail of a single Sales Invoice including items."""
    try:
        if not name:
            return response("Invoice name is required", {}, False, 400)

        doc = frappe.get_doc("Sales Invoice", name)

        items = []
        for item in doc.items:
            tax_rate = 0.0
            if item.item_tax_template:
                tax_rate = frappe.db.get_value(
                    "Item Tax Template Detail",
                    {"parent": item.item_tax_template},
                    "tax_rate"
                ) or 0.0
            # Get available UOMs for this item (for return UOM selection)
            available_uoms = frappe.get_all(
                "UOM Conversion Detail",
                filters={"parent": item.item_code, "parenttype": "Item"},
                fields=["uom", "conversion_factor"],
            )

            items.append({
                "item_code": item.item_code,
                "item_name": item.item_name,
                "description": item.description,
                "qty": item.qty,
                "uom": item.uom,
                "stock_qty": item.stock_qty,
                "stock_uom": item.stock_uom,
                "conversion_factor": item.conversion_factor,
                "rate": item.rate,
                "price_list_rate": item.price_list_rate,
                "discount_percentage": item.discount_percentage or 0,
                "discount_amount": item.discount_amount or 0,
                "amount": item.amount,
                "is_free_item": item.is_free_item or False,
                "tax_rate": float(tax_rate),
                "tax_amount": float(getattr(item, 'tax_amount', 0) or 0),
                "available_uoms": [{"uom": u.uom, "conversion_factor": u.conversion_factor} for u in available_uoms],
                "batch_no": getattr(item, 'batch_no', None) or "",
                "serial_and_batch_bundle": getattr(item, 'serial_and_batch_bundle', None) or "",
                "has_batch_no": frappe.db.get_value("Item", item.item_code, "has_batch_no") or 0,
            })

        data = {
            "name": doc.name,
            "customer": doc.customer,
            "customer_name": doc.customer_name,
            "posting_date": str(doc.posting_date) if doc.posting_date else None,
            "due_date": str(doc.due_date) if doc.due_date else None,
            "docstatus": doc.docstatus,
            "status": doc.status,
            "custom_payment_type": doc.custom_payment_type,
            "custom_sales_person": getattr(doc, "custom_sales_person", None),
            "company": doc.company,
            "is_return": doc.is_return,
            "return_against": doc.return_against,
            "items": items,
            "total": doc.total,
            "net_total": doc.net_total,
            "total_taxes_and_charges": doc.total_taxes_and_charges,
            "discount_amount": doc.discount_amount or 0,
            "grand_total": doc.grand_total,
            "outstanding_amount": doc.outstanding_amount,
            "creation": str(doc.creation) if doc.creation else None,
            "mobile_no": frappe.db.get_value("Customer", doc.customer, "mobile_no") or "",
        }

        return response("Invoice Detail", data, True, 200)

    except frappe.DoesNotExistError:
        return response(f"Sales Invoice '{name}' not found", {}, False, 404)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Invoice Detail Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_customer_list(name=None, mobile_no=None):
    try:
        override_enabled = should_override_sales_team()
        fields = ['name', 'customer_name', 'mobile_no', 'tax_id', 'custom_vat_registration_number', 'default_price_list']
        ignore_perms = True if override_enabled else False

        if name:
            # Search both customer_name and name (ID) fields using OR
            or_filters = [
                ['customer_name', 'like', f'%{name}%'],
                ['name', 'like', f'%{name}%']
            ]
            customer_list = frappe.get_list(
                "Customer", or_filters=or_filters, fields=fields,
                ignore_permissions=ignore_perms, limit_page_length=20,
                order_by="customer_name asc"
            )
        elif mobile_no:
            # Search by mobile number
            customer_list = frappe.get_list(
                "Customer", filters={'mobile_no': ['like', f'%{mobile_no}%']},
                fields=fields, ignore_permissions=ignore_perms,
                limit_page_length=20, order_by="customer_name asc"
            )
        else:
            # No search term — return recent customers
            customer_list = frappe.get_list(
                "Customer", fields=fields, ignore_permissions=ignore_perms,
                limit_page_length=20, order_by="modified desc"
            )

        for customer in customer_list:
            if customer.get('tax_id') is None:
                customer['tax_id'] = ""
            if customer.get('custom_vat_registration_number') is None:
                customer['custom_vat_registration_number'] = ""
            if customer.get('default_price_list') is None:
                customer['default_price_list'] = ""

        if customer_list:
            return response("Customer List", customer_list, True, 200)
        else:
            return response("No Customer List", [], True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


@frappe.whitelist(methods="POST")
def enable_customer_override():
    """Enable the customer override setting in Basket4Me Settings"""
    try:
        # Check if user has permission
        if not frappe.has_permission("Basket4Me Settings", "write"):
            return response("Permission denied", {}, False, 403)
        
        # Try to get or create the Basket4Me Settings document
        try:
            doc = get_basket4me_settings()
        except frappe.DoesNotExistError:
            # Create new document if it doesn't exist
            doc = frappe.new_doc("Basket4Me Settings")
            doc.name = "Basket4Me Settings"

        # Enable the override setting
        doc.override_sales_team_in_customer = 1
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        
        frappe.log_error(f"Customer override enabled by user: {frappe.session.user}", "Override Setting")
        return response("Customer override enabled successfully", {"override_enabled": True}, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error enabling customer override: {str(e)}\n{frappe.get_traceback()}", "Override Setting Error")
        return response(f"Error enabling override: {str(e)}", {}, False, 417)


@frappe.whitelist(methods="POST")
def enable_customer_based_price_list():
    """Enable the customer-based price list setting in Basket4Me Settings"""
    try:
        settings = get_basket4me_settings()
        settings.enable_customer_based_price_list = 1
        settings.save(ignore_permissions=True)
        frappe.db.commit()
        
        frappe.log_error(f"Customer-based price list enabled by user: {frappe.session.user}", "Settings Update")
        return response("Customer-based price list enabled successfully", {
            "enable_customer_based_price_list": 1,
            "message": "Setting updated successfully"
        }, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error enabling customer-based price list: {str(e)}\n{frappe.get_traceback()}", "Settings Error")
        return response(f"Error enabling setting: {str(e)}", {}, False, 417)


@frappe.whitelist()
def quick_enable_price_list():
    """Quick enable customer-based price list using GET method"""
    try:
        # Using raw SQL to bypass permissions
        frappe.db.sql("""
            UPDATE `tabBasket4Me Settings` 
            SET enable_customer_based_price_list = 1 
            WHERE name = 'Basket4Me Settings'
        """)
        frappe.db.commit()
        
        # Verify the change
        result = frappe.db.sql("""
            SELECT enable_customer_based_price_list 
            FROM `tabBasket4Me Settings` 
            WHERE name = 'Basket4Me Settings'
        """, as_dict=True)
        
        frappe.log_error(f"Quick enable price list by user: {frappe.session.user}", "Settings Update")
        return response("Customer-based price list enabled via SQL", {
            "enable_customer_based_price_list": result[0].get('enable_customer_based_price_list') if result else 0,
            "method": "direct_sql_update"
        }, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error in quick enable: {str(e)}\n{frappe.get_traceback()}", "Settings Error")
        return response(f"Error: {str(e)}", {}, False, 417)


@frappe.whitelist()
def force_enable_price_list():
    """Force enable customer-based price list with multiple approaches"""
    try:
        results = {}
        
        # Approach 1: Direct SQL update
        frappe.db.sql("""
            UPDATE `tabBasket4Me Settings` 
            SET enable_customer_based_price_list = 1 
            WHERE name = 'Basket4Me Settings'
        """)
        frappe.db.commit()
        results["sql_update"] = "executed"
        
        # Approach 2: Check if record exists, create if not
        exists = frappe.db.sql("""
            SELECT name FROM `tabBasket4Me Settings` 
            WHERE name = 'Basket4Me Settings'
        """)
        
        if not exists:
            # Create the settings record if it doesn't exist
            frappe.db.sql("""
                INSERT INTO `tabBasket4Me Settings` 
                (name, enable_customer_based_price_list, docstatus, idx) 
                VALUES ('Basket4Me Settings', 1, 0, 1)
            """)
            results["record_created"] = True
        else:
            results["record_exists"] = True
            
        frappe.db.commit()
        
        # Approach 3: Clear any cache
        frappe.clear_cache()
        results["cache_cleared"] = True
        
        # Verify the final state
        final_check = frappe.db.sql("""
            SELECT enable_customer_based_price_list, name 
            FROM `tabBasket4Me Settings` 
            WHERE name = 'Basket4Me Settings'
        """, as_dict=True)
        
        results["final_state"] = final_check[0] if final_check else "no_record"
        
        frappe.log_error(f"Force enable executed by: {frappe.session.user}", "Force Enable")
        return response("Force enable completed", results, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error in force enable: {str(e)}\n{frappe.get_traceback()}", "Force Enable Error")
        return response(f"Error: {str(e)}", {}, False, 417)


@frappe.whitelist()
def comprehensive_debug():
    """Comprehensive debug of the price list system"""
    try:
        debug_info = {}
        
        # Check Basket4Me Settings
        settings_query = frappe.db.sql("""
            SELECT * FROM `tabBasket4Me Settings` 
            WHERE name = 'Basket4Me Settings'
        """, as_dict=True)
        debug_info["basket4me_settings"] = settings_query[0] if settings_query else "NOT_FOUND"
        
        # Check customer
        customer_name = "Al Jawaher Island Sweets Company"
        customer_query = frappe.db.sql("""
            SELECT name, customer_name, default_price_list 
            FROM `tabCustomer` 
            WHERE name = %s OR customer_name = %s
        """, (customer_name, customer_name), as_dict=True)
        debug_info["customer"] = customer_query[0] if customer_query else "NOT_FOUND"
        
        # Check price list
        if customer_query:
            price_list = customer_query[0].get('default_price_list')
            if price_list:
                price_query = frappe.db.sql("""
                    SELECT item_code, price_list, price_list_rate 
                    FROM `tabItem Price` 
                    WHERE price_list = %s 
                    LIMIT 5
                """, (price_list,), as_dict=True)
                debug_info["sample_prices"] = price_query
        
        # Test the get_effective_price_list function logic
        try:
            from basket4me_pwa.api import get_effective_price_list
            effective_result = get_effective_price_list(customer_name)
            debug_info["effective_price_list_result"] = effective_result
        except Exception as func_error:
            debug_info["effective_price_list_error"] = str(func_error)
        
        # Check if there are any cached values
        debug_info["cache_status"] = "cleared" if frappe.clear_cache() else "error"
        
        return response("Comprehensive debug completed", debug_info, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error in comprehensive debug: {str(e)}\n{frappe.get_traceback()}", "Debug Error")
        return response(f"Debug error: {str(e)}", {}, False, 417)


@frappe.whitelist(methods="GET")
def test_price_list(customer=None):
    """Test function to debug price list resolution"""
    try:
        current_user = frappe.session.user
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": current_user}, "name")
        
        # Get effective price list
        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)
        
        return response("Price List Test", {
            "customer": customer,
            "sales_person": sales_person,
            "effective_price_list": effective_price_list,
            "current_user": current_user
        }, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error in test_price_list: {str(e)}\n{frappe.get_traceback()}", "Test Price List Error")
        return response(f"Error: {str(e)}", {}, False, 417)


@frappe.whitelist(methods="GET")
def debug_customer_price_list(customer=None):
    """Debug function to check customer price list directly"""
    try:
        if not customer:
            return response("Customer parameter required", {}, False, 400)
            
        # Direct database lookup
        customer_data = frappe.db.get_value("Customer", customer, ["name", "customer_name", "default_price_list"], as_dict=True)
        
        # Also try by customer_name in case the lookup is using the wrong field
        customer_by_name = frappe.db.get_value("Customer", {"customer_name": customer}, ["name", "customer_name", "default_price_list"], as_dict=True)
        
        return response("Customer Debug", {
            "search_term": customer,
            "by_name_field": customer_data,
            "by_customer_name_field": customer_by_name,
        }, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error in debug_customer_price_list: {str(e)}\n{frappe.get_traceback()}", "Debug Customer Error")
        return response(f"Error: {str(e)}", {}, False, 417)


@frappe.whitelist(methods="GET")
def debug_basket4me_settings():
    """Debug function to check Basket4Me Settings"""
    try:
        settings = get_basket4me_settings()
        
        return response("Basket4Me Settings Debug", {
            "enable_customer_based_price_list": settings.get("enable_customer_based_price_list"),
            "company": settings.get("company"),
            "sales_person_details_count": len(settings.get("sales_person_details", [])),
            "settings_name": settings.name,
        }, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error in debug_basket4me_settings: {str(e)}\n{frappe.get_traceback()}", "Debug Settings Error")
        return response(f"Error: {str(e)}", {}, False, 417)

    

@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_item_by_barcode(barcode=None):
    """Look up an item by scanning its barcode. Returns the item_code and barcode UOM."""
    try:
        if not barcode:
            return response("Barcode is required", {}, False, 400)

        result = frappe.db.get_value(
            "Item Barcode",
            {"barcode": barcode},
            ["parent", "uom"],
            as_dict=True
        )

        if result:
            return response("Item found by barcode", {
                "item_code": result.parent,
                "barcode_uom": result.uom or ""
            }, True, 200)
        else:
            return response("Barcode not found", {}, False, 404)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Get Item By Barcode Error")
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_item_list(name=None, item_name=None, customer=None, limit_start=0, limit_page_length=20):
    try:
        filters = {"custom_allow_mobile_app": 1, "disabled": 0}
        fields = ['name', 'item_name', "description", "stock_uom", "has_batch_no"]

        if name:
            # Try exact match first (for barcode lookups), then fall back to LIKE
            exact_filters = {**filters, 'name': name}
            item_list = frappe.db.get_list("Item", filters=exact_filters, fields=fields,
                                         limit_start=0, limit_page_length=1)
            if not item_list:
                filters['name'] = ["like", f"%{name}%"]
                item_list = frappe.db.get_list("Item", filters=filters, fields=fields,
                                             limit_start=int(limit_start),
                                             limit_page_length=int(limit_page_length))
        elif item_name:
            filters['item_name'] = ["like", f"%{item_name}%"]
            item_list = frappe.db.get_list("Item", filters=filters, fields=fields,
                                         limit_start=int(limit_start),
                                         limit_page_length=int(limit_page_length))
        else:
            item_list = frappe.db.get_list("Item", filters=filters, fields=fields,
                                         limit_start=int(limit_start),
                                         limit_page_length=int(limit_page_length))
        
        # Get settings safely
        include_tax = 0
        settings = None
        try:
            include_tax = frappe.db.get_value("Basket4Me Settings", None, "is_this_tax_included_in_basic_rate") or 0
            settings = get_basket4me_settings()
        except Exception as e:
            frappe.log_error(f"Error getting Basket4Me Settings: {str(e)}", "Item List Error")

        # Get sales person
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")

        # Get effective price list - this is working correctly
        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)
        
        # Get sales person details safely with better error handling
        sales_person_warehouse = None
        sales_person_details = None
        
        if settings and sales_person:
            try:
                # Check if sales_person_details exists and has items
                if hasattr(settings, 'sales_person_details') and settings.sales_person_details:
                    for detail in settings.sales_person_details:
                        if detail.sales_person == sales_person:
                            sales_person_details = detail
                            # Safely get warehouse with multiple fallbacks
                            if hasattr(detail, 'warehouse'):
                                sales_person_warehouse = detail.warehouse
                            elif hasattr(detail, 'default_warehouse'):
                                sales_person_warehouse = detail.default_warehouse
                            break
                            
                frappe.log_error(f"Sales person warehouse found: {sales_person_warehouse}", "Item List Debug")
                        
            except Exception as e:
                frappe.log_error(f"Error getting sales person warehouse: {str(e)}", "Item List Error")

        # Process each item with robust error handling
        for item in item_list:
            try:
                # Strip HTML tags from description
                if 'description' in item and item['description']:
                    item['description'] = strip_html_tags(item['description'])
                
                # Handle warehouse lookup safely
                available_qty = 0
                if sales_person_warehouse:
                    try:
                        available_qty = frappe.db.get_value("Bin", 
                            {"item_code": item["name"], "warehouse": sales_person_warehouse}, 
                            "actual_qty") or 0
                    except Exception as e:
                        frappe.log_error(f"Error getting stock for {item['name']}: {str(e)}", "Item List Error")

                # Get UOM details safely (including conversion_factor)
                # Use db.sql to bypass permission checks on child doctype
                uom_details = []
                try:
                    uom_details = frappe.db.sql(
                        """SELECT uom, conversion_factor
                        FROM `tabUOM Conversion Detail`
                        WHERE parent = %(parent)s AND parenttype = 'Item'
                        ORDER BY idx ASC""",
                        {"parent": item["name"]},
                        as_dict=True
                    )
                except Exception as e:
                    frappe.log_error(f"Error getting UOM for {item['name']}: {str(e)}", "Item List Error")

                # If no UOM conversion details, try to get stock UOM
                if not uom_details:
                    try:
                        stock_uom = frappe.db.get_value("Item", item["name"], "stock_uom")
                        if stock_uom:
                            uom_details = [{"uom": stock_uom, "conversion_factor": 1}]
                    except Exception as e:
                        frappe.log_error(f"Error getting stock UOM for {item['name']}: {str(e)}", "Item List Error")
                
                uoms_with_prices = []
                for uom_detail in uom_details:
                    try:
                        uom = uom_detail.get("uom")
                        if not uom:
                            continue
                            
                        # Get price safely
                        price = frappe.db.sql(
                            """
                            SELECT price_list_rate 
                            FROM `tabItem Price`
                            WHERE selling = 1 
                            AND item_code = %(item_code)s 
                            AND uom = %(uom)s 
                            AND price_list = %(price_list)s
                            ORDER BY creation DESC
                            LIMIT 1
                            """,
                            {"item_code": item["name"], "uom": uom, "price_list": effective_price_list},
                            as_dict=True
                        )

                        price_value = price[0]["price_list_rate"] if price else 0.0
                        cf = uom_detail.get("conversion_factor", 1)
                        uoms_with_prices.append({"uom": uom, "price": price_value, "conversion_factor": cf})
                        
                    except Exception as e:
                        frappe.log_error(f"Error getting price for {item['name']} - {uom_detail}: {str(e)}", "Item List Error")
                        # Add a default entry to prevent empty UOM list
                        uoms_with_prices.append({"uom": "error", "price": 0.0, "conversion_factor": 1})

                # Get tax rate safely
                tax_rate = 0.0
                try:
                    tax_rate = get_item_tax_rate(item["name"]) or 0.0
                except Exception as e:
                    frappe.log_error(f"Error getting tax rate for {item['name']}: {str(e)}", "Item List Error")

                # Get valuation rate (incoming/cost rate) for incoming rate display
                valuation_rate = 0.0
                last_purchase_rate = 0.0
                try:
                    valuation_rate = frappe.db.get_value("Item", item["name"], "valuation_rate") or 0.0
                except Exception:
                    pass

                # Get purchase rate from Bin valuation_rate for the sales person's warehouse
                if sales_person_warehouse:
                    try:
                        bin_valuation = frappe.db.get_value("Bin",
                            {"item_code": item["name"], "warehouse": sales_person_warehouse},
                            "valuation_rate") or 0.0
                        last_purchase_rate = bin_valuation
                    except Exception:
                        pass

                # Get last customer rate from SO and SI (whichever is most recent)
                last_customer_rate = 0.0
                if customer:
                    try:
                        last_rate = frappe.db.sql("""
                            SELECT rate, txn_date FROM (
                                SELECT soi.rate, so2.transaction_date as txn_date, so2.creation
                                FROM `tabSales Order Item` soi
                                JOIN `tabSales Order` so2 ON so2.name = soi.parent
                                WHERE so2.customer = %s AND soi.item_code = %s
                                AND so2.docstatus != 2
                                UNION ALL
                                SELECT sii.rate, si.posting_date as txn_date, si.creation
                                FROM `tabSales Invoice Item` sii
                                JOIN `tabSales Invoice` si ON si.name = sii.parent
                                WHERE si.customer = %s AND sii.item_code = %s
                                AND si.docstatus = 1 AND si.is_return = 0
                            ) combined
                            ORDER BY txn_date DESC, creation DESC LIMIT 1
                        """, (customer, item["name"], customer, item["name"]), as_dict=True)
                        if last_rate:
                            last_customer_rate = last_rate[0].get("rate") or 0.0
                    except Exception:
                        pass

                # MRP - from "MRP" price list based on default UOM
                mrp = 0.0
                try:
                    mrp_result = frappe.db.sql("""
                        SELECT price_list_rate
                        FROM `tabItem Price`
                        WHERE selling = 1 AND item_code = %s
                        AND price_list = 'MRP'
                        AND (uom = %s OR uom IS NULL OR uom = '')
                        ORDER BY creation DESC LIMIT 1
                    """, (item["name"], item.get("stock_uom", "Nos")), as_dict=True)
                    if mrp_result:
                        mrp = mrp_result[0].get("price_list_rate") or 0.0
                except Exception:
                    pass

                # Standard Selling Price - from "Standard Selling" price list based on default UOM
                standard_selling_price = 0.0
                try:
                    std_result = frappe.db.sql("""
                        SELECT price_list_rate
                        FROM `tabItem Price`
                        WHERE selling = 1 AND item_code = %s
                        AND price_list = 'Standard Selling'
                        AND (uom = %s OR uom IS NULL OR uom = '')
                        ORDER BY creation DESC LIMIT 1
                    """, (item["name"], item.get("stock_uom", "Nos")), as_dict=True)
                    if std_result:
                        standard_selling_price = std_result[0].get("price_list_rate") or 0.0
                except Exception:
                    pass

                # Set item properties safely
                item["default_uom"] = item.get("stock_uom", "Nos")
                item["uoms"] = uoms_with_prices
                item["tax_rate"] = tax_rate
                item["is_tax_included"] = include_tax
                item["available_qty"] = available_qty
                item["effective_price_list"] = effective_price_list
                item["valuation_rate"] = valuation_rate
                item["last_purchase_rate"] = last_purchase_rate
                item["last_customer_rate"] = last_customer_rate
                item["mrp"] = mrp
                item["standard_selling_price"] = standard_selling_price

            except Exception as e:
                frappe.log_error(f"Error processing item {item.get('name', 'unknown')}: {str(e)}", "Item List Error")
                # Set safe defaults
                item["default_uom"] = item.get("stock_uom", "Nos")
                item["uoms"] = []
                item["tax_rate"] = 0.0
                item["is_tax_included"] = include_tax
                item["available_qty"] = 0
                item["effective_price_list"] = effective_price_list
                item["valuation_rate"] = 0.0
                item["last_purchase_rate"] = 0.0
                item["last_customer_rate"] = 0.0
                item["mrp"] = 0.0
                item["standard_selling_price"] = 0.0

        if item_list:
            return response("Item List", item_list, True, 200)
        else:
            return response("No items found", [], True, 200)

    except Exception as exception:
        frappe.log_error(f"CRITICAL ERROR in get_item_list: {str(exception)}\n{frappe.get_traceback()}", "Get Item List Critical Error")
        return response(str(exception), {}, False, 417)



@frappe.whitelist(methods=["GET"])
def get_available_batches(item_code=None, warehouse=None):
    """Get available batches for an item at a warehouse, sorted by FEFO (First Expiry First Out)."""
    try:
        if not item_code:
            return response("item_code is required", [], False, 400)

        params = {"item_code": item_code}
        warehouse_filter_sbe = ""
        warehouse_filter_sle = ""
        if warehouse:
            warehouse_filter_sbe = "AND sbe.warehouse = %(warehouse)s"
            warehouse_filter_sle = "AND sle.warehouse = %(warehouse)s"
            params["warehouse"] = warehouse

        # ERPNext v15+: Query via Serial and Batch Bundle / Entry tables
        # Note: sbe.qty is already signed (positive=inward, negative=outward)
        batches = frappe.db.sql("""
            SELECT
                sbe.batch_no,
                b.expiry_date,
                b.manufacturing_date,
                SUM(sbe.qty) as available_qty
            FROM `tabSerial and Batch Entry` sbe
            JOIN `tabSerial and Batch Bundle` sbb
                ON sbb.name = sbe.parent
                AND sbb.docstatus = 1
                AND sbb.is_cancelled = 0
            JOIN `tabBatch` b
                ON b.name = sbe.batch_no
                AND b.disabled = 0
            WHERE sbe.item_code = %(item_code)s
                {warehouse_filter_sbe}
            GROUP BY sbe.batch_no
            HAVING available_qty > 0
            ORDER BY
                CASE WHEN b.expiry_date IS NULL THEN 1 ELSE 0 END,
                b.expiry_date ASC,
                b.creation ASC
        """.format(warehouse_filter_sbe=warehouse_filter_sbe), params, as_dict=True)

        # Fallback: legacy SLE-based batch tracking (older ERPNext or items without bundles)
        if not batches:
            batches = frappe.db.sql("""
                SELECT
                    b.name as batch_no,
                    b.expiry_date,
                    b.manufacturing_date,
                    COALESCE(SUM(sle.actual_qty), 0) as available_qty
                FROM `tabBatch` b
                LEFT JOIN `tabStock Ledger Entry` sle
                    ON sle.batch_no = b.name
                    AND sle.item_code = %(item_code)s
                    AND sle.is_cancelled = 0
                    {warehouse_filter_sle}
                WHERE b.item = %(item_code)s
                    AND b.disabled = 0
                GROUP BY b.name
                HAVING available_qty > 0
                ORDER BY
                    CASE WHEN b.expiry_date IS NULL THEN 1 ELSE 0 END,
                    b.expiry_date ASC,
                    b.creation ASC
            """.format(warehouse_filter_sle=warehouse_filter_sle), params, as_dict=True)

        # Add status and age based on expiry / manufacturing date
        today = frappe.utils.today()
        today_date = frappe.utils.getdate(today)
        for batch in batches:
            # Expiry info
            if batch.expiry_date:
                days_to_expiry = (batch.expiry_date - today_date).days
                batch["days_to_expiry"] = days_to_expiry
                if days_to_expiry < 0:
                    batch["status"] = "expired"
                elif days_to_expiry <= 30:
                    batch["status"] = "critical"
                elif days_to_expiry <= 90:
                    batch["status"] = "warning"
                else:
                    batch["status"] = "ok"
                batch["expiry_date"] = str(batch.expiry_date)
            else:
                batch["days_to_expiry"] = None
                batch["status"] = "ok"
                batch["expiry_date"] = None

            # Age (FIFO) — days since manufacturing or batch creation
            if batch.get("manufacturing_date"):
                age_days = (today_date - batch.manufacturing_date).days
                batch["age_days"] = age_days
                batch["manufacturing_date"] = str(batch.manufacturing_date)
            else:
                # Fallback: use batch creation date for age
                batch_creation = frappe.db.get_value("Batch", batch.batch_no, "creation")
                if batch_creation:
                    age_days = (today_date - frappe.utils.getdate(batch_creation)).days
                    batch["age_days"] = age_days
                else:
                    batch["age_days"] = None

            batch["available_qty"] = float(batch["available_qty"])

        # Sort by FIFO — oldest first (highest age first), expired at the end
        batches.sort(key=lambda b: (
            0 if b["status"] != "expired" else 1,
            -(b["age_days"] or 0)
        ))

        return response("Available batches", batches, True, 200)

    except Exception as e:
        frappe.log_error(f"Error in get_available_batches: {str(e)}\n{frappe.get_traceback()}", "Get Batches Error")
        return response(str(e), [], False, 500)


@frappe.whitelist(methods="GET")
def sync_items_for_pos(last_sync=None, page=0, page_size=500):
    """Bulk sync endpoint for POS local database. Returns items with barcodes, UOMs, prices, stock, and tax rates in batched queries."""
    try:
        page = int(page)
        page_size = min(int(page_size), 1000)

        # Get sales person context
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        effective_price_list = get_effective_price_list(sales_person=sales_person)

        # Get settings
        include_tax = 0
        sales_person_warehouse = None
        try:
            include_tax = frappe.db.get_value("Basket4Me Settings", None, "is_this_tax_included_in_basic_rate") or 0
            settings = get_basket4me_settings()
            if settings and sales_person and hasattr(settings, 'sales_person_details') and settings.sales_person_details:
                for detail in settings.sales_person_details:
                    if detail.sales_person == sales_person:
                        sales_person_warehouse = getattr(detail, 'warehouse', None) or getattr(detail, 'default_warehouse', None)
                        break
        except Exception:
            pass

        # Build item filters
        filters = {"custom_allow_mobile_app": 1, "disabled": 0}
        if last_sync:
            filters["modified"] = [">", last_sync]

        # On page 0 of incremental sync, find items that were disabled or removed from mobile
        disabled_item_codes = []
        if last_sync and page == 0:
            disabled_rows = frappe.db.sql("""
                SELECT name FROM `tabItem`
                WHERE modified > %(last_sync)s
                  AND (disabled = 1 OR custom_allow_mobile_app = 0 OR custom_allow_mobile_app IS NULL)
            """, {"last_sync": last_sync}, as_dict=True)
            disabled_item_codes = [r["name"] for r in disabled_rows]

        # Count total matching items
        total_count = frappe.db.count("Item", filters=filters)

        # Fetch page of items
        item_list = frappe.db.get_list("Item", filters=filters,
            fields=["name", "item_name", "description", "stock_uom", "has_batch_no", "modified"],
            order_by="modified ASC",
            limit_start=page * page_size,
            limit_page_length=page_size)

        if not item_list:
            return response("Sync items", {
                "items": [], "page": page, "page_size": page_size,
                "total_items": total_count, "has_more": False,
                "disabled_items": disabled_item_codes,
                "server_time": frappe.utils.now_datetime().isoformat()
            }, True, 200)

        item_codes = [item["name"] for item in item_list]
        item_codes_tuple = tuple(item_codes) if len(item_codes) > 1 else (item_codes[0],)

        # Batch-fetch UOM conversions
        uom_map = {}
        uom_rows = frappe.db.sql("""
            SELECT parent, uom, conversion_factor
            FROM `tabUOM Conversion Detail`
            WHERE parent IN %(items)s AND parenttype = 'Item'
            ORDER BY parent, idx ASC
        """, {"items": item_codes_tuple}, as_dict=True)
        for row in uom_rows:
            uom_map.setdefault(row["parent"], []).append(row)

        # Batch-fetch prices (latest per item+uom)
        price_map = {}
        if effective_price_list:
            price_rows = frappe.db.sql("""
                SELECT ip.item_code, ip.uom, ip.price_list_rate
                FROM `tabItem Price` ip
                INNER JOIN (
                    SELECT item_code, uom, MAX(creation) as max_creation
                    FROM `tabItem Price`
                    WHERE selling = 1 AND price_list = %(price_list)s
                      AND item_code IN %(items)s
                    GROUP BY item_code, uom
                ) latest ON ip.item_code = latest.item_code
                       AND ip.uom = latest.uom
                       AND ip.creation = latest.max_creation
                WHERE ip.selling = 1 AND ip.price_list = %(price_list)s
            """, {"price_list": effective_price_list, "items": item_codes_tuple}, as_dict=True)
            for row in price_rows:
                price_map.setdefault(row["item_code"], {})[row["uom"]] = row["price_list_rate"]

        # Batch-fetch barcodes
        barcode_map = {}
        barcode_rows = frappe.db.sql("""
            SELECT parent, barcode, uom
            FROM `tabItem Barcode`
            WHERE parent IN %(items)s
        """, {"items": item_codes_tuple}, as_dict=True)
        for row in barcode_rows:
            barcode_map.setdefault(row["parent"], []).append({"barcode": row["barcode"], "uom": row["uom"] or ""})

        # Batch-fetch stock
        stock_map = {}
        if sales_person_warehouse:
            stock_rows = frappe.db.sql("""
                SELECT item_code, actual_qty, valuation_rate
                FROM `tabBin`
                WHERE item_code IN %(items)s AND warehouse = %(warehouse)s
            """, {"items": item_codes_tuple, "warehouse": sales_person_warehouse}, as_dict=True)
            for row in stock_rows:
                stock_map[row["item_code"]] = row

        # Batch-fetch tax rates
        tax_map = {}
        tax_rows = frappe.db.sql("""
            SELECT it.parent as item_code, tt.tax_rate
            FROM `tabItem Tax` it
            JOIN `tabItem Tax Template Detail` tt ON tt.parent = it.item_tax_template
            WHERE it.parent IN %(items)s AND it.parenttype = 'Item'
        """, {"items": item_codes_tuple}, as_dict=True)
        for row in tax_rows:
            tax_map[row["item_code"]] = tax_map.get(row["item_code"], 0) + flt(row["tax_rate"])

        # Assemble results
        result_items = []
        for item in item_list:
            code = item["name"]

            # Build UOM list with prices
            uom_details = uom_map.get(code, [])
            if not uom_details:
                uom_details = [{"uom": item.get("stock_uom") or "Nos", "conversion_factor": 1}]

            uoms_with_prices = []
            item_prices = price_map.get(code, {})
            for ud in uom_details:
                uom = ud.get("uom")
                if not uom:
                    continue
                uoms_with_prices.append({
                    "uom": uom,
                    "price": flt(item_prices.get(uom, 0)),
                    "conversion_factor": flt(ud.get("conversion_factor", 1))
                })

            bin_data = stock_map.get(code, {})

            result_items.append({
                "name": code,
                "item_name": item.get("item_name") or "",
                "description": strip_html_tags(item.get("description") or ""),
                "stock_uom": item.get("stock_uom") or "Nos",
                "has_batch_no": item.get("has_batch_no", 0),
                "modified": str(item.get("modified") or ""),
                "uoms": uoms_with_prices,
                "barcodes": barcode_map.get(code, []),
                "tax_rate": flt(tax_map.get(code, 0)),
                "is_tax_included": include_tax,
                "available_qty": flt(bin_data.get("actual_qty", 0)),
                "valuation_rate": flt(bin_data.get("valuation_rate", 0))
            })

        has_more = (page + 1) * page_size < total_count

        return response("Sync items", {
            "items": result_items,
            "page": page,
            "page_size": page_size,
            "total_items": total_count,
            "has_more": has_more,
            "disabled_items": disabled_item_codes if page == 0 else [],
            "effective_price_list": effective_price_list or "",
            "warehouse": sales_person_warehouse or "",
            "is_tax_included": include_tax,
            "server_time": frappe.utils.now_datetime().isoformat()
        }, True, 200)

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Sync Items For POS Error")
        return response(str(e), {}, False, 500)


def get_item_tax_rate(item_code):
    item_doc = frappe.get_doc("Item", item_code)
    tax_rate = 0

    for item_tax in item_doc.taxes:
        if item_tax.item_tax_template:
            tax_template_doc = frappe.get_doc("Item Tax Template", item_tax.item_tax_template)
            for tax in tax_template_doc.taxes:
                tax_rate += tax.tax_rate
            break  

    return tax_rate
  


@frappe.whitelist(methods="POST")
def get_item_detail(item=None):
    try:
        if not item:
            return response("Item parameter is missing", {}, False, 400)
        
        details = frappe.db.get_value(
            "Item",
            item,
            ["item_code", "item_name", "description", "stock_uom"],
            as_dict=True
        )
        
        if not details:
            return response(f"Item '{item}' not found", {}, False, 404)
        
        # Strip HTML tags from description
        if details.get("description"):
            details["description"] = strip_html_tags(details["description"])
        
        price = frappe.db.get_value(
            "Item Price", 
            filters={"selling": 1, "item_code": item}, 
            fieldname="price_list_rate"
        )
        
        details["price"] = price if price else 0
        
        return response("Item Details", details, True, 200)
    
    except Exception as exception:
        frappe.log_error(frappe.get_traceback(), "Get Item Detail Error")
        return response(str(exception), {}, False, 417)



@frappe.whitelist(methods="POST")
def get_customer_detail(customer=None):
    try:
        if not customer:
            return response("Customer is missing", {}, False, 400)

        customer_details = frappe.db.get_value(
            "Customer",
            customer,
            ["name", "customer_name", "tax_id", "customer_group", "territory", "mobile_no"],
            as_dict=True
        )

        if not customer_details:
            return response(f"Customer '{customer}' not found", {}, False, 404)

        # Addresses
        address_list = frappe.get_all(
            "Dynamic Link",
            filters={
                "parenttype": "Address",
                "link_doctype": "Customer",
                "link_name": customer,
            },
            fields=["parent as address"]
        )

        addresses = []
        for address in address_list:
            address_doc = frappe.get_doc("Address", address["address"])
            addresses.append({
                "address_title": address_doc.address_title,
                "address_line1": address_doc.address_line1,
                "address_line2": address_doc.address_line2,
                "city": address_doc.city,
                "state": address_doc.state,
                "country": address_doc.country,
                "pincode": address_doc.pincode,
                "phone": address_doc.phone,
                "email_id": address_doc.email_id,
            })

        customer_details["addresses"] = addresses

        # Credit Limit (from Customer Credit Limit child table)
        credit_limit = frappe.db.get_value(
            "Customer Credit Limit",
            {"parent": customer, "parenttype": "Customer"},
            "credit_limit"
        ) or 0
        customer_details["credit_limit"] = flt(credit_limit)

        # Total Unpaid (outstanding from submitted invoices)
        total_unpaid = frappe.db.sql("""
            SELECT COALESCE(SUM(outstanding_amount), 0) as total
            FROM `tabSales Invoice`
            WHERE customer = %s AND docstatus = 1 AND outstanding_amount > 0
        """, (customer,), as_dict=True)
        customer_details["total_unpaid"] = flt(total_unpaid[0].total) if total_unpaid else 0

        # Annual Billing (this year, non-return submitted invoices)
        from frappe.utils import getdate, get_first_day
        year_start = get_first_day(getdate()).replace(month=1, day=1)
        annual_billing = frappe.db.sql("""
            SELECT COALESCE(SUM(grand_total), 0) as total
            FROM `tabSales Invoice`
            WHERE customer = %s AND docstatus = 1 AND is_return = 0
              AND posting_date >= %s
        """, (customer, year_start), as_dict=True)
        customer_details["annual_billing"] = flt(annual_billing[0].total) if annual_billing else 0

        return response("Customer Details", customer_details, True, 200)
    except frappe.DoesNotExistError:
        return response(f"Customer '{customer}' not found", {}, False, 404)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


import frappe
import json

import frappe
import json

@frappe.whitelist(methods="POST")
def create_customer(params):
    try:
        if isinstance(params, str):
            params = json.loads(params)

        customerdetails = params.get('customerdetails', {})
        if not frappe.db.exists("Customer", {'customer_name': customerdetails.get('name')}):
            sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")

            # Create Customer
            customer_doc = frappe.new_doc("Customer")
            customer_doc.customer_name = customerdetails.get('name')
            customer_doc.custom_customer_name_in_arabic = customerdetails.get('custom_customer_name_in_arabic')
            customer_doc.customer_group = customerdetails.get('customer_group')
            customer_doc.tax_id = customerdetails.get('tax_id')
            customer_doc.custom_vat_registration_number = customerdetails.get('custom_vat_registration_number')
            customer_doc.mobile_no = customerdetails.get('mobile_no')
            customer_doc.email_id = customerdetails.get('email')

            if customerdetails.get('value'):
                customer_doc.append("custom_additional_ids", {
                    "type_name": "Commercial Registration Number",
                    "type_code": "CRN",
                    "value": customerdetails['value']
                })

            if sales_person:
                customer_doc.append("sales_team", {
                    "sales_person": sales_person,
                    "allocated_percentage": 100  
                })
                
            customer_doc.flags.ignore_permissions = True
            customer_doc.save()

            # Check if address details exist before creating Address
            customerdetails_address = customerdetails.get('address')
            if customerdetails_address and all([
                customerdetails_address.get('addressline_1'),
                customerdetails_address.get('city'),
                customerdetails_address.get('country')
            ]):
                address_doc = frappe.new_doc("Address")
                address_doc.address_title = customerdetails.get('name')
                address_doc.address_type = "Billing"
                address_doc.address_line1 = customerdetails_address.get('addressline_1')
                address_doc.address_line2 = customerdetails_address.get('addressline_2', '')
                # Custom fields — safely set only if they exist on this instance
                if hasattr(address_doc, 'custom_building_number'):
                    address_doc.custom_building_number = customerdetails_address.get('custom_building_number', '')
                if hasattr(address_doc, 'custom_area'):
                    address_doc.custom_area = customerdetails_address.get('custom_area', '')
                address_doc.city = customerdetails_address.get('city')
                address_doc.state = customerdetails_address.get('state', '')
                address_doc.pincode = customerdetails_address.get('pincode', '')
                address_doc.country = customerdetails_address.get('country')

                address_doc.append("links", {
                    "link_doctype": "Customer",
                    "link_name": customer_doc.name
                })

                address_doc.is_primary_address = 1
                address_doc.flags.ignore_permissions = True
                address_doc.save()

        return response("Customer Created", {"name": customer_doc.name, "customer_name": customer_doc.customer_name}, True, 200)

    except Exception as exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


    
@frappe.whitelist(methods="POST")
def create_sales_invoice(params):
    try:
        if isinstance(params, str):
            params = json.loads(params)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        customer = params.get("customer")

        settings = get_basket4me_settings()
        sales_person_details = None

        for detail in settings.sales_person_details:
            if detail.sales_person == sales_person:
                sales_person_details = detail

        if not sales_person_details:
            return response(f"No Basket4Me Settings found for sales person {sales_person}", {}, False, 400)

        # User-selected price list overrides auto-detection
        user_price_list = params.get("price_list") or params.get("selling_price_list")
        if user_price_list and frappe.db.exists("Price List", user_price_list):
            effective_price_list = user_price_list
        else:
            effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)

        customer_name = params.get("customer_name")
        tax_id = params.get("tax_id")
        items = params.get("items")
        due_date = params.get("due_date")
        posting_date = params.get("posting_date")
        payment_type = params.get("custom_payment_type") or params.get("payment_type")
        additional_discount_amount = flt(params.get("additional_discount_amount") or params.get("discount_amount") or 0)

        if not customer:
            return response("Customer is required", {}, False, 400)
        if not items or not isinstance(items, list):
            return response("At least one item is required", {}, False, 400)

        sales_invoice = frappe.new_doc("Sales Invoice")
        sales_invoice.customer = customer
        sales_invoice.customer_name = customer_name
        sales_invoice.tax_id = tax_id
        sales_invoice.due_date = due_date
        sales_invoice.posting_date = posting_date
        sales_invoice.custom_payment_type = payment_type
        sales_invoice.cost_center = sales_person_details.cost_center
        sales_invoice.selling_price_list = effective_price_list
        sales_invoice.custom_mobile_app = 1
        sales_invoice.set_posting_time = 1
        sales_invoice.company = sales_person_details.company

        response_items = []

        for item in items:
            item_code = item.get("item_code")
            qty = item.get("qty")
            uom = item.get("uom")
            description = item.get("description")
            # Strip HTML tags from description
            if description:
                description = strip_html_tags(description)
            provided_rate = item.get("rate")
            is_free_item = item.get("is_free_item", False)  # Get is_free_item field, default to False

            item_price = frappe.db.sql(
                """
                SELECT price_list_rate
                FROM `tabItem Price`
                WHERE item_code = %(item_code)s
                AND uom = %(uom)s
                AND price_list = %(price_list)s
                ORDER BY uom DESC, creation DESC
                LIMIT 1
                """,
                {"item_code": item_code, "uom": uom, "price_list": effective_price_list},
                as_dict=True
            )

            latest_item_price = item_price[0]["price_list_rate"] if item_price else None

            # Determine the rate based on is_free_item flag
            if is_free_item:
                rate = 0  # Set rate to 0 for free items
            elif latest_item_price is not None:
                rate = latest_item_price
            else:
                rate = provided_rate

            price_list_rate = flt(rate)  # Original price before discount

            # Handle both discount_percentage and discount_amount from mobile app
            provided_discount_percentage = flt(item.get("discount_percentage", 0))
            provided_discount_amount = flt(item.get("discount_amount", 0))

            # Calculate discount based on what's provided
            if provided_discount_percentage and price_list_rate:
                # If discount percentage is provided, calculate discount amount from it
                discount_percentage = provided_discount_percentage
                discount_amount = (price_list_rate * discount_percentage) / 100
            elif provided_discount_amount and price_list_rate:
                # If discount amount is provided, calculate discount percentage from it
                discount_amount = provided_discount_amount
                discount_percentage = (discount_amount / price_list_rate) * 100
            else:
                discount_percentage = 0
                discount_amount = 0

            # Calculate the discounted rate
            discounted_rate = price_list_rate - discount_amount

            item_data = {
                "item_code": item_code,
                "qty": qty,
                "uom": uom,
                "description": description,
                "warehouse": sales_person_details.warehouse,
                "cost_center": sales_person_details.cost_center,
                "discount_percentage": discount_percentage,
                "discount_amount": discount_amount,
                "rate": discounted_rate,  # Use the discounted rate
                "stock_uom_rate": discounted_rate,  # Set stock UOM rate same as discounted rate
                "price_list_rate": price_list_rate,  # Original price before discount
                "base_rate": discounted_rate,  # Set base rate same as discounted rate
                "base_price_list_rate": price_list_rate,  # Original price before discount
                "is_free_item": is_free_item  # Set the is_free_item field in the invoice item
            }

            # Handle batch selection for pharmaceutical/batch-tracked items
            batch_no = item.get("batch_no")
            if batch_no:
                item_data["use_serial_batch_fields"] = 1
                item_data["batch_no"] = batch_no

            sales_invoice.append("items", item_data)

            response_item = {
                "item_code": item_code,
                "description": description,
                "qty": qty,
                "uom": uom,
                "discount_amount": discount_amount,
                "discount_percentage": discount_percentage,
                "price_list_rate": price_list_rate,
                "rate": discounted_rate,
                "is_free_item": is_free_item  # Include is_free_item in the response
            }
            response_items.append(response_item)

        # Bypass permissions during set_missing_values (loads Customer/Company docs internally)
        frappe.flags.ignore_permissions = True
        sales_invoice.run_method("set_missing_values")
        sales_invoice.run_method("set_other_charges")
        frappe.flags.ignore_permissions = False

        # Set additional discount AFTER set_missing_values to prevent reset
        # Apply on Net Total: discount before tax, so tax is calculated on reduced amount
        if additional_discount_amount:
            sales_invoice.apply_discount_on = "Net Total"
            sales_invoice.discount_amount = additional_discount_amount

        sales_invoice.run_method("calculate_taxes_and_totals")

        # Set PO Number and Remarks AFTER set_missing_values (which auto-generates remarks)
        po_no = params.get("po_no")
        remarks_text = params.get("remarks")
        if po_no:
            sales_invoice.po_no = po_no
        if remarks_text:
            sales_invoice.remarks = remarks_text

        # Enforce rate = 0 for free items after all calculations
        enforce_free_item_rates(sales_invoice)

        sales_invoice.append("sales_team", {
            "sales_person": sales_person,
            "allocated_percentage": 100
        })

        # Check if DN creation is disabled — if so, SI handles stock directly
        vs_settings = get_basket4me_settings()
        if vs_settings.ignore_create_delivery_note:
            sales_invoice.update_stock = 1
        else:
            sales_invoice.update_stock = 0

        # Credit sale: no POS payments, invoice remains unpaid for later collection
        is_credit = params.get("is_credit", False)
        if is_credit:
            sales_invoice.is_pos = 0
        else:
            # POS Payment: embed payments directly in the Sales Invoice
            pos_payments = params.get("payments")
            if pos_payments and isinstance(pos_payments, list):
                sales_invoice.is_pos = 1
                total_paid = 0
                for pay in pos_payments:
                    mode = pay.get("mode_of_payment")
                    amount = flt(pay.get("amount", 0))
                    if mode and amount > 0:
                        sales_invoice.append("payments", {
                            "mode_of_payment": mode,
                            "amount": amount
                        })
                        total_paid += amount
                # Handle change when customer overpays
                change_amount = flt(params.get("change_amount", 0))
                if change_amount > 0:
                    sales_invoice.change_amount = change_amount
                elif total_paid > sales_invoice.grand_total:
                    sales_invoice.change_amount = flt(total_paid - sales_invoice.grand_total)

        sales_invoice.flags.ignore_permissions = True
        sales_invoice.save()

        response_data = {
            "invoice_id": sales_invoice.name,
            "posting_date": posting_date,
            "due_date": due_date,
            "customer": customer,
            "customer_name": sales_invoice.customer_name,
            "tax_id": tax_id,
            "payment_type": payment_type,
            "sales_person": sales_person,
            "company": sales_invoice.company,
            "items": response_items,
            "total": sales_invoice.total,
            "net_total": sales_invoice.net_total,
            "total_taxes_and_charges": sales_invoice.total_taxes_and_charges,
            "additional_discount_amount": additional_discount_amount,
            "grand_total": sales_invoice.grand_total,
            "status": sales_invoice.status,
            "effective_price_list": effective_price_list  # Include for reference
        }

        return response("Sales Invoice Details", response_data, True, 201)

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Sales Invoice Creation Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="POST")
def update_sales_invoice(params):
    """Update a draft Sales Invoice. Only drafts (docstatus=0) can be updated."""
    try:
        if isinstance(params, str):
            params = json.loads(params)

        name = params.get("name")
        if not name:
            return response("Sales Invoice name is required", {}, False, 400)

        sales_invoice = frappe.get_doc("Sales Invoice", name)
        if sales_invoice.docstatus != 0:
            return response(f"Only Draft invoices can be updated. '{name}' is {sales_invoice.status}", {}, False, 400)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        customer = params.get("customer", sales_invoice.customer)

        settings = get_basket4me_settings()
        sales_person_details = None
        for detail in settings.sales_person_details:
            if detail.sales_person == sales_person:
                sales_person_details = detail

        if not sales_person_details:
            return response(f"No Basket4Me Settings found for sales person {sales_person}", {}, False, 400)

        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)

        # Update header fields
        if params.get("customer"):
            sales_invoice.customer = params["customer"]
        if params.get("customer_name"):
            sales_invoice.customer_name = params["customer_name"]
        if params.get("posting_date"):
            sales_invoice.posting_date = params["posting_date"]
        if params.get("due_date"):
            sales_invoice.due_date = params["due_date"]
        payment_type = params.get("custom_payment_type") or params.get("payment_type")
        if payment_type:
            sales_invoice.custom_payment_type = payment_type
        sales_invoice.selling_price_list = effective_price_list

        # Update items if provided
        items = params.get("items")
        if items and isinstance(items, list):
            sales_invoice.items = []
            response_items = []

            for item in items:
                item_code = item.get("item_code")
                qty = item.get("qty")
                uom = item.get("uom")
                description = item.get("description")
                if description:
                    description = strip_html_tags(description)
                provided_rate = item.get("rate")
                is_free_item = item.get("is_free_item", False)

                item_price = frappe.db.sql(
                    """
                    SELECT price_list_rate
                    FROM `tabItem Price`
                    WHERE item_code = %(item_code)s
                    AND uom = %(uom)s
                    AND price_list = %(price_list)s
                    ORDER BY uom DESC, creation DESC
                    LIMIT 1
                    """,
                    {"item_code": item_code, "uom": uom, "price_list": effective_price_list},
                    as_dict=True
                )

                latest_item_price = item_price[0]["price_list_rate"] if item_price else None

                if is_free_item:
                    rate = 0
                elif latest_item_price is not None:
                    rate = latest_item_price
                else:
                    rate = provided_rate

                price_list_rate = flt(rate)

                provided_discount_percentage = flt(item.get("discount_percentage", 0))
                provided_discount_amount = flt(item.get("discount_amount", 0))

                if provided_discount_percentage and price_list_rate:
                    discount_percentage = provided_discount_percentage
                    discount_amount = (price_list_rate * discount_percentage) / 100
                elif provided_discount_amount and price_list_rate:
                    discount_amount = provided_discount_amount
                    discount_percentage = (discount_amount / price_list_rate) * 100
                else:
                    discount_percentage = 0
                    discount_amount = 0

                discounted_rate = price_list_rate - discount_amount

                item_data = {
                    "item_code": item_code,
                    "qty": qty,
                    "uom": uom,
                    "description": description,
                    "warehouse": sales_person_details.warehouse,
                    "cost_center": sales_person_details.cost_center,
                    "discount_percentage": discount_percentage,
                    "discount_amount": discount_amount,
                    "rate": discounted_rate,
                    "stock_uom_rate": discounted_rate,
                    "price_list_rate": price_list_rate,
                    "base_rate": discounted_rate,
                    "base_price_list_rate": price_list_rate,
                    "is_free_item": is_free_item
                }

                sales_invoice.append("items", item_data)
                response_items.append({
                    "item_code": item_code,
                    "description": description,
                    "qty": qty,
                    "uom": uom,
                    "discount_amount": discount_amount,
                    "discount_percentage": discount_percentage,
                    "price_list_rate": price_list_rate,
                    "rate": discounted_rate,
                    "is_free_item": is_free_item
                })

            sales_invoice.run_method("set_missing_values")
            sales_invoice.run_method("set_other_charges")

            # Set additional discount AFTER set_missing_values to prevent reset
            if "discount_amount" in params:
                sales_invoice.apply_discount_on = "Grand Total"
                sales_invoice.discount_amount = params["discount_amount"]

            sales_invoice.run_method("calculate_taxes_and_totals")
            enforce_free_item_rates(sales_invoice)

        # Handle discount_amount even when items are not being updated
        if not (items and isinstance(items, list)) and "discount_amount" in params:
            sales_invoice.apply_discount_on = "Grand Total"
            sales_invoice.discount_amount = params["discount_amount"]
            sales_invoice.run_method("calculate_taxes_and_totals")

        sales_invoice.save(ignore_permissions=True)
        frappe.db.commit()

        data = {
            "name": sales_invoice.name,
            "customer": sales_invoice.customer,
            "customer_name": sales_invoice.customer_name,
            "posting_date": str(sales_invoice.posting_date),
            "due_date": str(sales_invoice.due_date) if sales_invoice.due_date else None,
            "docstatus": sales_invoice.docstatus,
            "status": sales_invoice.status,
            "custom_payment_type": sales_invoice.custom_payment_type,
            "total": sales_invoice.total,
            "net_total": sales_invoice.net_total,
            "total_taxes_and_charges": sales_invoice.total_taxes_and_charges,
            "discount_amount": sales_invoice.discount_amount or 0,
            "grand_total": sales_invoice.grand_total,
            "outstanding_amount": sales_invoice.outstanding_amount,
        }

        return response("Sales Invoice updated successfully", data, True, 200)

    except frappe.DoesNotExistError:
        return response(f"Sales Invoice '{name}' not found", {}, False, 404)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Sales Invoice Update Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_warehouse_list(name=None):
    try:
        filters = {}
        fields = ['name']

        if name:
            filters['name'] = name

        warehouse_list = frappe.db.get_list("Warehouse", filters=filters, fields=fields)

        if warehouse_list:
            return response("Warehouse List", warehouse_list, True, 200)
        else:
            return response("No Warehouse List", [], True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)
    

@frappe.whitelist(methods="POST")
def submit_sales_invoice(params):
    try:
        if isinstance(params, str):
            params = json.loads(params)

        name = params.get("name")

        if not name:
            return response("Sales Invoice name is required", {}, False, 400)

        sales_invoice = frappe.get_doc("Sales Invoice", name)

        if sales_invoice.docstatus == 1:
            return response(f"Sales Invoice '{name}' is already submitted", {}, False, 400)

        # ── Validate return quantities before submit (using stock_qty for UOM independence) ──
        if sales_invoice.is_return and sales_invoice.return_against:
            from frappe.utils import flt
            orig_items = frappe.get_all(
                "Sales Invoice Item",
                filters={"parent": sales_invoice.return_against},
                fields=["item_code", "stock_qty"],
            )
            orig_stock_qty_map = {}
            for oi in orig_items:
                orig_stock_qty_map[oi.item_code] = orig_stock_qty_map.get(oi.item_code, 0) + flt(oi.stock_qty)

            # Sum already-returned stock_qty from OTHER submitted returns (exclude this one and drafts)
            existing_returns = frappe.get_all(
                "Sales Invoice",
                filters={
                    "return_against": sales_invoice.return_against,
                    "docstatus": 1,
                    "name": ["!=", name],
                },
                pluck="name",
            )
            returned_stock_qty_map = {}
            for ret_name in existing_returns:
                ret_items = frappe.get_all(
                    "Sales Invoice Item",
                    filters={"parent": ret_name},
                    fields=["item_code", "stock_qty"],
                )
                for ri in ret_items:
                    returned_stock_qty_map[ri.item_code] = returned_stock_qty_map.get(ri.item_code, 0) + abs(flt(ri.stock_qty))

            errors = []
            for item in sales_invoice.items:
                ic = item.item_code
                req_stock_qty = abs(flt(item.stock_qty))
                orig_stock = orig_stock_qty_map.get(ic, 0)
                already_returned_stock = returned_stock_qty_map.get(ic, 0)
                balance_stock = orig_stock - already_returned_stock
                if balance_stock <= 0:
                    errors.append(f"{ic}: already fully returned")
                elif req_stock_qty > balance_stock + 0.01:  # small float tolerance
                    errors.append(f"{ic}: requested {abs(item.qty):.0f} {item.uom} but only {balance_stock:.1f} {item.stock_uom} remaining")

            if errors:
                return response("Return quantity exceeds balance:\n" + "\n".join(errors), {}, False, 400)

        # Bypass permission checks for Serial and Batch Bundle created during
        # on_submit hooks.  We monkey-patch Document.check_permission instead of
        # frappe.set_user("Administrator") to avoid corrupting the user session/cookies.
        sales_invoice.flags.ignore_permissions = True
        from frappe.model.document import Document
        _orig_check_perm = Document.check_permission
        _bypass_doctypes = ("Serial and Batch Bundle", "Batch", "Stock Ledger Entry")
        def _patched_check_perm(self, permtype='read', permlevel=None):
            if self.doctype in _bypass_doctypes:
                return
            return _orig_check_perm(self, permtype, permlevel)
        Document.check_permission = _patched_check_perm
        try:
            sales_invoice.submit()
        finally:
            Document.check_permission = _orig_check_perm

        return response(f"Sales Invoice '{name}' submitted successfully", {
            "sales_invoice": name,
            "status": sales_invoice.status
        }, True, 200)

    except frappe.DoesNotExistError:
        frappe.db.rollback()
        return response(f"Sales Invoice '{name}' not found", {}, False, 404)
    except Exception as e:
        frappe.db.rollback()  # Rollback SI submit + any partial DN creation
        frappe.log_error(frappe.get_traceback(), "Sales Invoice Submit Error")
        error_msg = str(e).replace('\n', ' ')
        # Strip HTML tags from error message
        error_msg = error_msg.replace('<', '').replace('>', '')
        return response(error_msg, {}, False, 417)

@frappe.whitelist(methods="POST")
def cancel_sales_invoice(params):
    try:
        if isinstance(params, str):
            params = json.loads(params)

        name = params.get("name")

        if not name:
            return response("Sales Invoice name is required", {}, False, 400)

        sales_invoice = frappe.get_doc("Sales Invoice", name)

        if sales_invoice.docstatus == 2:
            return response(f"Sales Invoice '{name}' is already Cancelled", {}, False, 400)

        sales_invoice.flags.ignore_permissions = True
        sales_invoice.cancel()

        return response(f"Sales Invoice '{name}' cancelled successfully", {
            "sales_invoice": name,
            "status": sales_invoice.status
        }, True, 200)

    except frappe.DoesNotExistError:
        frappe.db.rollback()
        return response(f"Sales Invoice '{name}' not found", {}, False, 404)
    except Exception as e:
        frappe.db.rollback()  # Rollback cancel + any partial hook operations
        frappe.log_error(frappe.get_traceback(), "Sales Invoice Cancel Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="POST")
def delete_sales_invoice(params):
    try:
        if isinstance(params, str):
            params = json.loads(params)

        name = params.get("name")

        if not name:
            return response("Sales Invoice name is required", {}, False, 400)

        sales_invoice = frappe.get_doc("Sales Invoice", name)

        if sales_invoice.docstatus != 0:
            return response(f"Only Draft invoices can be deleted. '{name}' is {sales_invoice.status}", {}, False, 400)

        frappe.delete_doc("Sales Invoice", name, force=True, ignore_permissions=True)
        frappe.db.commit()

        return response(f"Sales Invoice '{name}' deleted successfully", {
            "sales_invoice": name
        }, True, 200)

    except frappe.DoesNotExistError:
        return response(f"Sales Invoice '{name}' not found", {}, False, 404)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Sales Invoice Delete Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods=["GET", "POST"])
def get_print_pdf(doctype=None, name=None, print_format=None, letterhead=None):
    """Simple print endpoint that returns base64-encoded PDF or HTML fallback.
    Uses frappe.get_print() which is the most reliable print method."""
    try:
        if not doctype or not name:
            return response("doctype and name are required", {}, False, 400)

        if not frappe.db.exists(doctype, name):
            return response(f"{doctype} {name} not found", {}, False, 404)

        if not print_format:
            print_format = "Standard"

        # Generate print HTML (always works)
        # Basket4Me users may lack direct read permission — bypass for print
        frappe.flags.ignore_permissions = True
        try:
            kwargs = {"doctype": doctype, "name": name, "print_format": print_format}
            if letterhead is not None:
                kwargs["letterhead"] = letterhead
            html = frappe.get_print(**kwargs)
        finally:
            frappe.flags.ignore_permissions = False

        if not html:
            return response("Failed to generate print content", {}, False, 500)

        result = {
            "doctype": doctype,
            "name": name,
            "print_format": print_format,
            "html": html,
            "content_type": "text/html",
        }

        # Also try PDF generation (requires wkhtmltopdf)
        try:
            pdf_binary = get_pdf(html)
            result["pdf_base64"] = base64.b64encode(pdf_binary).decode()
        except Exception:
            pass

        return response("Print generated", result, True, 200)

    except frappe.PermissionError:
        return response("No permission to access this document", {}, False, 403)
    except Exception as e:
        frappe.log_error(f"Print error: {str(e)}\n{frappe.get_traceback()}", "Get Print PDF Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_print_formats(doctype=None):
    """Get available print formats for a doctype."""
    try:
        if not doctype:
            return response("doctype is required", {}, False, 400)

        formats = frappe.get_all(
            "Print Format",
            filters={"doc_type": doctype, "disabled": ["!=", 1]},
            fields=["name"],
            order_by="name asc",
        )

        result = [{"name": "Standard"}]
        for pf in formats:
            result.append({"name": pf.name})

        # Also fetch available letterheads
        letterheads = frappe.get_all(
            "Letter Head",
            filters={"disabled": 0},
            fields=["name", "is_default"],
            order_by="is_default desc, name asc",
        )
        letterhead_list = [{"name": lh.name, "is_default": lh.is_default} for lh in letterheads]

        return response("Print Formats", {
            "print_formats": result,
            "letterheads": letterhead_list,
        }, True, 200)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Get Print Formats Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_payment_type():
    """Get payment types dynamically from the custom_payment_type field options on Sales Invoice"""
    try:
        payment_types = []
        # Read options from the Custom Field definition
        options = frappe.db.get_value(
            "Custom Field",
            {"dt": "Sales Invoice", "fieldname": "custom_payment_type"},
            "options"
        )
        if options:
            payment_types = [
                {"payment_type": pt.strip()}
                for pt in options.split("\n")
                if pt.strip()
            ]
        # Fallback if no options found
        if not payment_types:
            payment_types = [{"payment_type": "Cash"}, {"payment_type": "Credit"}]

        return response("Payment Type", payment_types, True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_salesperson_config():
    """Get the current user's salesperson configuration from Basket4Me Settings.
    Returns payment types, sales person name, and sales person details."""
    try:
        current_user = frappe.session.user
        sales_person = frappe.db.get_value(
            "Sales Person", {"custom_user": current_user}, "name"
        )

        # Get payment types from custom_payment_type field options
        payment_types = []
        options = frappe.db.get_value(
            "Custom Field",
            {"dt": "Sales Invoice", "fieldname": "custom_payment_type"},
            "options"
        )
        if options:
            payment_types = [pt.strip() for pt in options.split("\n") if pt.strip()]
        if not payment_types:
            payment_types = ["Cash", "Credit"]

        result = {
            "sales_person": sales_person,
            "sales_person_valid": bool(sales_person),
            "payment_types": payment_types,
        }

        # Get sales person details from Basket4Me Settings
        sp_found_in_settings = False
        sp_detail = None
        if sales_person:
            try:
                settings = get_basket4me_settings()
                for detail in settings.sales_person_details:
                    if detail.sales_person == sales_person:
                        sp_detail = detail
                        result["mode_of_payment"] = detail.mode_of_payment
                        result["warehouse"] = detail.warehouse
                        result["cost_center"] = detail.cost_center
                        result["price_list"] = detail.price_list
                        result["company"] = detail.company or ""
                        result["deduction_account"] = detail.deduction_account or ""
                        sp_found_in_settings = True
                        break
            except Exception:
                pass
        result["sales_person_in_settings"] = sp_found_in_settings

        # Add ALL Basket4Me Settings fields
        try:
            vs_settings = get_basket4me_settings()
            result["validate_stock"] = vs_settings.validate_stock or 0
            result["custom_tax_inclusive"] = vs_settings.is_this_tax_included_in_basic_rate or 0

            # Universal payment modes from mode_of_payment_details child table
            universal_modes = []
            for mode_detail in (vs_settings.mode_of_payment_details or []):
                if mode_detail.mode_of_payment:
                    universal_modes.append(mode_detail.mode_of_payment)
            result["universal_payment_modes"] = universal_modes

            # All remaining Basket4Me Settings fields
            result["enable_customer_based_price_list"] = vs_settings.enable_customer_based_price_list or 0
            result["payment_entry_based_on_sales_person"] = vs_settings.payment_entry_based_on_sales_person or 0
            result["ignore_create_delivery_note"] = vs_settings.ignore_create_delivery_note or 0
            result["override_sales_team_in_customer"] = vs_settings.override_sales_team_in_customer or 0
            if not result.get("deduction_account"):
                result["deduction_account"] = vs_settings.deduction_account or ""
            result["default_target_warehouse"] = vs_settings.default_target_warehouse or ""
            if not result.get("cost_center"):
                result["posnext_cost_center"] = vs_settings.cost_center or ""
            result["view_all_transaction_role"] = vs_settings.view_all_transaction_role or ""
            result["allow_rate_edit"] = vs_settings.edit_rate or 0
            result["show_incoming_rate"] = getattr(vs_settings, 'show_incoming_rate', 0) or 0
            result["show_purchase_rate"] = getattr(vs_settings, 'show_purchase_rate', 0) or 0
            result["show_last_customer_rate"] = getattr(vs_settings, 'show_last_customer_rate', 0) or 0
            result["enable_batch_selection"] = getattr(vs_settings, 'enable_batch_selection', 0) or 0
        except Exception:
            result["validate_stock"] = 0
            result["custom_tax_inclusive"] = 0
            result["universal_payment_modes"] = []
            result["company"] = ""
            result["enable_customer_based_price_list"] = 0
            result["payment_entry_based_on_sales_person"] = 0
            result["ignore_create_delivery_note"] = 0
            result["override_sales_team_in_customer"] = 0
            result["deduction_account"] = ""
            result["default_target_warehouse"] = ""
            result["posnext_cost_center"] = ""
            result["view_all_transaction_role"] = ""
            result["allow_rate_edit"] = 0
            result["show_incoming_rate"] = 0
            result["show_purchase_rate"] = 0
            result["show_last_customer_rate"] = 0
            result["enable_batch_selection"] = 0

        # Add system settings (precision, date format) and currency from Company
        try:
            sys_settings = frappe.get_cached_doc("System Settings")
            result["float_precision"] = sys_settings.float_precision or ""
            result["currency_precision"] = sys_settings.currency_precision or ""
            result["date_format"] = sys_settings.date_format or "dd-mm-yyyy"
            result["number_format"] = sys_settings.number_format or "#,###.##"
            result["country"] = sys_settings.country or ""

            # Get currency from sales person's company (dynamic per login)
            currency_name = None
            sp_company = result.get("company") or ""
            if sp_company:
                company_currency = frappe.db.get_value("Company", sp_company, "default_currency")
                if company_currency:
                    currency_name = company_currency

            if not currency_name:
                currency_name = sys_settings.currency or "OMR"

            result["currency"] = currency_name
            if frappe.db.exists("Currency", currency_name):
                currency_doc = frappe.get_cached_doc("Currency", currency_name)
                result["currency_symbol"] = currency_doc.symbol or currency_name
            else:
                result["currency_symbol"] = currency_name
        except Exception:
            result["currency"] = "SAR"
            result["currency_symbol"] = "SAR"
            result["float_precision"] = ""
            result["currency_precision"] = ""
            result["country"] = ""

        # Find default customer for this company (Cash Customer / Walk-in / first available)
        try:
            sp_company = result.get("company") or ""
            default_cust = None
            if sp_company:
                # Try common default customer names first
                for cname in ["Cash Customer", "Walk In", "Walk-in", "Cash Sale"]:
                    cid = frappe.db.get_value("Customer", {"customer_name": cname}, "name")
                    if cid:
                        default_cust = cid
                        break
                # Try company-specific cash customer
                if not default_cust:
                    cid = frappe.db.get_value("Customer",
                        {"customer_name": ["like", f"%Cash%{sp_company}%"]}, "name")
                    if cid:
                        default_cust = cid
                if not default_cust:
                    cid = frappe.db.get_value("Customer",
                        {"customer_name": ["like", "%Cash%"]}, "name")
                    if cid:
                        default_cust = cid
            result["default_customer"] = default_cust or ""
        except Exception:
            result["default_customer"] = ""

        return response("Salesperson Config", result, True, 200)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Get Salesperson Config Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="DELETE")
def delete_invoice(params):
    try:
        if isinstance(params, str):
            params = json.loads(params)

        name = params.get("name")

        if not name:
            return response("Sales Invoice name is required", {}, False, 400)

        sales_invoice = frappe.get_doc("Sales Invoice", name)

        if sales_invoice.docstatus != 0:
            return response(f"Only draft invoices can be deleted. '{name}' is not a draft.", {}, False, 400)

        frappe.delete_doc("Sales Invoice", name, force=1)

        return response(f"Sales Invoice '{name}' deleted successfully", {
            "sales_invoice": name,
            "status": "Deleted"
        }, True, 200)

    except frappe.DoesNotExistError:
        return response(f"Sales Invoice '{name}' not found", {}, False, 404)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Delete Draft Invoice Error")
        return response(str(e), {}, False, 417)

@frappe.whitelist(methods="POST")
def invoice_details(invoice_id=None):
    try:
        invoice = frappe.get_doc("Sales Invoice", invoice_id)
        response_items = []
        person = []

        for item in invoice.items:
            tax_rate = None
            if item.item_tax_template:
                tax_rate = frappe.db.get_value(
                    "Item Tax Template Detail", 
                    {"parent": item.item_tax_template}, 
                    "tax_rate"
                )
            response_items.append({
                "item_code": item.item_code,
                "item_name": item.item_name,
                "description": strip_html_tags(item.description) if item.description else item.description,
                "uom": item.uom,
                "qty": item.qty,
                "rate": item.rate,
                "tax_rate": tax_rate if tax_rate else 0.0,
                "tax_amount": item.tax_amount,
                "discount_amount": item.discount_amount,
                "amount": item.amount
            })
        
        for sales in invoice.sales_team:
            person.append({
                "sales_person": sales.sales_person
            })

        # Get QR Code from Sales Invoice Additional Fields
        qr_code_data = None
        try:
            additional_fields = frappe.db.get_list(
                "Sales Invoice Additional Fields",
                filters={"sales_invoice": invoice_id},
                fields=["qr_code"],
                limit=1,
                ignore_permissions=True
            )
            
            if additional_fields:
                qr_code_data = additional_fields[0].get("qr_code")
        except Exception as e:
            frappe.log_error(f"Error fetching QR code: {str(e)}")
            qr_code_data = None

        # Get Customer Information
        customer_info = {
            "customer": invoice.customer,
            "customer_name": invoice.customer_name,
            "tax_id": invoice.tax_id if invoice.tax_id else ""
        }

        # Get Customer Address from Dynamic Link
        customer_address = {}
        try:
            # Find address linked to this customer
            address_links = frappe.db.sql("""
                SELECT dl.parent 
                FROM `tabDynamic Link` dl
                WHERE dl.link_doctype = 'Customer' 
                AND dl.link_name = %s
                AND dl.parenttype = 'Address'
            """, (invoice.customer,), as_dict=True)
            
            if address_links:
                address_name = address_links[0].parent
                address_doc = frappe.get_doc("Address", address_name)
                customer_address = {
                    "address_line1": address_doc.address_line1 or "",
                    "address_line2": address_doc.address_line2 or "",
                    "custom_building_number": getattr(address_doc, 'custom_building_number', '') or "",
                    "custom_area": getattr(address_doc, 'custom_area', '') or "",
                    "city": address_doc.city or "",
                    "state": address_doc.state or "",
                    "county": address_doc.county or "",
                    "pincode": address_doc.pincode or "",
                    "custom_cr_number": getattr(address_doc, 'custom_cr_number', '') or ""
                }
        except Exception as e:
            frappe.log_error(f"Error fetching customer address: {str(e)}", "Invoice Details Error")
            customer_address = {
                "address_line1": "", "address_line2": "", "custom_building_number": "",
                "custom_area": "", "city": "", "state": "", "county": "", "pincode": "", "custom_cr_number": ""
            }

        # Get Company Information
        company_info = {
            "company": invoice.company
        }
        
        # Get company tax_id
        try:
            company_tax_id = frappe.db.get_value("Company", invoice.company, "tax_id")
            company_info["tax_id"] = company_tax_id or ""
        except Exception as e:
            frappe.log_error(f"Error fetching company tax_id: {str(e)}", "Invoice Details Error")
            company_info["tax_id"] = ""

        # Get Company Address from Dynamic Link with is_your_company_address = 1
        company_address = {}
        try:
            # Find company address with is_your_company_address = 1
            company_address_links = frappe.db.sql("""
                SELECT dl.parent 
                FROM `tabDynamic Link` dl
                INNER JOIN `tabAddress` addr ON dl.parent = addr.name
                WHERE dl.link_doctype = 'Company' 
                AND dl.link_name = %s
                AND dl.parenttype = 'Address'
                AND addr.is_your_company_address = 1
            """, (invoice.company,), as_dict=True)
            
            if company_address_links:
                address_name = company_address_links[0].parent
                address_doc = frappe.get_doc("Address", address_name)
                company_address = {
                    "address_line1": address_doc.address_line1 or "",
                    "address_line2": address_doc.address_line2 or "",
                    "custom_building_number": getattr(address_doc, 'custom_building_number', '') or "",
                    "custom_area": getattr(address_doc, 'custom_area', '') or "",
                    "city": address_doc.city or "",
                    "state": address_doc.state or "",
                    "county": address_doc.county or "",
                    "pincode": address_doc.pincode or ""
                }
        except Exception as e:
            frappe.log_error(f"Error fetching company address: {str(e)}", "Invoice Details Error")
            company_address = {
                "address_line1": "", "address_line2": "", "custom_building_number": "",
                "custom_area": "", "city": "", "state": "", "county": "", "pincode": ""
            }

        # Get Default Letterhead HTML (Header and Footer)
        letterhead_data = {}
        try:
            letterhead = frappe.get_doc("Letter Head", "Default_Letterhead")
            letterhead_data = {
                "name": "Default_Letterhead",
                "header_html": letterhead.content or "",
                "footer_html": letterhead.footer or ""
            }
        except Exception as e:
            frappe.log_error(f"Error fetching letterhead: {str(e)}", "Invoice Details Error")
            letterhead_data = {
                "name": "Default_Letterhead",
                "header_html": "",
                "footer_html": ""
            }

        response_data = {
            "invoice_id": invoice_id,
            "posting_date": invoice.posting_date,
            "due_date": invoice.due_date,
            
            # Customer Section with Address
            "customer_info": {
                **customer_info,
                "address": customer_address
            },
            
            # Company Section with Address  
            "company_info": {
                **company_info,
                "address": company_address
            },
            
            # Legacy fields for backward compatibility
            "customer": invoice.customer,
            "customer_name": invoice.customer_name,
            "tax_id": invoice.tax_id if invoice.tax_id else "",
            "company": invoice.company,
            
            "payment_type": invoice.custom_payment_type,
            "items": response_items,
            "total": invoice.total,
            "net_total": invoice.net_total,
            "total_taxes_and_charges": invoice.total_taxes_and_charges,
            "tax_amount": invoice.total_taxes_and_charges,  # Added tax_amount field
            "additional_discount_amount": invoice.discount_amount,
            "grand_total": invoice.grand_total,
            "sales_person": person,
            "status": invoice.status,
            "qr_code": qr_code_data,
            "letterhead": letterhead_data  # Added letterhead data
        }
        return response("Sales Invoice Details", response_data, True, 200)

    except frappe.DoesNotExistError:
        return response(f"Sales Invoice '{invoice_id}' not found", {}, False, 404)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Invoice Details Fetch Error")
        return response(str(e), {}, False, 417)
    



@frappe.whitelist(methods="POST")
def create_sales_invoice_return(params):
    try:
        if isinstance(params, str):
            params = json.loads(params)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        customer = params.get("customer")

        settings = get_basket4me_settings()
        sales_person_details = None

        for detail in settings.sales_person_details:
            if detail.sales_person == sales_person:
                sales_person_details = detail

        if not sales_person_details:
            return response(f"No Basket4Me Settings found for sales person {sales_person}", {}, False, 400)

        # Get effective price list
        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)

        customer_name = params.get("customer_name")
        tax_id = params.get("tax_id")
        items = params.get("items")
        due_date = params.get("due_date")
        posting_date = params.get("posting_date")
        payment_type = params.get("custom_payment_type") or params.get("payment_type")
        mode_of_payment = params.get("mode_of_payment")
        return_against = params.get("return_against")
        return_reason = params.get("custom_return_reason") or params.get("return_reason")

        # If mode_of_payment is provided, derive custom_payment_type from its type
        if mode_of_payment and not payment_type:
            try:
                mop_type = frappe.db.get_value("Mode of Payment", mode_of_payment, "type")
                if mop_type == "Cash":
                    payment_type = "Cash"
                else:
                    payment_type = "Credit"
            except Exception:
                payment_type = "Credit"

        if not customer:
            return response("Customer is required", {}, False, 400)
        if not items or not isinstance(items, list):
            return response("At least one item is required", {}, False, 400)

        # ── Validate return quantities against balance (using stock_qty for UOM independence) ──
        if return_against:
            orig_items = frappe.get_all(
                "Sales Invoice Item",
                filters={"parent": return_against},
                fields=["item_code", "stock_qty"],
            )
            orig_stock_qty_map = {}
            for oi in orig_items:
                orig_stock_qty_map[oi.item_code] = orig_stock_qty_map.get(oi.item_code, 0) + flt(oi.stock_qty)

            # Already-returned stock quantities from submitted returns only
            # (drafts excluded at creation time — submit-time check guards against over-returning)
            existing_returns = frappe.get_all(
                "Sales Invoice",
                filters={
                    "return_against": return_against,
                    "docstatus": 1,
                },
                pluck="name",
            )
            returned_stock_qty_map = {}
            for ret_name in existing_returns:
                ret_items = frappe.get_all(
                    "Sales Invoice Item",
                    filters={"parent": ret_name},
                    fields=["item_code", "stock_qty"],
                )
                for ri in ret_items:
                    returned_stock_qty_map[ri.item_code] = returned_stock_qty_map.get(ri.item_code, 0) + abs(flt(ri.stock_qty))

            errors = []
            for item in items:
                ic = item.get("item_code")
                req_qty = abs(flt(item.get("qty")))
                req_uom = item.get("uom")

                # Convert requested qty to stock qty using conversion factor
                conversion_factor = 1.0
                if req_uom:
                    cf = frappe.db.get_value(
                        "UOM Conversion Detail",
                        {"parent": ic, "parenttype": "Item", "uom": req_uom},
                        "conversion_factor",
                    )
                    if cf:
                        conversion_factor = flt(cf)
                req_stock_qty = req_qty * conversion_factor

                orig_stock = orig_stock_qty_map.get(ic, 0)
                already_returned_stock = returned_stock_qty_map.get(ic, 0)
                balance_stock = orig_stock - already_returned_stock

                if balance_stock <= 0:
                    errors.append(f"{ic}: already fully returned")
                elif req_stock_qty > balance_stock + 0.01:  # small tolerance for float
                    # Show balance in requested UOM for clarity
                    balance_in_uom = balance_stock / conversion_factor
                    errors.append(f"{ic}: requested {req_qty:.0f} {req_uom or ''} but only {balance_in_uom:.1f} {req_uom or ''} remaining")

            if errors:
                return response(
                    "Return quantity exceeds balance:\n" + "\n".join(errors),
                    {}, False, 400,
                )

        sales_invoice = frappe.new_doc("Sales Invoice")
        sales_invoice.customer = customer
        sales_invoice.customer_name = customer_name
        sales_invoice.tax_id = tax_id
        sales_invoice.due_date = due_date
        sales_invoice.posting_date = posting_date
        sales_invoice.is_return = 1
        sales_invoice.custom_mobile_app = 1
        sales_invoice.custom_payment_type = payment_type
        if return_against:
            sales_invoice.return_against = return_against
        # Set update_stock for returns:
        # - With return_against: match original invoice's update_stock
        # - Standalone returns: use Basket4Me Settings ignore_create_delivery_note
        # Always allow negative stock for returns (stock goes back to warehouse)
        vs_settings = get_basket4me_settings()
        if return_against:
            orig_update_stock = frappe.db.get_value("Sales Invoice", return_against, "update_stock")
            sales_invoice.update_stock = orig_update_stock or 0
        elif vs_settings.ignore_create_delivery_note:
            sales_invoice.update_stock = 1
        else:
            sales_invoice.update_stock = 0

        # Allow negative stock for returns — prevents "X units needed in Warehouse" error
        frappe.flags.ignore_negative_stock = True
        # Set return reason if field exists (KSA compliance - mandatory for returns)
        if return_reason and hasattr(sales_invoice, 'custom_return_reason'):
            sales_invoice.custom_return_reason = return_reason
        elif hasattr(sales_invoice, 'custom_return_reason'):
            sales_invoice.custom_return_reason = "Return"
        sales_invoice.cost_center = sales_person_details.cost_center
        sales_invoice.selling_price_list = effective_price_list  # Use effective price list
        sales_invoice.set_posting_time = 1
        sales_invoice.company = sales_person_details.company

        # Build a map of original invoice item rates (for return_against returns)
        orig_item_rate_map = {}
        if return_against:
            orig_si_items = frappe.get_all(
                "Sales Invoice Item",
                filters={"parent": return_against},
                fields=["item_code", "rate", "price_list_rate", "discount_percentage", "discount_amount", "uom"],
            )
            for osi in orig_si_items:
                orig_item_rate_map[osi.item_code] = osi

        response_items = []
        for item in items:
            item_code = item.get("item_code")
            qty = item.get("qty")
            uom = item.get("uom")
            description = item.get("description")
            # Strip HTML tags from description
            if description:
                description = strip_html_tags(description)
            provided_rate = item.get("rate")
            is_free_item = item.get("is_free_item", False)  # Get is_free_item field, default to False

            # For return-against returns, use rates from the original invoice
            if is_free_item:
                rate = 0
            elif return_against and item_code in orig_item_rate_map:
                orig_item = orig_item_rate_map[item_code]
                rate = flt(orig_item.rate)
            else:
                # Fallback: look up from Item Price table
                item_price = frappe.db.sql(
                    """
                    SELECT price_list_rate
                    FROM `tabItem Price`
                    WHERE item_code = %(item_code)s
                    AND uom = %(uom)s
                    AND price_list = %(price_list)s
                    ORDER BY uom DESC, creation DESC
                    LIMIT 1
                    """,
                    {"item_code": item_code, "uom": uom, "price_list": effective_price_list},
                    as_dict=True
                )
                latest_item_price = item_price[0]["price_list_rate"] if item_price else None
                rate = latest_item_price if latest_item_price is not None else provided_rate

            # For return-against: copy price/discount from original invoice item
            if return_against and item_code in orig_item_rate_map and not is_free_item:
                orig_item = orig_item_rate_map[item_code]
                price_list_rate = flt(orig_item.price_list_rate)
                discount_percentage = flt(orig_item.discount_percentage)
                discount_amount = flt(orig_item.discount_amount)
                discounted_rate = flt(orig_item.rate)
            else:
                price_list_rate = flt(rate)

                provided_discount_percentage = flt(item.get("discount_percentage", 0))
                provided_discount_amount = flt(item.get("discount_amount", 0))

                if provided_discount_percentage and price_list_rate:
                    discount_percentage = provided_discount_percentage
                    discount_amount = (price_list_rate * discount_percentage) / 100
                elif provided_discount_amount and price_list_rate:
                    discount_amount = provided_discount_amount
                    discount_percentage = (discount_amount / price_list_rate) * 100
                else:
                    discount_percentage = 0
                    discount_amount = 0

                discounted_rate = price_list_rate - discount_amount

            item_data = {
                "item_code": item_code,
                "qty": qty,
                "uom": uom,
                "description": description,
                "warehouse": sales_person_details.warehouse,
                "cost_center": sales_person_details.cost_center,
                "discount_percentage": discount_percentage,
                "discount_amount": discount_amount,
                "rate": discounted_rate,  # Use the discounted rate
                "stock_uom_rate": discounted_rate,  # Set stock UOM rate same as discounted rate
                "price_list_rate": price_list_rate,  # Original price before discount
                "base_rate": discounted_rate,  # Set base rate same as discounted rate
                "base_price_list_rate": price_list_rate,  # Original price before discount
                "is_free_item": is_free_item  # Set the is_free_item field in the invoice item
            }

            # Handle batch selection for pharmaceutical/batch-tracked items
            batch_no = item.get("batch_no")
            if batch_no:
                item_data["use_serial_batch_fields"] = 1
                item_data["batch_no"] = batch_no

            sales_invoice.append("items", item_data)

            response_item ={
                "item_code": item_code,
                "description": description,
                "qty": qty,
                "uom": uom,
                "discount_amount": discount_amount,
                "discount_percentage": discount_percentage,
                "price_list_rate": price_list_rate,
                "rate": discounted_rate,
                "is_free_item": is_free_item  # Include is_free_item in the response
            }

            response_items.append(response_item)

        sales_invoice.write_off_outstanding_amount_automatically = 1

        frappe.flags.ignore_permissions = True
        sales_invoice.run_method("set_missing_values")
        sales_invoice.run_method("set_other_charges")
        frappe.flags.ignore_permissions = False
        sales_invoice.run_method("calculate_taxes_and_totals")

        # Enforce rate = 0 for free items after all calculations
        enforce_free_item_rates(sales_invoice)

        sales_invoice.append("sales_team", {
            "sales_person": sales_person,
            "allocated_percentage": 100
        })

        # Embed payment directly in the invoice (POS mode) instead of separate Payment Entry
        if mode_of_payment:
            sales_invoice.is_pos = 1
            sales_invoice.append("payments", {
                "mode_of_payment": mode_of_payment,
                "amount": flt(sales_invoice.grand_total)  # Negative for returns
            })

        sales_invoice.flags.ignore_permissions = True

        # Bypass permission checks for Serial and Batch Bundle / Batch created
        # during on_submit hooks (stock ledger processing for batch items).
        from frappe.model.document import Document
        _orig_check_perm = Document.check_permission
        _bypass_doctypes = ("Serial and Batch Bundle", "Batch", "Stock Ledger Entry")
        def _patched_check_perm(self, permtype='read', permlevel=None):
            if self.doctype in _bypass_doctypes:
                return
            return _orig_check_perm(self, permtype, permlevel)
        Document.check_permission = _patched_check_perm
        try:
            sales_invoice.save()
            sales_invoice.submit()
        finally:
            Document.check_permission = _orig_check_perm
            frappe.flags.ignore_negative_stock = False

        response_data = {
            "invoice_id": sales_invoice.name,
            "posting_date": posting_date,
            "customer": customer,
            "customer_name": sales_invoice.customer_name,
            "tax_id": tax_id,
            "payment_type": payment_type,
            "sales_person": sales_person,
            "company": sales_invoice.company,
            "items": response_items,
            "total": sales_invoice.total,
            "net_total": sales_invoice.net_total,
            "total_taxes_and_charges": sales_invoice.total_taxes_and_charges,
            "grand_total": sales_invoice.grand_total,
            "status": sales_invoice.status,
            "company": sales_invoice.company,
            "effective_price_list": effective_price_list  # Include for reference
        }

        return response("Sales Invoice Details", response_data, True, 201)

    except Exception as e:
        frappe.flags.ignore_negative_stock = False
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Sales Invoice Creation Error")
        return response(str(e), {}, False, 417)


# Payment Entry Api

@frappe.whitelist(methods="GET")
def get_receipt_list(name=None, customer=None, status=None, search=None, from_date=None, to_date=None, payment_type=None, page=1, page_size=20):
    """
    Get list of Payment Entries (Receipts/Payments).

    Query params:
        name: Filter by exact PE name
        customer: Filter by customer
        status: Draft / Submitted / Cancelled
        search: Search by name or party_name
        from_date / to_date: Date range filter
        payment_type: "Receive" (Receipt) or "Pay" (Payment) or None (all)
        page / page_size: Pagination
    """
    try:
        filters = {"party_type": "Customer"}
        fields = [
            'name', 'party', 'party_name', 'posting_date', 'paid_amount',
            'mode_of_payment', 'payment_type', 'docstatus', 'status', 'creation',
            'reference_no', 'reference_date',
        ]

        if name:
            filters['name'] = name

        if customer:
            filters['party'] = customer

        if payment_type:
            filters['payment_type'] = payment_type

        if status:
            if status == "Draft":
                filters['docstatus'] = 0
            elif status == "Submitted":
                filters['docstatus'] = 1
            elif status == "Cancelled":
                filters['docstatus'] = 2

        if from_date and to_date:
            filters['posting_date'] = ["between", [from_date, to_date]]
        elif from_date:
            filters['posting_date'] = [">=", from_date]
        elif to_date:
            filters['posting_date'] = ["<=", to_date]

        # Search by name or party_name
        or_filters = None
        if search:
            or_filters = [
                ["name", "like", f"%{search}%"],
                ["party_name", "like", f"%{search}%"],
            ]

        page = int(page or 1)
        page_size = int(page_size or 20)
        start = (page - 1) * page_size

        receipt_list = frappe.db.get_list(
            "Payment Entry",
            filters=filters,
            or_filters=or_filters,
            fields=fields,
            order_by="posting_date desc, creation desc",
            limit_start=start,
            limit_page_length=page_size,
        )

        total_count = frappe.db.count("Payment Entry", filters=filters)

        return response("Receipt List", {"receipts": receipt_list, "total_count": total_count, "page": page, "page_size": page_size}, True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)
    



@frappe.whitelist(methods="GET")
def get_customer_invoices(customer=None, customer_name=None):
    try:
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        override_enabled = should_override_sales_team()
        
        if override_enabled:
            # When override is enabled, show ALL customers with outstanding invoices
            # Use raw SQL to bypass all restrictions
            conditions = ["status IN ('Unpaid', 'Overdue', 'Return', 'Partly Paid')"]
            values = []
            
            if customer:
                conditions.append("customer = %s")
                values.append(customer)
            if customer_name:
                conditions.append("customer_name LIKE %s")
                values.append(f"%{customer_name}%")
            
            where_clause = " AND ".join(conditions)
            
            sql = f"""
                SELECT customer, customer_name, SUM(outstanding_amount) as total_outstanding
                FROM `tabSales Invoice`
                WHERE {where_clause}
                GROUP BY customer
                HAVING total_outstanding > 0
                ORDER BY total_outstanding DESC
            """
            
            invoice_data = frappe.db.sql(sql, values, as_dict=True)
            
        else:
            # Original logic with sales team restrictions
            SalesInvoice = DocType("Sales Invoice")
            SalesTeam = DocType("Sales Team")
            
            query = (
                frappe.qb.from_(SalesInvoice)
                .inner_join(SalesTeam)
                .on(SalesTeam.parent == SalesInvoice.name)
                .select(
                    SalesInvoice.customer,
                    SalesInvoice.customer_name,
                    Sum(SalesInvoice.outstanding_amount).as_("total_outstanding")
                )
                .where(
                    (SalesInvoice.status.isin(["Unpaid", "Overdue", "Return", "Partly Paid"])) &
                    (SalesTeam.sales_person == sales_person)
                )
                .groupby(SalesInvoice.customer)
            )

            if customer:
                query = query.where(SalesInvoice.customer == customer)
            if customer_name:
                query = query.where(SalesInvoice.customer_name.like(f"%{customer_name}%"))

            invoice_data = query.run(as_dict=True)

        if invoice_data:
            return response("Customer Invoice List", invoice_data, True, 200)
        else:
            return response("No Outstanding Invoices Found", [], True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


@frappe.whitelist(methods="POST")
def customer_invoice_details(customer=None):
    try:
        if not customer:
            return response("Customer must be provided", [], False, 400)
        
        # Check if override is enabled
        override_enabled = should_override_sales_team()
        
        if override_enabled:
            # Bypass Frappe permissions when override is enabled
            # Get customer details using raw SQL
            customer_data = frappe.db.sql("""
                SELECT name, customer_name, tax_id, custom_vat_registration_number
                FROM `tabCustomer`
                WHERE name = %s
            """, (customer,), as_dict=True)
            
            if not customer_data:
                return response("Customer not found", {}, False, 404)
            
            customer_info = customer_data[0]
            
            # Get invoices using raw SQL to bypass permissions
            invoices = frappe.db.sql("""
                SELECT name as invoice_id, posting_date, due_date, grand_total, 
                       outstanding_amount, status
                FROM `tabSales Invoice`
                WHERE customer = %s 
                AND status IN ('Unpaid', 'Overdue', 'Partly Paid', 'Return')
                ORDER BY posting_date DESC
            """, (customer,), as_dict=True)
            
        else:
            # Use standard Frappe methods with permission checking
            customer_info = frappe.get_doc("Customer", customer)

            invoices = frappe.db.get_list(
                "Sales Invoice",
                filters={"customer": customer_info.name,"status": ["in", ["Unpaid", "Overdue", "Partly Paid", "Return"]]},
                fields=["name as invoice_id","posting_date", "due_date", "grand_total", "outstanding_amount", "status"],
                order_by="posting_date DESC"
            )
            
            # Convert to dict for consistent response format
            customer_info = {
                "name": customer_info.name,
                "customer_name": customer_info.customer_name,
                "tax_id": customer_info.tax_id if customer_info.tax_id else ""
            }

        response_data = {
            "customer": customer_info["name"],
            "customer_name": customer_info["customer_name"],
            "tax_id": customer_info["tax_id"] if customer_info.get("tax_id") else "",
            "invoices": invoices
        }

        if response_data["invoices"]:
            return response("Customer Invoice Details", response_data, True, 200)
        else:
            return response("No Invoices Found for this Customer", response_data, True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_available_modes_of_payment():
    """
    Get available modes of payment based on payment_entry_based_on_sales_person setting
    If enabled: Returns sales person specific mode + universal modes from settings
    If disabled: Returns all available modes of payment from the system
    """
    try:
        settings = get_basket4me_settings()
        
        # Check the new setting
        payment_based_on_sales_person = getattr(settings, 'payment_entry_based_on_sales_person', True)
        
        if payment_based_on_sales_person:
            # Sales person based logic - use current user's sales person
            sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
            
            if not sales_person:
                return response("No Sales Person linked to the logged-in user", {}, False, 400)
            
            # Find sales person details for current user
            sales_person_details = None
            for detail in settings.sales_person_details:
                if detail.sales_person == sales_person:
                    sales_person_details = detail
                    break
            
            if not sales_person_details:
                return response(f"No Basket4Me Settings found for sales person {sales_person}", {}, False, 400)
            
            available_modes = []
            
            # Add sales person specific mode if found
            if sales_person_details and sales_person_details.mode_of_payment:
                mode_doc = frappe.get_doc("Mode of Payment", sales_person_details.mode_of_payment)
                available_modes.append({
                    "mode_of_payment": sales_person_details.mode_of_payment,
                    "type": "sales_person_specific",
                    "mop_type": mode_doc.type if mode_doc else "Cash",
                    "sales_person": sales_person,
                    "enabled": mode_doc.enabled if mode_doc else 1
                })

            # Add universal modes from Mode Of Payment Details table
            # Filter by sales person's company (if company is set on the MOP Detail row)
            sales_person_mode = sales_person_details.mode_of_payment if sales_person_details else None
            sp_company = sales_person_details.company if sales_person_details else None
            for mode_detail in settings.mode_of_payment_details:
                # Skip if MOP Detail has a company set and it doesn't match the sales person's company
                if sp_company and getattr(mode_detail, 'company', None) and mode_detail.company != sp_company:
                    continue
                # Avoid duplicates
                if mode_detail.mode_of_payment != sales_person_mode:
                    mode_doc = frappe.get_doc("Mode of Payment", mode_detail.mode_of_payment)
                    available_modes.append({
                        "mode_of_payment": mode_detail.mode_of_payment,
                        "type": "universal",
                        "mop_type": mode_doc.type if mode_doc else "Bank",
                        "sales_person": None,
                        "enabled": mode_doc.enabled if mode_doc else 1
                    })
            
            return response("Available modes of payment retrieved successfully", {
                "sales_person": sales_person,
                "modes_of_payment": available_modes,
                "total_modes": len(available_modes),
                "payment_based_on_sales_person": True,
                "override_enabled": False  # When using sales person based, no override
            }, True, 200)
            
        else:
            # Show sales person's MOP + universal modes filtered by company
            available_modes = []
            sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
            sp_company = None

            # Add sales person's own MOP first
            if sales_person:
                for detail in settings.sales_person_details:
                    if detail.sales_person == sales_person:
                        sp_company = detail.company
                        if detail.mode_of_payment:
                            mode_doc = frappe.get_doc("Mode of Payment", detail.mode_of_payment)
                            available_modes.append({
                                "mode_of_payment": detail.mode_of_payment,
                                "type": "sales_person_specific",
                                "mop_type": mode_doc.type if mode_doc else "Cash",
                                "sales_person": sales_person,
                                "enabled": mode_doc.enabled if mode_doc else 1
                            })
                        break

            # Add universal modes filtered by company
            sp_mode = available_modes[0]["mode_of_payment"] if available_modes else None
            for mode_detail in settings.mode_of_payment_details:
                if sp_company and getattr(mode_detail, 'company', None) and mode_detail.company != sp_company:
                    continue
                if mode_detail.mode_of_payment != sp_mode:
                    mode_doc = frappe.get_doc("Mode of Payment", mode_detail.mode_of_payment)
                    available_modes.append({
                        "mode_of_payment": mode_detail.mode_of_payment,
                        "type": "universal",
                        "mop_type": mode_doc.type if mode_doc else "Bank",
                        "sales_person": None,
                        "enabled": mode_doc.enabled if mode_doc else 1
                    })
            
            return response("Available modes of payment retrieved successfully", {
                "sales_person": None,
                "modes_of_payment": available_modes,
                "total_modes": len(available_modes),
                "payment_based_on_sales_person": False,
                "override_enabled": False
            }, True, 200)
        
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


@frappe.whitelist(methods="POST")
def create_payment_entry(params=None):
    try:
        # Support both params wrapper and raw body
        if params:
            if isinstance(params, str):
                params = json.loads(params)
        else:
            params = frappe.parse_json(frappe.request.get_data().decode())
        
        # Validate required parameters
        required_params = ["party", "paid_amount", "invoices"]
        missing_params = []
        for param in required_params:
            if param not in params:
                missing_params.append(param)
        
        if missing_params:
            return response(f"Missing required parameters: {', '.join(missing_params)}", {}, False, 400)
        
        # Parse invoices if it's a JSON string
        invoices_raw = params.get("invoices", [])
        if isinstance(invoices_raw, str):
            invoices_raw = json.loads(invoices_raw)
        params["invoices"] = invoices_raw

        # Validate invoices parameter
        if not isinstance(params["invoices"], list) or len(params["invoices"]) == 0:
            return response("Parameter 'invoices' must be a non-empty list", {}, False, 400)

        settings = get_basket4me_settings()

        # Validate deduction account and cost center if deductions are provided
        deductions_data = params.get("deductions", params.get("deduction", []))  # Support both 'deductions' and 'deduction'
        if isinstance(deductions_data, str):
            deductions_data = json.loads(deductions_data)
        if deductions_data:
            if not sales_person_details.deduction_account:
                return response("Deduction Account not configured in Sales Person Details", {}, False, 400)
            if not sales_person_details.cost_center:
                return response("Cost Center not configured in Sales Person Details", {}, False, 400)
        
        # Check the new setting
        payment_based_on_sales_person = getattr(settings, 'payment_entry_based_on_sales_person', True)
        
        if payment_based_on_sales_person:
            # Sales person based logic - use current user's sales person
            sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
            
            if not sales_person:
                return response("No Sales Person linked to the logged-in user", {}, False, 400)
            
            # Find sales person details for current user
            sales_person_details = None
            for detail in settings.sales_person_details:
                if detail.sales_person == sales_person:
                    sales_person_details = detail
                    break
            
            if not sales_person_details:
                return response(f"No Basket4Me Settings found for sales person {sales_person}", {}, False, 400)

            company = frappe.get_doc('Company', {"name": sales_person_details.company})

            # Enhanced Mode of Payment Selection Logic - Sales Person Based
            selected_mode_of_payment = params.get("mode_of_payment")

            # Validate and determine mode of payment
            if selected_mode_of_payment:
                # Accept any valid ERPNext Mode of Payment OR one from Basket4Me Settings
                valid_modes = []
                if sales_person_details and sales_person_details.mode_of_payment:
                    valid_modes.append(sales_person_details.mode_of_payment)
                for mode_detail in settings.mode_of_payment_details:
                    valid_modes.append(mode_detail.mode_of_payment)

                if selected_mode_of_payment in valid_modes or frappe.db.exists("Mode of Payment", selected_mode_of_payment):
                    mode_of_payment = selected_mode_of_payment
                else:
                    return response(f"Invalid mode of payment '{selected_mode_of_payment}'. Available modes: {', '.join(valid_modes)}", {}, False, 400)
            else:
                # No mode specified - use configured modes only (never silently default to sales person name)
                valid_modes = [m.mode_of_payment for m in settings.mode_of_payment_details]
                if valid_modes:
                    mode_of_payment = valid_modes[0]
                else:
                    # Fallback to first universal mode if no sales person specific mode
                    if settings.mode_of_payment_details:
                        mode_of_payment = settings.mode_of_payment_details[0].mode_of_payment
                    else:
                        return response("No mode of payment configured in Basket4Me Settings", {}, False, 400)
        
        else:
            # Mode of payment details only (with sales person fallback)
            sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")

            # Still look up sales person details for fallback mode
            sales_person_details = None
            if sales_person:
                for detail in settings.sales_person_details:
                    if detail.sales_person == sales_person:
                        sales_person_details = detail
                        break

            company = frappe.get_doc('Company', {"name": sales_person_details.company if sales_person_details else None})

            # Build valid modes: universal modes + sales person mode
            valid_modes = []
            for mode_detail in settings.mode_of_payment_details:
                valid_modes.append(mode_detail.mode_of_payment)
            if sales_person_details and sales_person_details.mode_of_payment:
                if sales_person_details.mode_of_payment not in valid_modes:
                    valid_modes.append(sales_person_details.mode_of_payment)

            selected_mode_of_payment = params.get("mode_of_payment")

            if selected_mode_of_payment:
                # Validate against both Basket4Me Settings modes AND ERPNext Mode of Payment list
                if selected_mode_of_payment in valid_modes or frappe.db.exists("Mode of Payment", selected_mode_of_payment):
                    mode_of_payment = selected_mode_of_payment
                else:
                    return response(f"Invalid mode of payment '{selected_mode_of_payment}'. Available modes: {', '.join(valid_modes)}", {}, False, 400)
            else:
                if valid_modes:
                    mode_of_payment = valid_modes[0]
                else:
                    return response("No mode of payment configured. Please pass mode_of_payment parameter.", {}, False, 400)
        
        # Get account for selected mode of payment
        mode_doc = frappe.get_doc("Mode of Payment", mode_of_payment)
        if not mode_doc.accounts:
            return response(f"No account configured for mode of payment '{mode_of_payment}'", {}, False, 400)
        
        paid_to_account = mode_doc.accounts[0].default_account

        # Normalize invoice field names to support multiple formats
        normalized_invoices = []
        for inv in params["invoices"]:
            normalized_invoices.append({
                "invoice_id": inv.get("invoice_id") or inv.get("invoice") or inv.get("reference_name"),
                "allocated_amount": flt(inv.get("allocated_amount") or inv.get("amount") or 0),
            })
        params["invoices"] = normalized_invoices

        # Validate all invoice_ids are provided
        for inv in params["invoices"]:
            if not inv.get("invoice_id"):
                return response("Missing required parameter: 'invoice_id' (or 'invoice' or 'reference_name') in invoices array", {}, False, 400)

        return_invoices = []
        non_return_invoices = []
        payment_entry = None  # Initialize to avoid unbound variable

        for invoice_data in params["invoices"]:
            invoice = frappe.get_doc("Sales Invoice", invoice_data["invoice_id"])
            if invoice.is_return:
                return_invoices.append(invoice_data)
            else:
                non_return_invoices.append(invoice_data)

        for invoice_data in return_invoices:
            invoice = frappe.get_doc("Sales Invoice", invoice_data["invoice_id"])
            payment_entry = frappe.new_doc("Payment Entry")
            payment_entry.party_type = "Customer"
            payment_entry.party = params["party"]
            payment_entry.payment_type = "Pay"
            payment_entry.posting_date = params.get("reference_date")
            payment_entry.paid_amount = params["paid_amount"]
            payment_entry.received_amount = params["paid_amount"]
            payment_entry.reference_no = params.get("reference_no")
            payment_entry.reference_date = params.get("reference_date")
            payment_entry.mode_of_payment = mode_of_payment
            payment_entry.paid_to = company.default_receivable_account
            payment_entry.paid_from = paid_to_account
            payment_entry.target_exchange_rate = 1
            payment_entry.source_exchange_rate = 1
            payment_entry.custom_sales_person = sales_person
            payment_entry.cost_center = sales_person_details.cost_center if sales_person_details else None
            payment_entry.company = sales_person_details.company if sales_person_details else None
            outstatnding = abs(invoice_data["allocated_amount"]) - params["paid_amount"]
            payment_entry.append("references", {
                "reference_doctype": "Sales Invoice",
                "reference_name": invoice.name,
                "outstanding_amount": outstatnding,
                "allocated_amount": invoice_data["allocated_amount"]
            })
            
            # Add deductions for return invoices
            for deduction in deductions_data:
                payment_entry.append("deductions", {
                    "account": sales_person_details.deduction_account,
                    "cost_center": sales_person_details.cost_center,
                    "amount": deduction["amount"]
                })

            payment_entry.flags.ignore_permissions = True
            payment_entry.insert(ignore_permissions=True)

        if non_return_invoices:
            payment_entry = frappe.new_doc("Payment Entry")
            payment_entry.party_type = "Customer"
            payment_entry.party = params["party"]
            payment_entry.payment_type = "Receive"
            payment_entry.paid_amount = params["paid_amount"]
            payment_entry.received_amount = params["paid_amount"]
            payment_entry.reference_no = params.get("reference_no")
            payment_entry.reference_date = params.get("reference_date")
            payment_entry.mode_of_payment = mode_of_payment
            payment_entry.paid_to = paid_to_account
            payment_entry.paid_from = company.default_receivable_account
            payment_entry.target_exchange_rate = 1
            payment_entry.source_exchange_rate = 1
            payment_entry.custom_sales_person = sales_person
            payment_entry.cost_center = sales_person_details.cost_center if sales_person_details else None
            payment_entry.company = sales_person_details.company if sales_person_details else None
            for invoice_data in non_return_invoices:
                invoice = frappe.get_doc("Sales Invoice", invoice_data["invoice_id"])
                payment_entry.append("references", {
                    "reference_doctype": "Sales Invoice",
                    "reference_name": invoice.name,
                    "allocated_amount": invoice_data.get("allocated_amount")
                })
            for deduction in deductions_data:
                payment_entry.append("deductions", {
                    "account": sales_person_details.deduction_account,
                    "cost_center": sales_person_details.cost_center,
                    "amount": deduction["amount"]
                })

            payment_entry.flags.ignore_permissions = True
            payment_entry.insert(ignore_permissions=True)

        return response("Payment Entry created as Draft", {
            "receipt_id": payment_entry.name,
            "docstatus": 0,
            "status": "Draft",
            "mode_of_payment": mode_of_payment,
            "sales_person": sales_person,
            "total_amount": params["paid_amount"]
        }, True, 200)

    except KeyError as ke:
        frappe.db.rollback()
        error_msg = f"Missing required parameter: {str(ke)}"
        frappe.log_error(f"KeyError in create_payment_entry: {error_msg}\n\nRequest params: {frappe.request.get_data().decode()}", "Payment Entry Creation Error")
        return response(error_msg, {}, False, 400)
    except Exception as exception:
        frappe.db.rollback()
        error_msg = str(exception)
        frappe.log_error(f"Exception in create_payment_entry: {error_msg}\n\nRequest params: {frappe.request.get_data().decode()}\n\nTraceback: {frappe.get_traceback()}", "Payment Entry Creation Error")
        return response(error_msg, {}, False, 417)



@frappe.whitelist(methods="GET")
def get_return_invoice_list(name=None, customer=None, status=None, search=None):
    try:
        fields = ['name', 'customer', 'customer_name', 'posting_date', 'grand_total', 'outstanding_amount', 'status', 'docstatus', 'return_against', 'creation']

        conditions = ["si.is_return = 1"]
        values = []

        if name:
            conditions.append("si.name = %s")
            values.append(name)

        if customer:
            conditions.append("si.customer = %s")
            values.append(customer)

        if status:
            conditions.append("si.status = %s")
            values.append(status)

        if search:
            conditions.append("(si.name LIKE %s OR si.customer_name LIKE %s)")
            values.append(f"%{search}%")
            values.append(f"%{search}%")

        where_clause = " AND ".join(conditions)
        si_fields = ", ".join([f"si.{f}" for f in fields])

        invoice_list = frappe.db.sql(
            f"SELECT {si_fields}, c.mobile_no FROM `tabSales Invoice` si LEFT JOIN `tabCustomer` c ON si.customer = c.name WHERE {where_clause} ORDER BY si.posting_date DESC, si.creation DESC",
            values,
            as_dict=True,
        )

        if invoice_list:
            return response("Invoice List", invoice_list, True, 200)
        else:
            return response("No Invoice List", [], True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_returned_qty(invoice_name=None):
    """Get already-returned quantities per item for a given invoice.
    Returns stock_qty (UOM-independent) for each item_code."""
    try:
        if not invoice_name:
            return response("Invoice name is required", {}, False, 400)

        # Find only submitted returns against this invoice (docstatus=1)
        # Draft returns should not block creating new returns
        returns = frappe.get_all(
            "Sales Invoice",
            filters={
                "return_against": invoice_name,
                "docstatus": 1,
            },
            pluck="name"
        )

        returned_items = {}
        for ret_name in returns:
            ret_items = frappe.get_all(
                "Sales Invoice Item",
                filters={"parent": ret_name},
                fields=["item_code", "stock_qty"]
            )
            for item in ret_items:
                code = item.get("item_code")
                if code not in returned_items:
                    returned_items[code] = 0
                returned_items[code] += abs(flt(item.get("stock_qty", 0)))

        return response("Returned Qty", returned_items, True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


@frappe.whitelist(methods="POST")
def receipt_details(receipt_id=None):
    try:
        receipt = frappe.get_doc("Payment Entry", receipt_id)
        response_items = []
        response_deductions = []

        for item in receipt.references:
            inv_data = {
                "invoice_id": item.reference_name,
                "name": item.reference_name,
                "reference_doctype": item.reference_doctype,
                "date": item.due_date,
                "invoice_amount": item.total_amount,
                "balance_amount": item.outstanding_amount,
                "allocated_amount": item.allocated_amount,
            }

            # Enrich with invoice-level fields (same as get_invoice_list)
            if item.reference_doctype == "Sales Invoice" and frappe.db.exists("Sales Invoice", item.reference_name):
                si = frappe.db.get_value("Sales Invoice", item.reference_name,
                    ["customer", "customer_name", "posting_date", "grand_total",
                     "outstanding_amount", "status", "docstatus", "creation"],
                    as_dict=True)
                if si:
                    inv_data.update({
                        "customer": si.customer,
                        "customer_name": si.customer_name,
                        "posting_date": str(si.posting_date) if si.posting_date else None,
                        "grand_total": si.grand_total,
                        "outstanding_amount": si.outstanding_amount,
                        "status": si.status,
                        "docstatus": si.docstatus,
                        "creation": str(si.creation) if si.creation else None,
                        "mobile_no": frappe.db.get_value("Customer", si.customer, "mobile_no"),
                    })

                    # Compute return_status
                    orig_qty = frappe.db.sql("""
                        SELECT item_code, SUM(stock_qty) as qty FROM `tabSales Invoice Item`
                        WHERE parent = %s GROUP BY item_code
                    """, item.reference_name, as_dict=True)
                    ret_qty = frappe.db.sql("""
                        SELECT sii.item_code, SUM(ABS(sii.stock_qty)) as qty
                        FROM `tabSales Invoice` si
                        JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
                        WHERE si.is_return = 1 AND si.docstatus = 1 AND si.return_against = %s
                        GROUP BY sii.item_code
                    """, item.reference_name, as_dict=True)

                    if not ret_qty:
                        inv_data["return_status"] = "not_returned"
                    else:
                        orig_map = {r.item_code: flt(r.qty) for r in orig_qty}
                        ret_map = {r.item_code: flt(r.qty) for r in ret_qty}
                        all_fully = all(flt(ret_map.get(ic, 0)) >= flt(oq) - 0.01 for ic, oq in orig_map.items())
                        inv_data["return_status"] = "fully_returned" if all_fully else "partially_returned"

            response_items.append(inv_data)

        for deduction in receipt.deductions:
            response_deductions.append({
                "account": deduction.account,
                "discount": deduction.amount,
            })

        # Customer details for print receipt
        customer_name = receipt.party
        customer_address = None
        customer_gstno = None
        customer_outstanding = 0.0
        customer_old_balance = 0.0

        if customer_name:
            # Get primary address
            addr_name = frappe.db.get_value("Dynamic Link",
                {"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Address"},
                "parent")
            if addr_name:
                addr = frappe.db.get_value("Address", addr_name,
                    ["address_line1", "address_line2", "city", "state", "pincode", "country", "gstin"],
                    as_dict=True)
                if addr:
                    parts = [addr.address_line1, addr.address_line2, addr.city, addr.state, addr.pincode, addr.country]
                    customer_address = ", ".join([p for p in parts if p])
                    customer_gstno = addr.gstin

            # If no GSTIN from address, check customer tax_id
            if not customer_gstno:
                customer_gstno = frappe.db.get_value("Customer", customer_name, "tax_id")

            # Total outstanding
            customer_outstanding = frappe.db.sql("""
                SELECT COALESCE(SUM(outstanding_amount), 0)
                FROM `tabSales Invoice`
                WHERE customer = %s AND docstatus = 1 AND outstanding_amount > 0
            """, customer_name)[0][0] or 0.0

            # Old balance = outstanding before this receipt's posting date
            if receipt.posting_date:
                customer_old_balance = frappe.db.sql("""
                    SELECT COALESCE(SUM(outstanding_amount), 0)
                    FROM `tabSales Invoice`
                    WHERE customer = %s AND docstatus = 1
                    AND posting_date < %s AND outstanding_amount > 0
                """, (customer_name, receipt.posting_date))[0][0] or 0.0

        # Received by
        received_by = frappe.db.get_value("User", receipt.owner, "full_name") or receipt.owner

        response_data = {
            "receipt_id": receipt_id,
            "posting_date": str(receipt.posting_date) if receipt.posting_date else None,
            "customer": receipt.party,
            "customer_name": receipt.party_name,
            "customer_address": customer_address,
            "customer_gstno": customer_gstno,
            "customer_old_balance": customer_old_balance,
            "customer_outstanding": customer_outstanding,
            "received_by": received_by,
            "paid_amount": receipt.paid_amount,
            "mode_of_payment": receipt.mode_of_payment,
            "payment_type": receipt.payment_type,
            "reference_no": receipt.reference_no,
            "reference_date": str(receipt.reference_date) if receipt.reference_date else None,
            "docstatus": receipt.docstatus,
            "status": receipt.status,
            "creation": str(receipt.creation) if receipt.creation else None,
            "invoice_details": response_items,
            "deduction_details": response_deductions,
        }
        return response("Receipt Details", response_data, True, 200)

    except frappe.DoesNotExistError:
        return response(f"Receipt '{receipt_id}' not found", {}, False, 404)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Receipt Details Fetch Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="POST")
def cancel_payment_entry(params):
    """Cancel a submitted Payment Entry."""
    try:
        if isinstance(params, str):
            params = json.loads(params)

        name = params.get("name")
        if not name:
            return response("Payment Entry name is required", {}, False, 400)

        pe = frappe.get_doc("Payment Entry", name)

        if pe.docstatus == 2:
            return response(f"Payment Entry '{name}' is already cancelled", {}, False, 400)
        if pe.docstatus == 0:
            return response(f"Payment Entry '{name}' is a draft, cannot cancel", {}, False, 400)

        pe.flags.ignore_permissions = True
        pe.cancel()

        return response(f"Payment Entry '{name}' cancelled successfully", {
            "name": name,
            "docstatus": pe.docstatus,
            "status": pe.status,
        }, True, 200)

    except frappe.DoesNotExistError:
        frappe.db.rollback()
        return response(f"Payment Entry '{name}' not found", {}, False, 404)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Cancel Payment Entry Error")
        error_msg = str(e).replace('<', '').replace('>', '')
        return response(error_msg, {}, False, 417)
    

@frappe.whitelist(methods="POST")
def delete_payment_entry(params):
    """Delete a draft or cancelled Payment Entry."""
    try:
        if isinstance(params, str):
            params = json.loads(params)

        name = params.get("name")
        if not name:
            return response("Payment Entry name is required", {}, False, 400)

        if not frappe.db.exists("Payment Entry", name):
            return response(f"Payment Entry '{name}' not found", {}, False, 404)

        pe = frappe.get_doc("Payment Entry", name)

        if pe.docstatus == 1:
            return response(f"Payment Entry '{name}' is submitted. Cancel it before deleting.", {}, False, 400)

        frappe.delete_doc("Payment Entry", name, ignore_permissions=True)
        frappe.db.commit()

        return response(f"Payment Entry '{name}' deleted successfully", {"name": name}, True, 200)

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Delete Payment Entry Error")
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def update_payment_entry(params):
    """Update a draft Payment Entry."""
    try:
        if isinstance(params, str):
            params = json.loads(params)

        name = params.get("name")
        if not name:
            return response("Payment Entry name is required", {}, False, 400)

        if not frappe.db.exists("Payment Entry", name):
            return response(f"Payment Entry '{name}' not found", {}, False, 404)

        pe = frappe.get_doc("Payment Entry", name)

        if pe.docstatus != 0:
            return response("Can only update Payment Entry in Draft state", {}, False, 400)

        # Update allowed fields
        if params.get("paid_amount") is not None:
            pe.paid_amount = flt(params.get("paid_amount"))
            pe.received_amount = flt(params.get("paid_amount"))

        if params.get("mode_of_payment"):
            pe.mode_of_payment = params.get("mode_of_payment")

        if params.get("posting_date"):
            pe.posting_date = params.get("posting_date")

        if params.get("reference_no"):
            pe.reference_no = params.get("reference_no")

        if params.get("reference_date"):
            pe.reference_date = params.get("reference_date")

        if params.get("remarks"):
            pe.remarks = params.get("remarks")

        # Update invoice references if provided
        if params.get("invoices") and isinstance(params.get("invoices"), list):
            pe.references = []
            for inv in params.get("invoices"):
                pe.append("references", {
                    "reference_doctype": inv.get("reference_doctype", "Sales Invoice"),
                    "reference_name": inv.get("reference_name") or inv.get("invoice"),
                    "allocated_amount": flt(inv.get("allocated_amount") or inv.get("amount")),
                })

        pe.save()
        frappe.db.commit()

        return response("Payment Entry updated successfully", {
            "name": pe.name,
            "docstatus": pe.docstatus,
            "status": pe.status,
            "paid_amount": pe.paid_amount,
            "mode_of_payment": pe.mode_of_payment,
        }, True, 200)

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Update Payment Entry Error")
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def submit_payment_entry(params):
    """Submit a draft Payment Entry."""
    try:
        if isinstance(params, str):
            params = json.loads(params)

        name = params.get("name")
        if not name:
            return response("Payment Entry name is required", {}, False, 400)

        if not frappe.db.exists("Payment Entry", name):
            return response(f"Payment Entry '{name}' not found", {}, False, 404)

        pe = frappe.get_doc("Payment Entry", name)

        if pe.docstatus == 1:
            return response(f"Payment Entry '{name}' is already submitted", {}, False, 400)
        if pe.docstatus == 2:
            return response(f"Payment Entry '{name}' is cancelled", {}, False, 400)

        pe.submit()
        frappe.db.commit()

        return response("Payment Entry submitted successfully", {
            "name": pe.name,
            "docstatus": pe.docstatus,
            "status": pe.status,
            "paid_amount": pe.paid_amount,
        }, True, 200)

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Submit Payment Entry Error")
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def create_material_request(params):
    try:
        if isinstance(params, str):
            params = frappe.parse_json(params)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        target = frappe.db.get_value("Basket4Me Settings", None, "default_target_warehouse")

        settings = get_basket4me_settings()
        sales_person_details = None

        for detail in settings.sales_person_details:
            if detail.sales_person == sales_person:
                sales_person_details = detail

        if not sales_person_details:
            frappe.throw(f"No Basket4Me Settings found for sales person {sales_person}")

        if not target:
            frappe.throw("Default Target Warehouse is not set in Basket4Me Settings.")

        schedule_date = params.get("schedule_date")
        transaction_date = params.get("transaction_date")
        is_return = params.get("is_return")
        items = params.get("items")

        if not items:
            frappe.throw("Items are required to create a Material Request.")
        
        material_request = frappe.new_doc("Material Request")
        material_request.material_request_type = "Material Transfer"
        material_request.transaction_date = transaction_date
        material_request.schedule_date = schedule_date
        material_request.is_return = is_return
        material_request.company = sales_person_details.company
        if is_return:
            material_request.set_from_warehouse = sales_person_details.warehouse
            material_request.set_warehouse = target
        else:
            material_request.set_warehouse = sales_person_details.warehouse

        response_items = []

        for item in items:
            if is_return:
                item_data = {
                    "item_code": item.get("item_code"),
                    "qty": item.get("qty"),
                    "uom": item.get("uom"),
                    "from_warehouse": sales_person_details.warehouse,
                    "warehouse": target,  
                    "cost_center": sales_person_details.cost_center,
                }
            else:
                item_data = {
                    "item_code": item.get("item_code"),
                    "qty": item.get("qty"),
                    "uom": item.get("uom"),
                    "warehouse": sales_person_details.warehouse,  
                    "cost_center": sales_person_details.cost_center,
                }

            material_request.append("items", item_data)

            response_items.append({
                "item_code": item.get("item_code"),
                "qty": item.get("qty"),
                "uom": item.get("uom"),
            })
        
        material_request.flags.ignore_permissions = True

        material_request.insert()

        response_data = {
            "request_id": material_request.name,
            "material_request_type": "Material Transfer",
            "transaction_date": transaction_date,
            "schedule_date": schedule_date,
            "is_return": is_return,
            "set_from_warehouse": material_request.set_from_warehouse if material_request.set_from_warehouse else "",
            "set_warehouse": material_request.set_warehouse,
            "company": material_request.company,
            "items": response_items,
            "status": material_request.status,
        }

        return response("Material Request created successfully.", response_data, True, 200)

    except frappe.DoesNotExistError:
        return response("Not found", {}, False, 404)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Material Request Creation Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_customer_group_list(name=None):
    try:
        filters = {"is_group":0}
        fields = ['name']

        if name:
            filters['name'] = name

        group_list = frappe.db.get_list("Customer Group", filters=filters, fields=fields)


        if group_list:
            return response("Customer Group List", group_list, True, 200)
        else:
            return response("No Customer Group List", [], True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)
    

@frappe.whitelist(methods="GET")
def get_customer_route_list(name=None, route_code=None, today_only=None):
    """
    Get list of Customer Routes.

    Query params:
        name: Search by route code or name
        route_code: Filter by exact route code
        today_only: If "1", return only routes scheduled for today
    """
    try:
        filters = {}
        or_filters = None

        if route_code:
            filters["route_code"] = route_code

        if name:
            or_filters = [
                ["route_code", "like", f"%{name}%"],
                ["route_name", "like", f"%{name}%"],
            ]

        routes = frappe.get_all(
            "Customer Route",
            filters=filters,
            or_filters=or_filters,
            fields=[
                "name", "route_code", "route_name",
                "monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday"
            ],
            order_by="route_code asc",
            limit_page_length=0
        )

        # Filter by today's day if requested
        if today_only == "1":
            import datetime
            day_name = datetime.datetime.now().strftime("%A").lower()
            routes = [r for r in routes if r.get(day_name)]

        # Add customer count for each route
        for r in routes:
            r["customer_count"] = frappe.db.count("Customer", {"custom_route": r["name"]}) or 0

        return response("Customer Routes fetched", {"routes": routes, "total_count": len(routes)}, True, 200)
    except Exception as e:
        frappe.log_error(title="Get Customer Route List Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_material_request_list(name=None):
    try:
        filters = {}
        fields = ['name', 'material_request_type', 'schedule_date', 'transaction_date', 'set_warehouse', 'set_from_warehouse', 'status', 'docstatus', 'is_return']

        if name:
            filters['name'] = name


        request_list = frappe.db.get_list("Material Request", filters=filters, fields=fields)

        if request_list:
            return response("Request List", request_list, True, 200)
        else:
            return response("No Request List", [], True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_material_request_detail(name=None):
    """Get full detail of a single Material Request including items."""
    try:
        if not name:
            return response("Material Request name is required", {}, False, 400)

        doc = frappe.get_doc("Material Request", name)

        items = []
        for item in doc.items:
            items.append({
                "item_code": item.item_code,
                "item_name": item.item_name,
                "qty": item.qty,
                "uom": item.uom,
                "warehouse": item.warehouse,
                "from_warehouse": getattr(item, "from_warehouse", None),
                "cost_center": getattr(item, "cost_center", None),
            })

        data = {
            "name": doc.name,
            "material_request_type": doc.material_request_type,
            "schedule_date": str(doc.schedule_date) if doc.schedule_date else None,
            "transaction_date": str(doc.transaction_date) if doc.transaction_date else None,
            "is_return": getattr(doc, "is_return", 0),
            "set_warehouse": doc.set_warehouse,
            "set_from_warehouse": getattr(doc, "set_from_warehouse", None),
            "status": doc.status,
            "docstatus": doc.docstatus,
            "company": doc.company,
            "items": items,
        }

        return response("Material Request Detail", data, True, 200)

    except frappe.DoesNotExistError:
        return response(f"Material Request '{name}' not found", {}, False, 404)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Material Request Detail Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="POST")
def update_material_request(params):
    """Update a draft Material Request."""
    try:
        if isinstance(params, str):
            params = frappe.parse_json(params)

        name = params.get("name")
        if not name:
            return response("Material Request name is required", {}, False, 400)

        doc = frappe.get_doc("Material Request", name)

        if doc.docstatus != 0:
            return response(f"Only Draft requests can be updated. '{name}' is {doc.status}", {}, False, 400)

        if params.get("schedule_date"):
            doc.schedule_date = params["schedule_date"]

        items = params.get("items")
        if items and isinstance(items, list):
            doc.items = []
            for item in items:
                doc.append("items", {
                    "item_code": item.get("item_code"),
                    "qty": item.get("qty"),
                    "uom": item.get("uom"),
                    "warehouse": item.get("warehouse") or doc.set_warehouse,
                    "from_warehouse": item.get("from_warehouse") or getattr(doc, "set_from_warehouse", None),
                })

        doc.flags.ignore_permissions = True
        doc.save()

        return response(f"Material Request '{name}' updated successfully", {
            "name": name,
            "status": doc.status,
        }, True, 200)

    except frappe.DoesNotExistError:
        frappe.db.rollback()
        return response(f"Material Request not found", {}, False, 404)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Material Request Update Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="POST")
def submit_material_request(params):
    """Submit a draft Material Request."""
    try:
        if isinstance(params, str):
            params = frappe.parse_json(params)

        name = params.get("name")
        if not name:
            return response("Material Request name is required", {}, False, 400)

        doc = frappe.get_doc("Material Request", name)

        if doc.docstatus == 1:
            return response(f"Material Request '{name}' is already submitted", {}, False, 400)

        doc.flags.ignore_permissions = True
        doc.submit()

        return response(f"Material Request '{name}' submitted successfully", {
            "name": name,
            "status": doc.status,
        }, True, 200)

    except frappe.DoesNotExistError:
        frappe.db.rollback()
        return response(f"Material Request not found", {}, False, 404)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Material Request Submit Error")
        error_msg = str(e).replace('<', '').replace('>', '')
        return response(error_msg, {}, False, 417)


@frappe.whitelist(methods="POST")
def delete_material_request(params):
    """Delete a draft Material Request."""
    try:
        if isinstance(params, str):
            params = frappe.parse_json(params)

        name = params.get("name")
        if not name:
            return response("Material Request name is required", {}, False, 400)

        doc = frappe.get_doc("Material Request", name)

        if doc.docstatus != 0:
            return response(f"Only Draft requests can be deleted. '{name}' is {doc.status}", {}, False, 400)

        frappe.delete_doc("Material Request", name, force=True, ignore_permissions=True)
        frappe.db.commit()

        return response(f"Material Request '{name}' deleted successfully", {
            "name": name,
        }, True, 200)

    except frappe.DoesNotExistError:
        return response(f"Material Request not found", {}, False, 404)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Material Request Delete Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_customer_group_list(name=None):
    try:
        filters = {}
        fields = ['name']

        if name:
            filters['name'] = name


        group_list = frappe.db.get_list("Customer Group", filters=filters, fields=fields)

        if group_list:
            return response("Customer Group", group_list, True, 200)
        else:
            return response("No Customer Group", [], True, 200)
    except Exception as exception:
        frappe.log_error(frappe.get_traceback())
        return response(str(exception), {}, False, 417)


@frappe.whitelist(methods="POST")
def set_customer_price_list(params):
    """
    Set default price list for a customer
    """
    try:
        if isinstance(params, str):
            params = json.loads(params)
        
        customer = params.get("customer")
        price_list = params.get("price_list")
        
        if not customer or not price_list:
            return response("Customer and Price List are required", {}, False, 400)
        
        # Validate that the price list exists
        if not frappe.db.exists("Price List", price_list):
            return response(f"Price List '{price_list}' does not exist", {}, False, 404)
        
        # Update customer with price list
        frappe.db.set_value("Customer", customer, "default_price_list", price_list)
        frappe.db.commit()
        
        return response("Customer price list updated successfully", {
            "customer": customer,
            "price_list": price_list
        }, True, 200)
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Set Customer Price List Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_customer_price_list(customer=None):
    """
    Get the effective price list for a customer
    """
    try:
        if not customer:
            return response("Customer is required", {}, False, 400)
        
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)
        
        settings = get_basket4me_settings()
        is_customer_based = settings.get("enable_customer_based_price_list", 0)
        
        customer_price_list = frappe.db.get_value("Customer", customer, "default_price_list")
        
        return response("Customer Price List Configuration", {
            "customer": customer,
            "effective_price_list": effective_price_list,
            "customer_default_price_list": customer_price_list,
            "is_customer_based_pricing_enabled": is_customer_based,
            "fallback_sales_person_price_list": None if not sales_person else next(
                (detail.price_list for detail in settings.sales_person_details 
                 if detail.sales_person == sales_person), None
            )
        }, True, 200)
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Get Customer Price List Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_price_list():
    """
    Get all available price lists for configuration
    """
    try:
        price_lists = frappe.get_all("Price List", 
                                   filters={"enabled": 1, "selling": 1}, 
                                   fields=["name", "price_list_name", "currency"])
        
        if price_lists:
            return response("Price List", price_lists, True, 200)
        else:
            return response("No Price Lists found", [], True, 200)
            
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Get Price List Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def get_price_list_details(name=None, page_number=1, page_size=20):
    """
    Get price lists with associated customers, item count, and company info.

    Query params:
        name: Filter/search by price list name
        page_number: Page number (default: 1)
        page_size: Records per page (default: 20)
    """
    try:
        _page_size = int(page_size or 20)
        _offset = (int(page_number or 1) - 1) * _page_size

        filters = {"enabled": 1, "selling": 1}
        or_filters = None
        if name:
            or_filters = [
                ["name", "like", f"%{name}%"],
                ["price_list_name", "like", f"%{name}%"],
            ]

        price_lists = frappe.get_all(
            "Price List",
            filters=filters,
            or_filters=or_filters,
            fields=["name", "price_list_name", "currency"],
            order_by="name asc",
            limit_start=_offset,
            limit_page_length=_page_size
        )

        total_count = frappe.db.count("Price List", filters=filters)

        for pl in price_lists:
            # Customers using this price list
            pl["customers"] = frappe.get_all(
                "Customer",
                filters={"default_price_list": pl["name"]},
                fields=["name", "customer_name", "mobile_no", "territory"],
                limit_page_length=0
            )
            pl["customer_count"] = len(pl["customers"])

            # Item count in this price list
            pl["item_count"] = frappe.db.count("Item Price", {
                "price_list": pl["name"],
                "selling": 1
            })

            # Companies using this as default (from Selling Settings or Sales Person Details)
            pl["companies"] = frappe.db.sql("""
                SELECT DISTINCT spd.company
                FROM `tabSales Person Details` spd
                WHERE spd.price_list = %s AND spd.company IS NOT NULL AND spd.company != ''
            """, pl["name"], as_list=True)
            pl["companies"] = [c[0] for c in pl["companies"]] if pl["companies"] else []

        return response("Price List details fetched", {
            "price_lists": price_lists,
            "total_count": total_count,
            "page_number": int(page_number or 1),
            "page_size": _page_size,
        }, True, 200)
    except Exception as e:
        frappe.log_error(title="Get Price List Details Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_price_list_items(price_list=None, item_code=None, item_name=None, name=None, search=None, customer=None, page_number=1, page_size=50):
    """
    Get items and their prices for a specific price list with enhanced details.

    Query params:
        price_list: Price List name (required)
        name / item_code: Filter by item code
        item_name: Filter by item name
        search: Search across item_code and item_name
        customer: Customer name (for last customer rate)
        page_number / page_size: Pagination
    """
    try:
        if not price_list:
            return response("price_list is required", {}, False, 400)

        _page_size = int(page_size or 50)
        _offset = (int(page_number or 1) - 1) * _page_size

        filters = {"price_list": price_list, "selling": 1}
        or_filters = None

        # Exact/partial filter by item_code
        code_filter = name or item_code
        if code_filter:
            filters["item_code"] = ["like", f"%{code_filter}%"]

        # Partial filter by item_name
        if item_name:
            filters["item_name"] = ["like", f"%{item_name}%"]

        # Search across both item_code and item_name
        if search:
            or_filters = [
                ["item_code", "like", f"%{search}%"],
                ["item_name", "like", f"%{search}%"],
            ]

        # Build SQL to get only the latest Item Price per item_code+uom
        conditions = ["ip.price_list = %s", "ip.selling = 1"]
        values = [price_list]

        code_filter = name or item_code
        if code_filter:
            conditions.append("ip.item_code LIKE %s")
            values.append(f"%{code_filter}%")

        if item_name:
            conditions.append("ip.item_name LIKE %s")
            values.append(f"%{item_name}%")

        if search:
            conditions.append("(ip.item_code LIKE %s OR ip.item_name LIKE %s)")
            values.extend([f"%{search}%", f"%{search}%"])

        # Exclude disabled items
        conditions.append("NOT EXISTS (SELECT 1 FROM `tabItem` it WHERE it.name = ip.item_code AND it.disabled = 1)")

        where_clause = " AND ".join(conditions)

        # Get latest Item Price per item_code+uom (most recent valid_from)
        count_sql = f"""
            SELECT COUNT(*) FROM (
                SELECT ip.item_code, ip.uom
                FROM `tabItem Price` ip
                WHERE {where_clause}
                GROUP BY ip.item_code, ip.uom
            ) as grouped
        """
        total_count = frappe.db.sql(count_sql, values)[0][0]

        items_sql = f"""
            SELECT ip.name, ip.item_code, ip.item_name, ip.uom, ip.price_list_rate,
                   ip.currency, ip.valid_from, ip.valid_upto, ip.batch_no
            FROM `tabItem Price` ip
            INNER JOIN (
                SELECT item_code, uom, MAX(valid_from) as max_valid_from
                FROM `tabItem Price`
                WHERE price_list = %s AND selling = 1
                GROUP BY item_code, uom
            ) latest ON ip.item_code = latest.item_code
                AND COALESCE(ip.uom, '') = COALESCE(latest.uom, '')
                AND ip.valid_from = latest.max_valid_from
                AND ip.price_list = %s AND ip.selling = 1
            WHERE {where_clause}
            ORDER BY ip.item_code ASC
            LIMIT %s OFFSET %s
        """
        items = frappe.db.sql(items_sql, [price_list, price_list] + values + [_page_size, _offset], as_dict=True)

        # Enrich each item with enhanced details
        for item in items:
            ic = item["item_code"]

            # Default UOM (stock_uom)
            stock_uom = frappe.db.get_value("Item", ic, "stock_uom") or "Nos"
            item["default_uom"] = stock_uom

            # All UOMs with conversion factor and rate for selected price list
            uom_details = frappe.db.sql("""
                SELECT uom, conversion_factor
                FROM `tabUOM Conversion Detail`
                WHERE parent = %s AND parenttype = 'Item'
                ORDER BY idx ASC
            """, ic, as_dict=True)

            if not uom_details:
                uom_details = [{"uom": stock_uom, "conversion_factor": 1.0}]

            uoms = []
            for ud in uom_details:
                uom_name = ud.get("uom")
                cf = ud.get("conversion_factor", 1.0)
                # Rate for this UOM in the selected price list
                rate = frappe.db.sql("""
                    SELECT price_list_rate FROM `tabItem Price`
                    WHERE selling = 1 AND item_code = %s AND price_list = %s
                    AND (uom = %s OR uom IS NULL OR uom = '')
                    ORDER BY CASE WHEN uom = %s THEN 0 ELSE 1 END, creation DESC
                    LIMIT 1
                """, (ic, price_list, uom_name, uom_name), as_dict=True)
                uom_rate = rate[0]["price_list_rate"] if rate else 0.0
                uoms.append({
                    "uom": uom_name,
                    "conversion_factor": cf,
                    "rate": uom_rate
                })
            item["uoms"] = uoms

            # MRP - from "MRP" price list based on default UOM
            mrp_result = frappe.db.sql("""
                SELECT price_list_rate FROM `tabItem Price`
                WHERE selling = 1 AND item_code = %s AND price_list = 'MRP'
                AND (uom = %s OR uom IS NULL OR uom = '')
                ORDER BY creation DESC LIMIT 1
            """, (ic, stock_uom), as_dict=True)
            item["mrp"] = mrp_result[0]["price_list_rate"] if mrp_result else 0.0

            # Standard Selling Price based on default UOM
            std_result = frappe.db.sql("""
                SELECT price_list_rate FROM `tabItem Price`
                WHERE selling = 1 AND item_code = %s AND price_list = 'Standard Selling'
                AND (uom = %s OR uom IS NULL OR uom = '')
                ORDER BY creation DESC LIMIT 1
            """, (ic, stock_uom), as_dict=True)
            item["standard_selling_price"] = std_result[0]["price_list_rate"] if std_result else 0.0

            # Last Customer Rate from SO and SI (whichever is most recent)
            item["last_customer_rate"] = 0.0
            if customer:
                last_rate = frappe.db.sql("""
                    SELECT rate, txn_date FROM (
                        SELECT soi.rate, so2.transaction_date as txn_date, so2.creation
                        FROM `tabSales Order Item` soi
                        JOIN `tabSales Order` so2 ON so2.name = soi.parent
                        WHERE so2.customer = %s AND soi.item_code = %s
                        AND so2.docstatus != 2
                        UNION ALL
                        SELECT sii.rate, si.posting_date as txn_date, si.creation
                        FROM `tabSales Invoice Item` sii
                        JOIN `tabSales Invoice` si ON si.name = sii.parent
                        WHERE si.customer = %s AND sii.item_code = %s
                        AND si.docstatus = 1 AND si.is_return = 0
                    ) combined
                    ORDER BY txn_date DESC, creation DESC LIMIT 1
                """, (customer, ic, customer, ic), as_dict=True)
                if last_rate:
                    item["last_customer_rate"] = last_rate[0].get("rate") or 0.0

            # Available stock qty from salesperson warehouse or total
            item["available_qty"] = 0.0
            try:
                sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
                settings = get_basket4me_settings()
                warehouse = None
                if sales_person and settings:
                    for detail in settings.sales_person_details:
                        if detail.sales_person == sales_person:
                            warehouse = detail.warehouse
                            break

                if warehouse:
                    item["available_qty"] = flt(frappe.db.get_value("Bin",
                        {"item_code": ic, "warehouse": warehouse}, "actual_qty") or 0)
                else:
                    # Total stock across all warehouses
                    total_qty = frappe.db.sql("""
                        SELECT COALESCE(SUM(actual_qty), 0) as qty
                        FROM `tabBin` WHERE item_code = %s
                    """, ic)
                    item["available_qty"] = flt(total_qty[0][0]) if total_qty else 0.0
            except Exception:
                item["available_qty"] = 0.0

        return response("Price List items fetched", {
            "items": items,
            "price_list": price_list,
            "total_count": total_count,
            "page_number": int(page_number or 1),
            "page_size": _page_size,
        }, True, 200)
    except Exception as e:
        frappe.log_error(title="Get Price List Items Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def toggle_sales_team_override(params):
    """
    Enable or disable sales team override
    """
    try:
        if isinstance(params, str):
            params = json.loads(params)
        
        enable_override = params.get("enable_override", False)
        
        # Update Basket4Me Settings
        settings = get_basket4me_settings()
        settings.override_sales_team_in_customer = 1 if enable_override else 0
        settings.save()
        
        return response("Sales Team Override Updated", {
            "override_enabled": bool(enable_override),
            "message": "All customers are now visible to all salespersons" if enable_override else "Customer visibility is now restricted to sales team assignments"
        }, True, 200)
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Toggle Sales Team Override Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist()
def get_sales_team_override_status(enable_override=None):
    """
    Get current status of sales team override (GET) or toggle it (POST)
    Args:
        enable_override: Boolean to enable/disable override (only for POST)
    """
    try:
        # If enable_override parameter is provided, toggle the setting
        if enable_override is not None:
            # Handle toggle functionality
            if isinstance(enable_override, str):
                enable_override = enable_override.lower() in ['true', '1', 'yes']
            
            # Update Basket4Me Settings
            settings = get_basket4me_settings()
            settings.override_sales_team_in_customer = 1 if enable_override else 0
            settings.save()
            
            return response("Sales Team Override Updated", {
                "override_enabled": bool(enable_override),
                "message": "All customers are now visible to all salespersons" if enable_override else "Customer visibility is now restricted to sales team assignments"
            }, True, 200)
        
        # Default behavior: return current status
        override_enabled = should_override_sales_team()
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        
        # Get customer counts for comparison
        total_customers = frappe.db.count("Customer")
        
        if not override_enabled and sales_person:
            assigned_customers = frappe.db.sql("""
                SELECT COUNT(DISTINCT parent) 
                FROM `tabSales Team` 
                WHERE sales_person = %s AND parenttype = 'Customer'
            """, (sales_person,))[0][0] or 0
        else:
            assigned_customers = total_customers
        
        return response("Sales Team Override Status", {
            "override_enabled": override_enabled,
            "current_sales_person": sales_person,
            "total_customers": total_customers,
            "accessible_customers": assigned_customers,
            "description": "All customers visible" if override_enabled else "Only assigned customers visible"
        }, True, 200)
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Sales Team Override API Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def test_customer_pricing(customer=None, item_code=None):
    """
    Test API to show which price list and price is used for a specific customer and item
    """
    try:
        if not customer:
            return response("Customer parameter is required", {}, False, 400)
        
        if not item_code:
            return response("Item code parameter is required", {}, False, 400)
        
        # Get current sales person
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        
        # Get effective price list
        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)
        
        # Get customer's default price list (if any)
        customer_default_price_list = frappe.db.get_value("Customer", customer, "default_price_list")
        
        # Get sales person's price list from Basket4Me Settings
        settings = get_basket4me_settings()
        sales_person_price_list = None
        for detail in settings.sales_person_details:
            if detail.sales_person == sales_person:
                sales_person_price_list = detail.price_list
                break
        
        # Check if customer-based pricing is enabled
        customer_based_enabled = settings.get("enable_customer_based_price_list", 0)
        
        # Get item price from effective price list
        item_price = frappe.db.sql("""
            SELECT price_list_rate, uom, price_list
            FROM `tabItem Price`
            WHERE item_code = %s
            AND price_list = %s
            AND selling = 1
            ORDER BY creation DESC
            LIMIT 1
        """, (item_code, effective_price_list), as_dict=True)
        
        # Get all available prices for this item to show comparison
        all_prices = frappe.db.sql("""
            SELECT price_list, price_list_rate, uom
            FROM `tabItem Price`
            WHERE item_code = %s
            AND selling = 1
            ORDER BY price_list, uom
        """, (item_code,), as_dict=True)
        
        result = {
            "customer": customer,
            "item_code": item_code,
            "current_sales_person": sales_person,
            "customer_based_pricing_enabled": bool(customer_based_enabled),
            "customer_default_price_list": customer_default_price_list or "Not Set",
            "sales_person_price_list": sales_person_price_list or "Not Found",
            "effective_price_list_used": effective_price_list,
            "price_selection_logic": get_price_selection_explanation(customer, sales_person, customer_default_price_list, customer_based_enabled),
            "selected_price": item_price[0] if item_price else {"price_list_rate": 0, "message": "No price found"},
            "all_available_prices": all_prices
        }
        
        return response("Customer Pricing Test Result", result, True, 200)
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Test Customer Pricing Error")
        return response(str(e), {}, False, 417)

def get_price_selection_explanation(customer, sales_person, customer_default_price_list, customer_based_enabled):
    """Helper function to explain which price list selection logic was used"""
    
    if customer_based_enabled and customer_default_price_list:
        return f"Customer-based pricing is ENABLED and customer has default price list '{customer_default_price_list}' → Using customer's price list"
    
    elif customer_based_enabled and not customer_default_price_list:
        return f"Customer-based pricing is ENABLED but customer has no default price list → Falling back to sales person's price list"
    
    elif not customer_based_enabled:
        return f"Customer-based pricing is DISABLED → Using sales person's price list from Basket4Me Settings"
    
    else:
        return "Using final fallback to standard selling price list"
    


@frappe.whitelist()
def check_basket4me_settings_status():
    """Check the actual status of Basket4Me Settings doctype and data"""
    try:
        results = {}
        
        # 1. Check if doctype exists
        try:
            doctype_exists = frappe.db.exists("DocType", "Basket4Me Settings")
            results["doctype_exists"] = bool(doctype_exists)
        except Exception as e:
            results["doctype_check_error"] = str(e)
        
        # 2. Check table structure
        try:
            table_info = frappe.db.sql("""
                SHOW TABLES LIKE 'tabBasket4Me Settings'
            """)
            results["table_exists"] = bool(table_info)
        except Exception as e:
            results["table_check_error"] = str(e)
        
        # 3. Try to get settings using frappe.get_doc
        try:
            settings = get_basket4me_settings()
            results["get_doc_success"] = True
            results["settings_data"] = {
                "name": settings.name,
                "enable_customer_based_price_list": getattr(settings, 'enable_customer_based_price_list', 'FIELD_NOT_FOUND'),
                "company": getattr(settings, 'company', 'FIELD_NOT_FOUND')
            }
        except Exception as e:
            results["get_doc_error"] = str(e)
        
        # 4. Try direct SQL query
        try:
            sql_result = frappe.db.sql("""
                SELECT * FROM `tabBasket4Me Settings` LIMIT 1
            """, as_dict=True)
            results["sql_query_success"] = True
            results["sql_data"] = sql_result[0] if sql_result else "NO_RECORDS"
        except Exception as e:
            results["sql_query_error"] = str(e)
        
        # 5. Check if any records exist
        try:
            count = frappe.db.count("Basket4Me Settings")
            results["record_count"] = count
        except Exception as e:
            results["count_error"] = str(e)
        
        # 6. Try to create a new record
        try:
            if results.get("record_count", 0) == 0:
                new_doc = frappe.new_doc("Basket4Me Settings")
                new_doc.enable_customer_based_price_list = 1
                new_doc.insert(ignore_permissions=True)
                frappe.db.commit()
                results["record_created"] = True
            else:
                results["record_creation_skipped"] = "Records already exist"
        except Exception as e:
            results["creation_error"] = str(e)
        
        return response("Basket4Me Settings status check completed", results, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error in Basket4Me Settings check: {str(e)}\n{frappe.get_traceback()}", "Settings Check Error")
        return response(f"Check error: {str(e)}", {}, False, 417)


@frappe.whitelist(methods=['GET'])
def test_customer_price_assignment():
    """Test function to verify customer price assignment logic"""
    try:
        results = {}
        
        # Get all customers with default_price_list
        customers_with_price_lists = frappe.db.sql("""
            SELECT name, customer_name, default_price_list, creation
            FROM `tabCustomer`
            WHERE default_price_list IS NOT NULL 
            AND default_price_list != ''
            ORDER BY creation DESC
            LIMIT 10
        """, as_dict=True)
        
        results['customers_with_price_lists'] = customers_with_price_lists
        results['total_customers_with_price_lists'] = len(customers_with_price_lists)
        
        # Test get_effective_price_list with these customers
        test_results = []
        for customer in customers_with_price_lists:
            test_result = {
                'customer_name': customer.name,
                'customer_display_name': customer.customer_name,
                'expected_price_list': customer.default_price_list,
                'actual_price_list': get_effective_price_list(customer=customer.name),
                'match': False
            }
            test_result['match'] = (test_result['expected_price_list'] == test_result['actual_price_list'])
            test_results.append(test_result)
        
        results['test_results'] = test_results
        results['successful_matches'] = len([r for r in test_results if r['match']])
        results['failed_matches'] = len([r for r in test_results if not r['match']])
        
        return response("Customer price assignment test completed", results, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error in customer price test: {str(e)}\n{frappe.get_traceback()}", "Customer Price Test Error")
        return response(f"Test error: {str(e)}", {}, False, 417)


@frappe.whitelist(methods=['GET'])
def verify_customer_pricing():
    """Verify customer pricing with comprehensive debugging"""
    try:
        # Get a few test customers
        test_customers = frappe.db.sql("""
            SELECT name, customer_name, default_price_list
            FROM `tabCustomer`
            WHERE default_price_list IS NOT NULL 
            AND default_price_list != ''
            LIMIT 5
        """, as_dict=True)
        
        results = {
            'test_customers': test_customers,
            'pricing_tests': []
        }
        
        for customer in test_customers:
            # Test with exact name
            price_list_result = get_effective_price_list(customer=customer.name)
            
            # Test lookup details
            lookup_debug = {
                'customer_input': customer.name,
                'customer_display_name': customer.customer_name,
                'expected_price_list': customer.default_price_list,
                'actual_price_list': price_list_result,
                'match': customer.default_price_list == price_list_result
            }
            
            # Additional database checks
            lookup_debug['db_checks'] = {}
            
            # Direct name lookup
            direct_lookup = frappe.db.get_value("Customer", customer.name, "default_price_list")
            lookup_debug['db_checks']['direct_name'] = direct_lookup
            
            # Customer name field lookup
            name_field_lookup = frappe.db.get_value("Customer", {"customer_name": customer.customer_name}, "default_price_list")
            lookup_debug['db_checks']['customer_name_field'] = name_field_lookup
            
            # Fuzzy SQL lookup
            fuzzy_lookup = frappe.db.sql("""
                SELECT name, customer_name, default_price_list 
                FROM `tabCustomer` 
                WHERE TRIM(LOWER(name)) = TRIM(LOWER(%s))
                   OR TRIM(LOWER(customer_name)) = TRIM(LOWER(%s))
                LIMIT 1
            """, (customer.name, customer.name), as_dict=True)
            lookup_debug['db_checks']['fuzzy_lookup'] = fuzzy_lookup
            
            results['pricing_tests'].append(lookup_debug)
        
        return response("Customer pricing verification completed", results, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error in customer pricing verification: {str(e)}\n{frappe.get_traceback()}", "Customer Pricing Verification Error")
        return response(f"Verification error: {str(e)}", {}, False, 417)


# Add this as a new diagnostic function to isolate the issue
@frappe.whitelist(methods="GET")
def get_item_list_diagnostic(name=None, item_name=None, customer=None, limit_start=0, limit_page_length=5):
    """
    Diagnostic version of get_item_list to identify exact failure point
    """
    try:
        frappe.log_error("=== DIAGNOSTIC START ===", "Item List Diagnostic")
        
        # Step 1: Basic item query
        filters = {"custom_allow_mobile_app": 1}
        fields = ['name', 'item_name', "description"]

        if name:
            filters['name'] = ["like", f"%{name}%"]
        elif item_name:
            filters['item_name'] = ["like", f"%{item_name}%"]

        frappe.log_error(f"Step 1: Getting items with filters: {filters}", "Item List Diagnostic")
        item_list = frappe.db.get_list("Item", filters=filters, fields=fields, 
                                     limit_start=int(limit_start),
                                     limit_page_length=int(limit_page_length))
        frappe.log_error(f"Step 1 SUCCESS: Found {len(item_list)} items", "Item List Diagnostic")

        # Step 2: Get sales person
        frappe.log_error("Step 2: Getting sales person", "Item List Diagnostic")
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        frappe.log_error(f"Step 2 SUCCESS: Sales person = {sales_person}", "Item List Diagnostic")

        # Step 3: Get effective price list
        frappe.log_error("Step 3: Getting effective price list", "Item List Diagnostic")
        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)
        frappe.log_error(f"Step 3 SUCCESS: Effective price list = {effective_price_list}", "Item List Diagnostic")

        # Step 4: Basic settings
        frappe.log_error("Step 4: Getting basic settings", "Item List Diagnostic")
        try:
            include_tax = frappe.db.get_value("Basket4Me Settings", None, "is_this_tax_included_in_basic_rate") or 0
            frappe.log_error(f"Step 4a SUCCESS: include_tax = {include_tax}", "Item List Diagnostic")
        except Exception as e:
            frappe.log_error(f"Step 4a FAILED: {str(e)}", "Item List Diagnostic")
            include_tax = 0

        try:
            settings = get_basket4me_settings()
            frappe.log_error("Step 4b SUCCESS: Got Basket4Me Settings", "Item List Diagnostic")
        except Exception as e:
            frappe.log_error(f"Step 4b FAILED: {str(e)}", "Item List Diagnostic")
            settings = None

        # Step 5: Sales person details
        frappe.log_error("Step 5: Getting sales person details", "Item List Diagnostic")
        sales_person_details = None
        if settings and sales_person:
            try:
                for detail in settings.sales_person_details:
                    if detail.sales_person == sales_person:
                        sales_person_details = detail
                        break
                frappe.log_error(f"Step 5 SUCCESS: Sales person details found = {sales_person_details is not None}", "Item List Diagnostic")
            except Exception as e:
                frappe.log_error(f"Step 5 FAILED: {str(e)}", "Item List Diagnostic")

        # Step 6: Process ONLY the first item to isolate issues
        frappe.log_error("Step 6: Processing first item only", "Item List Diagnostic")
        if item_list:
            item = item_list[0]  # Only process first item
            frappe.log_error(f"Step 6a: Processing item {item['name']}", "Item List Diagnostic")
            
            # Strip HTML tags from description
            if 'description' in item and item['description']:
                original_desc = item['description']
                item['description'] = strip_html_tags(item['description'])
                frappe.log_error(f"Step 6a-HTML: Cleaned description from '{original_desc}' to '{item['description']}'", "Item List Diagnostic")
            
            # 6b: Stock check
            try:
                available_qty = 0
                if sales_person_details and hasattr(sales_person_details, 'warehouse') and sales_person_details.warehouse:
                    available_qty = frappe.db.get_value("Bin", 
                        {"item_code": item["name"], "warehouse": sales_person_details.warehouse}, 
                        "actual_qty") or 0
                frappe.log_error(f"Step 6b SUCCESS: available_qty = {available_qty}", "Item List Diagnostic")
                item["available_qty"] = available_qty
            except Exception as e:
                frappe.log_error(f"Step 6b FAILED: {str(e)}", "Item List Diagnostic")
                item["available_qty"] = 0

            # 6c: UOM details
            try:
                uom_details = frappe.get_all(
                    "UOM Conversion Detail",
                    filters={"parent": item["name"]},
                    fields=["uom"]
                )
                frappe.log_error(f"Step 6c SUCCESS: Found {len(uom_details)} UOMs", "Item List Diagnostic")
            except Exception as e:
                frappe.log_error(f"Step 6c FAILED: {str(e)}", "Item List Diagnostic")
                uom_details = []

            # 6d: Price lookup for first UOM only
            uoms_with_prices = []
            if uom_details:
                try:
                    uom = uom_details[0]["uom"]
                    frappe.log_error(f"Step 6d: Getting price for UOM {uom}", "Item List Diagnostic")
                    
                    price = frappe.db.sql(
                        """
                        SELECT price_list_rate 
                        FROM `tabItem Price`
                        WHERE selling = 1 
                        AND item_code = %(item_code)s 
                        AND uom = %(uom)s 
                        AND price_list = %(price_list)s
                        ORDER BY creation DESC
                        LIMIT 1
                        """,
                        {"item_code": item["name"], "uom": uom, "price_list": effective_price_list},
                        as_dict=True
                    )
                    
                    price_value = price[0]["price_list_rate"] if price else 0.0
                    uoms_with_prices.append({"uom": uom, "price": price_value})
                    frappe.log_error(f"Step 6d SUCCESS: Price = {price_value}", "Item List Diagnostic")
                    
                except Exception as e:
                    frappe.log_error(f"Step 6d FAILED: {str(e)}", "Item List Diagnostic")
                    uoms_with_prices.append({"uom": "error", "price": 0.0})

            # 6e: Tax rate
            try:
                tax_rate = get_item_tax_rate(item["name"]) or 0.0
                frappe.log_error(f"Step 6e SUCCESS: Tax rate = {tax_rate}", "Item List Diagnostic")
            except Exception as e:
                frappe.log_error(f"Step 6e FAILED: {str(e)}", "Item List Diagnostic")
                tax_rate = 0.0

            # 6f: Set all properties
            item["uoms"] = uoms_with_prices
            item["tax_rate"] = tax_rate
            item["is_tax_included"] = include_tax
            item["effective_price_list"] = effective_price_list
            
            frappe.log_error("Step 6f SUCCESS: All properties set", "Item List Diagnostic")

        frappe.log_error("=== DIAGNOSTIC SUCCESS ===", "Item List Diagnostic")
        
        return response("Diagnostic Item List", item_list, True, 200)

    except Exception as exception:
        frappe.log_error(f"=== DIAGNOSTIC FAILED === {str(exception)}\n{frappe.get_traceback()}", "Item List Diagnostic Error")
        return response(str(exception), {}, False, 417)


# Also add this simpler test function
@frappe.whitelist(methods="GET")
def test_simple_item_list(customer=None):
    """
    Super simple test - just return items with effective price list
    """
    try:
        # Get a few items
        items = frappe.db.get_list("Item", 
                                 filters={"custom_allow_mobile_app": 1}, 
                                 fields=['name', 'item_name'], 
                                 limit=3)
        
        # Get sales person and price list
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)
        
        result = {
            "customer": customer,
            "sales_person": sales_person, 
            "effective_price_list": effective_price_list,
            "item_count": len(items),
            "items": items
        }
        
        return response("Simple Item Test", result, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Simple test failed: {str(e)}\n{frappe.get_traceback()}", "Simple Test Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods="GET")
def test_letterhead():
    """Test endpoint to check if Default_Letterhead exists and has content"""
    try:
        letterhead_data = {}
        
        # Check if Default_Letterhead exists
        exists = frappe.db.exists("Letter Head", "Default_Letterhead")
        letterhead_data["exists"] = bool(exists)
        
        if exists:
            try:
                letterhead = frappe.get_doc("Letter Head", "Default_Letterhead")
                letterhead_data.update({
                    "name": letterhead.name,
                    "header_html": letterhead.header or "",
                    "footer_html": letterhead.footer or "",
                    "disabled": letterhead.disabled if hasattr(letterhead, 'disabled') else False,
                    "is_default": letterhead.is_default if hasattr(letterhead, 'is_default') else False
                })
                
                # Check if header/footer have actual content
                letterhead_data["header_has_content"] = bool(letterhead.header and letterhead.header.strip())
                letterhead_data["footer_has_content"] = bool(letterhead.footer and letterhead.footer.strip())
                
            except Exception as e:
                letterhead_data["fetch_error"] = str(e)
        else:
            # List all available letterheads
            all_letterheads = frappe.db.get_list("Letter Head", fields=["name", "disabled"], limit=10)
            letterhead_data["available_letterheads"] = all_letterheads
            
        return response("Letterhead Test Results", letterhead_data, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Error testing letterhead: {str(e)}\n{frappe.get_traceback()}", "Letterhead Test Error")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods=["GET", "POST"])
def get_pdf_download_link(doctype, name):
    """
    Simple API to get direct PDF download link with default format and letterhead
    
    Args:
        doctype: Document type (e.g., "Sales Invoice")
        name: Document name (e.g., "MNT-2025-0005")
    
    Returns:
        Direct PDF download URL using Frappe's native PDF endpoint
    """
    try:
        # Check permissions - respect override setting
        override_enabled = should_override_sales_team()
        has_permission = False
        
        if override_enabled and doctype == "Sales Invoice":
            # If override is enabled for Sales Invoice, allow access to all invoices
            has_permission = True
        else:
            # Use standard Frappe permission checking
            has_permission = frappe.has_permission(doctype, "read", name)
        
        if not has_permission:
            return response("No permission to access this document", {}, False, 403)
        
        doc = frappe.get_doc(doctype, name)
        if not doc:
            return response(f"{doctype} '{name}' not found", {}, False, 404)
        
        # Get default print format
        default_print_format = frappe.db.get_value("Property Setter", 
            {"doc_type": doctype, "property": "default_print_format"}, "value")
        if not default_print_format:
            default_print_format = "Standard"
        
        # Get default letterhead
        letterhead = None
        if hasattr(doc, 'company') and doc.company:
            letterhead = frappe.db.get_value("Company", doc.company, "default_letter_head")
        if not letterhead:
            letterhead = frappe.db.get_value("Letter Head", {"is_default": 1}, "name")
        
        # Build PDF download URL
        base_url = frappe.utils.get_url()
        pdf_url = f"{base_url}/api/method/frappe.utils.print_format.download_pdf?doctype={doctype}&name={name}&format={default_print_format}"
        if letterhead:
            pdf_url += f"&letterhead={letterhead}"
        
        return response("PDF download link generated successfully", {
            "pdf_url": pdf_url,
            "doctype": doctype,
            "name": name,
            "print_format": default_print_format,
            "letterhead": letterhead
        }, True, 200)
        
    except Exception as e:
        return response(f"Error generating PDF link: {str(e)}", {}, False, 417)

# Direct print view endpoint for GET requests
@frappe.whitelist(methods=["GET", "POST"])
def direct_print_view(doctype, name, format=None, letterhead=None):
    """
    Direct endpoint that redirects to Frappe's printview with proper authentication
    Access via: GET /api/method/basket4me_pwa.api.direct_print_view?doctype=Sales Invoice&name=MNT-2025-0005
    
    Note: User must be logged into Frappe in the same browser session
    """
    try:
        # For browser access, check if user is logged in through Frappe session
        if not frappe.session.user or frappe.session.user == "Guest":
            # Return a user-friendly login message
            return f"""
            <html>
            <head><title>Authentication Required</title></head>
            <body style="font-family: Arial, sans-serif; text-align: center; margin-top: 100px;">
                <h2>🔒 Authentication Required</h2>
                <p>Please <a href="/login">login to Frappe</a> first to access this print view.</p>
                <p>After logging in, try this link again:</p>
                <p><a href="/api/method/basket4me_pwa.api.direct_print_view?doctype={doctype}&name={name}">Print {name}</a></p>
                <br>
                <a href="/login" style="background: #007bff; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Login to Frappe</a>
            </body>
            </html>
            """
        
        # Check document permissions - respect override setting
        override_enabled = should_override_sales_team()
        has_permission = False
        
        if override_enabled and doctype == "Sales Invoice":
            # If override is enabled for Sales Invoice, allow access to all invoices
            has_permission = True
        else:
            # Use standard Frappe permission checking
            has_permission = frappe.has_permission(doctype, "read", name)
        
        if not has_permission:
            return f"""
            <html>
            <head><title>Access Denied</title></head>
            <body style="font-family: Arial, sans-serif; text-align: center; margin-top: 100px;">
                <h2>🚫 Access Denied</h2>
                <p>You don't have permission to access {doctype} '{name}'</p>
                <p>Contact your administrator if you believe this is an error.</p>
                <br>
                <a href="/app" style="background: #28a745; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Go to App</a>
            </body>
            </html>
            """
        
        # Get document
        doc = frappe.get_doc(doctype, name)
        if not doc:
            frappe.local.response.http_status_code = 404
            return f"<h1>404 - {doctype} '{name}' not found</h1>"
        
        # Use provided format or get default
        if not format:
            format = frappe.db.get_value("Property Setter", 
                {"doc_type": doctype, "property": "default_print_format"}, "value") or "Standard"
        
        # Use provided letterhead or get default
        if not letterhead:
            if hasattr(doc, 'company') and doc.company:
                letterhead = frappe.db.get_value("Company", doc.company, "default_letter_head")
            if not letterhead:
                letterhead = frappe.db.get_value("Letter Head", {"is_default": 1}, "name")
        
        # Build the PDF download URL instead of printview
        base_url = frappe.utils.get_url()
        redirect_url = f"{base_url}/api/method/frappe.utils.print_format.download_pdf?doctype={doctype}&name={name}&format={format}"
        if letterhead:
            redirect_url += f"&letterhead={letterhead}"
        
        # Redirect to the PDF download
        frappe.local.response.type = "redirect"
        frappe.local.response.location = redirect_url
        
        return f'<script>window.location.href="{redirect_url}";</script>'
            
    except Exception as e:
        frappe.local.response.http_status_code = 500
        return f"<h1>500 - Error generating print view: {str(e)}</h1>"

# Simple test API to verify API is working
@frappe.whitelist(allow_guest=True)
def test_print_api():
    """Simple test API to verify our print API is accessible"""
    return {
        "message": "Print API is working!",
        "success": True,
        "timestamp": frappe.utils.now()
    }

# Default Print API - Automatic format and letterhead
@frappe.whitelist()
def print_document_default(doctype, name, output_type="pdf"):
    """
    Simple print API that automatically uses default print format and letterhead
    
    Args:
        doctype: Document type (e.g., "Sales Invoice")
        name: Document name (e.g., "MNT-2025-0006")
        output_type: "pdf", "html", or "url"
    
    Returns:
        PDF content, HTML content, or print URL based on output_type
    """
    try:
        # Validate document exists and user has permission - respect override setting
        override_enabled = should_override_sales_team()
        has_permission = False
        
        if override_enabled and doctype == "Sales Invoice":
            # If override is enabled for Sales Invoice, allow access to all invoices
            has_permission = True
        else:
            # Use standard Frappe permission checking
            has_permission = frappe.has_permission(doctype, "read", name)
        
        if not has_permission:
            return response("No permission to access this document", {}, False, 403)
        
        doc = frappe.get_doc(doctype, name)
        if not doc:
            return response(f"{doctype} '{name}' not found", {}, False, 404)
        
        # Get default print format for the doctype
        default_print_format = frappe.db.get_value("Property Setter", 
            {"doc_type": doctype, "property": "default_print_format"}, "value")
        
        if not default_print_format:
            # Fallback to "Standard" if no default is set
            default_print_format = "Standard"
        
        # Get default letterhead from company or system default
        letterhead = None
        if hasattr(doc, 'company') and doc.company:
            letterhead = frappe.db.get_value("Company", doc.company, "default_letter_head")
        
        # If no company letterhead, find the default letterhead
        if not letterhead:
            # Get the default letterhead
            default_letterhead = frappe.db.get_value("Letter Head", {"is_default": 1}, "name")
            if not default_letterhead:
                # If no default, get any available letterhead
                default_letterhead = frappe.db.get_value("Letter Head", {}, "name")
            letterhead = default_letterhead
        
        # Generate URLs based on output type
        if output_type == "url":
            base_url = frappe.utils.get_url()
            # Use Frappe's PDF download endpoint
            url = f"{base_url}/api/method/frappe.utils.print_format.download_pdf?doctype={doctype}&name={name}&format={default_print_format}"
            if letterhead:
                url += f"&letterhead={letterhead}"
            
            return response("PDF download URL generated successfully", {
                "url": url,
                "print_format": default_print_format,
                "letterhead": letterhead,
                "output_type": "pdf"
            }, True, 200)
        
        elif output_type == "html_url":
            base_url = frappe.utils.get_url()
            # Use Frappe's print view URL for HTML
            url = f"{base_url}/printview?doctype={doctype}&name={name}&format={default_print_format}"
            if letterhead:
                url += f"&letterhead={letterhead}"
            url += "&settings=%7B%7D&_lang=en"
            
            return response("HTML print URL generated successfully", {
                "url": url,
                "print_format": default_print_format,
                "letterhead": letterhead,
                "output_type": "html"
            }, True, 200)
        
        # Generate HTML content
        from frappe.www.printview import get_print_context
        
        print_context = get_print_context(
            doctype=doctype,
            name=name,
            print_format=default_print_format,
            letterhead=letterhead,
            lang="en"
        )
        
        if output_type == "html":
            return response("Print HTML generated successfully", {
                "html": print_context.get("html", ""),
                "print_format": default_print_format,
                "letterhead": letterhead
            }, True, 200)
        
        # Generate PDF
        elif output_type == "pdf":
            from frappe.utils.pdf import get_pdf
            
            html_content = print_context.get("html", "")
            if not html_content:
                return response("Failed to generate HTML content", {}, False, 500)
            
            pdf_content = get_pdf(html_content)
            
            # Return PDF as base64 encoded string
            import base64
            pdf_base64 = base64.b64encode(pdf_content).decode('utf-8')
            
            return response("PDF generated successfully", {
                "pdf_base64": pdf_base64,
                "print_format": default_print_format,
                "letterhead": letterhead,
                "filename": f"{name}.pdf"
            }, True, 200)
            
    except Exception as e:
        frappe.log_error(f"Print API Error: {str(e)}", "Print Document Default")
        return response(f"Error generating print output: {str(e)}", {}, False, 500)

# ========================================
# FRAPPE PRINT API - DYNAMIC IMPLEMENTATION
# ========================================

@frappe.whitelist(methods=["GET", "POST"])
def print_document(doctype=None, name=None, print_format=None, output_type="pdf", **kwargs):
    """
    Universal print method that works with any document type
    
    Args:
        doctype: Any Frappe doctype (Sales Invoice, Purchase Order, etc.)
        name: Document name
        print_format: Print format name (auto-detects if not provided)
        output_type: "pdf", "html", or "both"
        **kwargs: Additional print options (letterhead, language, etc.)
    """
    try:
        # Validate inputs
        if not doctype or not name:
            return response("Document type and name are required", {}, False, 400)
        
        # Validate document exists and user has permission
        if not frappe.db.exists(doctype, name):
            return response(f"Document {doctype} {name} not found", {}, False, 404)
        
        doc = frappe.get_doc(doctype, name)
        
        # Check permissions - respect override setting
        override_enabled = should_override_sales_team()
        has_permission = False
        
        if override_enabled and doctype == "Sales Invoice":
            # If override is enabled for Sales Invoice, allow access to all invoices
            has_permission = True
        else:
            # Use standard Frappe permission checking
            has_permission = frappe.has_permission(doctype, "read", doc=doc)
        
        if not has_permission:
            return response("No permission to access this document", {}, False, 403)
        
        # Auto-detect print format if not provided
        if not print_format:
            print_format = get_default_print_format(doctype)
        
        # Generate output based on type
        result = {
            "doctype": doctype, 
            "name": name, 
            "print_format": print_format,
            "generated_at": frappe.utils.now()
        }
        
        if output_type in ["html", "both"]:
            html_data = get_dynamic_html(doctype, name, print_format, **kwargs)
            result["html"] = html_data
        
        if output_type in ["pdf", "both"]:
            pdf_data = get_dynamic_pdf(doctype, name, print_format, **kwargs)
            result["pdf"] = pdf_data
        
        return response("Document printed successfully", result, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Print API Error: {str(e)}\n{frappe.get_traceback()}", "Print Document Error")
        return response(str(e), {}, False, 417)


def get_dynamic_html(doctype, name, print_format, **kwargs):
    """Generate HTML for any document dynamically"""
    try:
        # Get the document
        doc = frappe.get_doc(doctype, name)
        
        # Try the original approach with proper parameters
        html, style = get_html_and_style(
            doctype, name, print_format
        )
        
        return {
            "html": html,
            "css": style,
            "full_html": f"<style>{style}</style>{html}" if style else html,
            "size": len(html) if html else 0
        }
    except Exception as e:
        frappe.log_error(f"HTML Generation Error: {str(e)}")
        return {"error": str(e)}


def get_dynamic_pdf(doctype, name, print_format, **kwargs):
    """Generate PDF for any document dynamically"""
    try:
        html_data = get_dynamic_html(doctype, name, print_format, **kwargs)
        
        if "error" in html_data:
            return html_data
        
        # PDF options - can be customized per doctype
        pdf_options = get_pdf_options(doctype, **kwargs)
        
        pdf_binary = get_pdf(html_data["full_html"], options=pdf_options)
        
        return {
            "pdf_base64": base64.b64encode(pdf_binary).decode(),
            "filename": generate_filename(doctype, name, print_format),
            "size": len(pdf_binary),
            "content_type": "application/pdf"
        }
    except Exception as e:
        frappe.log_error(f"PDF Generation Error: {str(e)}")
        return {"error": str(e)}


def get_default_print_format(doctype):
    """Auto-detect the best print format for a doctype"""
    try:
        # Get all print formats for this doctype
        formats = frappe.get_all(
            "Print Format",
            filters={"doc_type": doctype, "disabled": 0},
            fields=["name", "standard", "default_print_format"],
            order_by="default_print_format desc, standard desc"
        )
        
        if formats:
            return formats[0].name
        return "Standard"
    except Exception:
        return "Standard"


def get_pdf_options(doctype, **kwargs):
    """Get PDF options based on doctype"""
    base_options = {
        "page-size": "A4",
        "margin-top": "0.5in",
        "margin-right": "0.5in",
        "margin-bottom": "0.5in",
        "margin-left": "0.5in",
        "encoding": "UTF-8",
        "no-outline": None
    }
    
    # Customize based on doctype
    doctype_options = {
        "Sales Invoice": {"orientation": "Portrait"},
        "Purchase Order": {"orientation": "Portrait"},
        "Delivery Note": {"orientation": "Portrait", "page-size": "A4"},
        "Quotation": {"orientation": "Portrait"},
        "Payment Entry": {"page-size": "A4", "margin-top": "1in"}
    }
    
    if doctype in doctype_options:
        base_options.update(doctype_options[doctype])
    
    # Override with any custom options
    base_options.update(kwargs.get('pdf_options', {}))
    
    return base_options


def generate_filename(doctype, name, print_format):
    """Generate dynamic filename"""
    timestamp = frappe.utils.now().split()[0]  # Get date part
    format_suffix = f"_{print_format}" if print_format and print_format != "Standard" else ""
    return f"{doctype.replace(' ', '_')}_{name}{format_suffix}_{timestamp}.pdf"


@frappe.whitelist(methods=["GET"])
def get_available_print_formats(doctype=None):
    """Get all available print formats for a doctype"""
    try:
        if not doctype:
            return response("Document type is required", {}, False, 400)
        
        formats = frappe.get_all(
            "Print Format",
            filters={"doc_type": doctype, "disabled": 0},
            fields=["name", "print_format_type", "standard", "default_print_format", "custom_format"]
        )
        
        return response("Print formats retrieved", formats, True, 200)
        
    except Exception as e:
        frappe.log_error(f"Print Formats Error: {str(e)}")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods=["POST"])
def bulk_print_documents(documents=None, print_options=None):
    """
    Print multiple documents of different types dynamically
    
    documents: [
        {"doctype": "Sales Invoice", "name": "INV-001", "print_format": "Standard"},
        {"doctype": "Purchase Order", "name": "PO-001"},
        ...
    ]
    """
    try:
        if isinstance(documents, str):
            documents = json.loads(documents)
        if isinstance(print_options, str):
            print_options = json.loads(print_options)
            
        if not documents or not isinstance(documents, list):
            return response("Documents list is required", {}, False, 400)
        
        print_options = print_options or {}
        results = []
        
        for doc_info in documents:
            try:
                result = print_document(
                    doctype=doc_info.get("doctype"),
                    name=doc_info.get("name"),
                    print_format=doc_info.get("print_format"),
                    output_type=print_options.get("output_type", "pdf"),
                    **print_options
                )
                
                results.append({
                    "document": doc_info,
                    "status": "success" if result.get("success") else "error",
                    "result": result
                })
                
            except Exception as e:
                results.append({
                    "document": doc_info,
                    "status": "error",
                    "error": str(e)
                })
        
        successful = len([r for r in results if r["status"] == "success"])
        failed = len([r for r in results if r["status"] == "error"])
        
        return response(
            f"Bulk print completed: {successful} successful, {failed} failed",
            {
                "results": results, 
                "summary": {"total": len(documents), "successful": successful, "failed": failed}
            },
            True,
            200
        )
        
    except Exception as e:
        frappe.log_error(f"Bulk Print Error: {str(e)}")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods=["POST"])
def print_with_filters(doctype=None, filters=None, print_format=None, limit=50):
    """
    Print multiple documents based on filters
    
    Example: Print all invoices from last month
    """
    try:
        if isinstance(filters, str):
            filters = json.loads(filters)
            
        if not doctype:
            return response("Document type is required", {}, False, 400)
        
        if not filters:
            filters = {}
        
        # Get documents based on filters
        documents = frappe.get_all(
            doctype,
            filters=filters,
            limit=int(limit),
            fields=["name"]
        )
        
        if not documents:
            return response("No documents found with given filters", {}, False, 404)
        
        # Convert to bulk print format
        doc_list = [{"doctype": doctype, "name": doc.name, "print_format": print_format} for doc in documents]
        
        return bulk_print_documents(doc_list, {"print_format": print_format})
        
    except Exception as e:
        frappe.log_error(f"Filtered Print Error: {str(e)}")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods=["POST"])
def smart_print_invoice(invoice_name=None, customer_preference=None, include_letterhead=True):
    """
    Smart printing for Sales Invoice with automatic format selection
    Integrates with your existing invoice_details endpoint
    """
    try:
        if not invoice_name:
            return response("Invoice name is required", {}, False, 400)
        
        # Get invoice details using your existing endpoint
        invoice_data = invoice_details(invoice_name)
        
        if not invoice_data.get("success"):
            return invoice_data  # Return the error from invoice_details
        
        invoice_info = invoice_data["data"]
        
        # Determine print format based on customer preference or document properties
        print_format = "Standard"
        
        if customer_preference == "detailed":
            print_format = "Detailed"
        elif customer_preference == "minimal":
            print_format = "Simple"
        
        # Check if custom format exists for this company
        company = invoice_info.get("company")
        if company:
            custom_formats = frappe.get_all(
                "Print Format",
                filters={"doc_type": "Sales Invoice", "custom_format": 1, "name": ["like", f"%{company}%"]},
                fields=["name"]
            )
            if custom_formats:
                print_format = custom_formats[0].name
        
        # Print options with letterhead integration
        print_options = {}
        if include_letterhead and invoice_info.get("letterhead"):
            letterhead_info = invoice_info["letterhead"]
            if letterhead_info.get("name"):
                print_options["letterhead"] = letterhead_info["name"]
        
        # Generate PDF
        result = print_document(
            doctype="Sales Invoice",
            name=invoice_name,
            print_format=print_format,
            output_type="pdf",
            **print_options
        )
        
        if result.get("success"):
            # Enhance result with invoice data
            result["data"]["invoice_info"] = {
                "customer": invoice_info.get("customer"),
                "grand_total": invoice_info.get("grand_total"),
                "tax_amount": invoice_info.get("tax_amount"),
                "status": invoice_info.get("status")
            }
        
        return result
        
    except Exception as e:
        frappe.log_error(f"Smart Print Error: {str(e)}")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods=["GET"])
def download_invoice_pdf(invoice_name=None, print_format=None):
    """
    Download invoice PDF directly - returns base64 encoded PDF
    Integrates with your existing authentication system
    """
    try:
        if not invoice_name:
            return response("Invoice name is required", {}, False, 400)
        
        result = smart_print_invoice(
            invoice_name=invoice_name,
            customer_preference="detailed",
            include_letterhead=True
        )
        
        if result.get("success") and result.get("data", {}).get("pdf"):
            pdf_data = result["data"]["pdf"]
            
            return response(
                "PDF generated successfully",
                {
                    "filename": pdf_data.get("filename"),
                    "pdf_base64": pdf_data.get("pdf_base64"),
                    "size": pdf_data.get("size"),
                    "content_type": "application/pdf",
                    "download_ready": True
                },
                True,
                200
            )
        else:
            return response("Failed to generate PDF", {}, False, 417)
            
    except Exception as e:
        frappe.log_error(f"Download PDF Error: {str(e)}")
        return response(str(e), {}, False, 417)


@frappe.whitelist(methods=["GET"])
def print_url_generator(doctype=None, name=None, print_format=None):
    """
    Generate print URLs for documents
    Returns both preview URL and download URL
    """
    try:
        if not doctype or not name:
            return response("Document type and name are required", {}, False, 400)
        
        site_url = frappe.utils.get_url()
        
        # Generate URLs
        preview_url = f"{site_url}/printview?doctype={doctype}&name={name}"
        if print_format:
            preview_url += f"&format={print_format}"
        
        download_url = f"{site_url}/api/method/basket4me_pwa.api.download_invoice_pdf?invoice_name={name}"
        if print_format:
            download_url += f"&print_format={print_format}"
        
        api_url = f"{site_url}/api/method/basket4me_pwa.api.print_document"
        
        return response(
            "Print URLs generated",
            {
                "preview_url": preview_url,
                "download_url": download_url if doctype == "Sales Invoice" else None,
                "api_url": api_url,
                "print_format": print_format or get_default_print_format(doctype),
                "doctype": doctype,
                "name": name
            },
            True,
            200
        )
        
    except Exception as e:
        frappe.log_error(f"URL Generator Error: {str(e)}")
        return response(str(e), {}, False, 417)


# ===== SHIFT OPENING / CLOSING ENTRY =====

@frappe.whitelist(methods="POST")
def create_shift_opening_entry(params=None):
    """Create a Shift Opening Entry when user opens a shift."""
    try:
        if isinstance(params, str):
            params = json.loads(params)
        params = params or {}

        user = frappe.session.user
        company = params.get("company", "")

        # Auto-detect company from Basket4Me Settings if not provided
        if not company:
            try:
                vs = get_basket4me_settings()
                company = vs.company or ""
            except Exception:
                pass

        if not company:
            return response("Company is required", {}, False, 400)

        # Get sales person for current user
        sales_person = ""
        try:
            sp_list = frappe.get_all(
                "Sales Person",
                filters={"user": user, "enabled": 1},
                fields=["name"],
                limit=1,
            )
            if sp_list:
                sales_person = sp_list[0].name
        except Exception:
            pass

        # Build opening balance details from params
        balance_details = params.get("balance_details", [])
        if not balance_details:
            # Default: Cash with 0 opening
            balance_details = [{"mode_of_payment": "Cash", "opening_amount": 0}]

        doc = frappe.get_doc({
            "doctype": "Shift Opening Entry",
            "user": user,
            "company": company,
            "sales_person": sales_person,
            "posting_date": nowdate(),
            "period_start_date": frappe.utils.now_datetime(),
            "status": "Open",
            "balance_details": balance_details,
        })
        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        return response(
            "Shift opening entry created",
            {
                "name": doc.name,
                "user": doc.user,
                "sales_person": doc.sales_person,
                "company": doc.company,
                "period_start_date": str(doc.period_start_date),
                "posting_date": str(doc.posting_date),
                "status": doc.status,
            },
            True,
            200,
        )
    except Exception as e:
        frappe.log_error(f"Shift Opening Error: {str(e)}")
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def create_shift_closing_entry(params=None):
    """Create a Shift Closing Entry when user closes a shift.
    Aggregates all Sales Invoices from the shift period automatically."""
    try:
        if isinstance(params, str):
            params = json.loads(params)
        params = params or {}

        shift_opening_entry = params.get("shift_opening_entry")
        if not shift_opening_entry:
            return response("Shift Opening Entry is required", {}, False, 400)

        # Verify opening entry exists and is Open
        if not frappe.db.exists("Shift Opening Entry", shift_opening_entry):
            return response(f"Shift Opening Entry {shift_opening_entry} not found", {}, False, 404)

        opening_doc = frappe.get_doc("Shift Opening Entry", shift_opening_entry)
        if opening_doc.status != "Open":
            return response(
                f"Shift Opening Entry {shift_opening_entry} is not Open (status: {opening_doc.status})",
                {},
                False,
                400,
            )

        # Build payment reconciliation with closing amounts from frontend
        closing_amounts = params.get("closing_amounts", {})

        doc = frappe.get_doc({
            "doctype": "Shift Closing Entry",
            "shift_opening_entry": shift_opening_entry,
            "company": opening_doc.company,
            "user": opening_doc.user,
            "sales_person": opening_doc.sales_person,
            "posting_date": nowdate(),
            "posting_time": frappe.utils.nowtime(),
            "period_start_date": opening_doc.period_start_date,
            "period_end_date": frappe.utils.now_datetime(),
            "status": "Draft",
        })

        # Validate triggers set_totals_from_invoices which populates everything
        doc.insert(ignore_permissions=True)

        # Update closing amounts from frontend if provided
        if closing_amounts:
            for row in doc.payment_reconciliation:
                if row.mode_of_payment in closing_amounts:
                    row.closing_amount = flt(closing_amounts[row.mode_of_payment])
                    row.difference = row.closing_amount - row.expected_amount
            doc.save(ignore_permissions=True)

        # Mark as Submitted
        doc.status = "Submitted"
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        return response(
            "Shift closing entry created",
            {
                "name": doc.name,
                "shift_opening_entry": doc.shift_opening_entry,
                "grand_total": doc.grand_total,
                "net_total": doc.net_total,
                "total_quantity": doc.total_quantity,
                "total_invoices": doc.total_invoices,
                "period_start_date": str(doc.period_start_date),
                "period_end_date": str(doc.period_end_date),
                "payment_reconciliation": [
                    {
                        "mode_of_payment": r.mode_of_payment,
                        "opening_amount": r.opening_amount,
                        "expected_amount": r.expected_amount,
                        "closing_amount": r.closing_amount,
                        "difference": r.difference,
                    }
                    for r in (doc.payment_reconciliation or [])
                ],
                "status": doc.status,
            },
            True,
            200,
        )
    except Exception as e:
        frappe.log_error(f"Shift Closing Error: {str(e)}")
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_open_shift():
    """Get the current user's open shift entry."""
    try:
        user = frappe.session.user
        shifts = frappe.get_all(
            "Shift Opening Entry",
            filters={"user": user, "status": "Open"},
            fields=["name", "user", "sales_person", "company", "posting_date",
                     "period_start_date", "status"],
            order_by="creation desc",
            limit=1,
        )
        if shifts:
            return response("Open shift found", shifts[0], True, 200)
        return response("No open shift", None, True, 200)
    except Exception as e:
        return response(str(e), {}, False, 500)


# ==================== SALES ORDER APIs ====================

@frappe.whitelist(methods="POST")
def create_sales_order(params):
    """
    Create a Sales Order in Draft mode.

    Args (via params dict):
        customer: Customer name (required)
        items: list of {"item_code", "qty", "uom", "rate", "discount_percentage", "discount_amount"} (required)
        delivery_date: Delivery date (optional, defaults to today)
        price_list: User-selected price list (optional, overrides auto-detected price list)
        po_no: Purchase Order number (optional)
        remarks: Remarks (optional)
        custom_payment_type: Payment type (optional)
        additional_discount_amount: Overall discount amount (optional)
    """
    try:
        if isinstance(params, str):
            params = json.loads(params)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        customer = params.get("customer")

        settings = get_basket4me_settings()
        sales_person_details = None
        for detail in settings.sales_person_details:
            if detail.sales_person == sales_person:
                sales_person_details = detail

        if not sales_person_details:
            return response(f"No Basket4Me Settings found for sales person {sales_person}", {}, False, 400)

        # User-selected price list overrides auto-detection
        user_price_list = params.get("price_list") or params.get("selling_price_list")
        if user_price_list and frappe.db.exists("Price List", user_price_list):
            effective_price_list = user_price_list
        else:
            effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)

        items = params.get("items")
        delivery_date = params.get("delivery_date") or nowdate()
        posting_date = params.get("posting_date") or nowdate()
        payment_type = params.get("custom_payment_type") or params.get("payment_type")
        additional_discount_amount = flt(params.get("additional_discount_amount") or params.get("discount_amount") or 0)

        if not customer:
            return response("Customer is required", {}, False, 400)
        if not items or not isinstance(items, list):
            return response("At least one item is required", {}, False, 400)

        so = frappe.new_doc("Sales Order")
        so.customer = customer
        so.customer_name = params.get("customer_name")
        so.company = sales_person_details.company
        so.transaction_date = posting_date
        so.delivery_date = delivery_date
        so.order_type = "Sales"
        so.selling_price_list = effective_price_list
        so.set_warehouse = sales_person_details.warehouse
        so.cost_center = sales_person_details.cost_center
        if payment_type:
            so.custom_payment_type = payment_type
        so.custom_mobile_app = 1

        response_items = []
        for item in items:
            item_code = item.get("item_code")
            qty = item.get("qty", 1)
            uom = item.get("uom")
            description = item.get("description")
            if description:
                description = strip_html_tags(description)
            provided_rate = item.get("rate")
            is_free_item = item.get("is_free_item", False)

            item_price = frappe.db.sql(
                """
                SELECT price_list_rate
                FROM `tabItem Price`
                WHERE item_code = %(item_code)s
                AND uom = %(uom)s
                AND price_list = %(price_list)s
                ORDER BY uom DESC, creation DESC
                LIMIT 1
                """,
                {"item_code": item_code, "uom": uom, "price_list": effective_price_list},
                as_dict=True
            )

            latest_item_price = item_price[0]["price_list_rate"] if item_price else None

            # price_list_rate is the reference rate from Item Price
            price_list_rate = flt(latest_item_price) if latest_item_price is not None else flt(provided_rate)

            # Actual rate: use frontend-provided rate; fallback to price list rate
            if is_free_item:
                rate = 0
            elif provided_rate is not None and str(provided_rate).strip() != "":
                rate = flt(provided_rate)
            else:
                rate = price_list_rate

            provided_discount_percentage = flt(item.get("discount_percentage", 0))
            provided_discount_amount = flt(item.get("discount_amount", 0))

            if provided_discount_percentage and price_list_rate:
                discount_percentage = provided_discount_percentage
                discount_amount = (price_list_rate * discount_percentage) / 100
            elif provided_discount_amount and price_list_rate:
                discount_amount = provided_discount_amount
                discount_percentage = (discount_amount / price_list_rate) * 100
            else:
                discount_percentage = 0
                discount_amount = 0

            discounted_rate = rate - discount_amount

            item_data = {
                "item_code": item_code,
                "qty": qty,
                "uom": uom,
                "description": description,
                "warehouse": sales_person_details.warehouse,
                "cost_center": sales_person_details.cost_center,
                "discount_percentage": discount_percentage,
                "discount_amount": discount_amount,
                "rate": discounted_rate,
                "price_list_rate": price_list_rate,
                "delivery_date": delivery_date,
                "is_free_item": is_free_item
            }

            batch_no = item.get("batch_no")
            if batch_no:
                item_data["use_serial_batch_fields"] = 1
                item_data["batch_no"] = batch_no

            so.append("items", item_data)

            # Get UOM conversion factor
            conversion_factor = 1.0
            if uom:
                cf = frappe.db.get_value("UOM Conversion Detail",
                    {"parent": item_code, "uom": uom}, "conversion_factor")
                if cf:
                    conversion_factor = flt(cf)

            response_items.append({
                "item_code": item_code,
                "description": description,
                "qty": qty,
                "uom": uom,
                "stock_uom": frappe.db.get_value("Item", item_code, "stock_uom"),
                "conversion_factor": conversion_factor,
                "discount_amount": discount_amount,
                "discount_percentage": discount_percentage,
                "price_list_rate": price_list_rate,
                "rate": discounted_rate,
                "is_free_item": is_free_item
            })

        frappe.flags.ignore_permissions = True
        so.run_method("set_missing_values")
        frappe.flags.ignore_permissions = False

        if additional_discount_amount:
            so.apply_discount_on = "Net Total"
            so.discount_amount = additional_discount_amount

        so.run_method("calculate_taxes_and_totals")

        po_no = params.get("po_no")
        remarks_text = params.get("remarks")
        if po_no:
            so.po_no = po_no
        if remarks_text:
            so.remarks = remarks_text

        so.append("sales_team", {
            "sales_person": sales_person,
            "allocated_percentage": 100
        })

        so.insert(ignore_permissions=True)
        frappe.db.commit()

        return response(
            "Sales Order created successfully",
            {
                "name": so.name,
                "docstatus": so.docstatus,
                "status": so.status,
                "customer": so.customer,
                "customer_name": so.customer_name,
                "posting_date": str(so.transaction_date),
                "delivery_date": str(so.delivery_date),
                "total": so.total,
                "grand_total": so.grand_total,
                "items": response_items,
            },
            True,
            200,
        )
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Create Sales Order Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def update_sales_order(params):
    """
    Update an existing Draft Sales Order.

    Args (via params dict):
        name: Sales Order name (required)
        items: list of items to replace (optional)
        delivery_date: Updated delivery date (optional)
        po_no: Purchase Order number (optional)
        remarks: Remarks (optional)
        custom_payment_type: Payment type (optional)
        additional_discount_amount: Overall discount amount (optional)
    """
    try:
        if isinstance(params, str):
            params = json.loads(params)

        so_name = params.get("name")
        if not so_name:
            return response("Sales Order name is required", {}, False, 400)

        if not frappe.db.exists("Sales Order", so_name):
            return response(f"Sales Order {so_name} not found", {}, False, 404)

        so = frappe.get_doc("Sales Order", so_name)

        if so.docstatus != 0:
            return response("Can only update Sales Order in Draft state", {}, False, 400)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        settings = get_basket4me_settings()
        sales_person_details = None
        for detail in settings.sales_person_details:
            if detail.sales_person == sales_person:
                sales_person_details = detail

        if not sales_person_details:
            return response(f"No Basket4Me Settings found for sales person {sales_person}", {}, False, 400)

        customer = params.get("customer") or so.customer
        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)

        items = params.get("items")
        delivery_date = params.get("delivery_date") or str(so.delivery_date)

        if params.get("customer"):
            so.customer = params.get("customer")
        if params.get("customer_name"):
            so.customer_name = params.get("customer_name")
        if params.get("delivery_date"):
            so.delivery_date = params.get("delivery_date")
        if params.get("custom_payment_type") or params.get("payment_type"):
            so.custom_payment_type = params.get("custom_payment_type") or params.get("payment_type")
        if params.get("po_no"):
            so.po_no = params.get("po_no")
        if params.get("remarks"):
            so.remarks = params.get("remarks")

        additional_discount_amount = flt(params.get("additional_discount_amount") or params.get("discount_amount") or 0)

        if items and isinstance(items, list):
            so.items = []
            so.selling_price_list = effective_price_list

            for item in items:
                item_code = item.get("item_code")
                qty = item.get("qty", 1)
                uom = item.get("uom")
                description = item.get("description")
                if description:
                    description = strip_html_tags(description)
                provided_rate = item.get("rate")
                is_free_item = item.get("is_free_item", False)

                item_price = frappe.db.sql(
                    """
                    SELECT price_list_rate
                    FROM `tabItem Price`
                    WHERE item_code = %(item_code)s
                    AND uom = %(uom)s
                    AND price_list = %(price_list)s
                    ORDER BY uom DESC, creation DESC
                    LIMIT 1
                    """,
                    {"item_code": item_code, "uom": uom, "price_list": effective_price_list},
                    as_dict=True
                )

                latest_item_price = item_price[0]["price_list_rate"] if item_price else None

                # price_list_rate is the reference rate from Item Price
                price_list_rate = flt(latest_item_price) if latest_item_price is not None else flt(provided_rate)

                # Actual rate: use frontend-provided rate; fallback to price list rate
                if is_free_item:
                    rate = 0
                elif provided_rate is not None and str(provided_rate).strip() != "":
                    rate = flt(provided_rate)
                else:
                    rate = price_list_rate

                provided_discount_percentage = flt(item.get("discount_percentage", 0))
                provided_discount_amount = flt(item.get("discount_amount", 0))

                if provided_discount_percentage and price_list_rate:
                    discount_percentage = provided_discount_percentage
                    discount_amount = (price_list_rate * discount_percentage) / 100
                elif provided_discount_amount and price_list_rate:
                    discount_amount = provided_discount_amount
                    discount_percentage = (discount_amount / price_list_rate) * 100
                else:
                    discount_percentage = 0
                    discount_amount = 0

                discounted_rate = rate - discount_amount

                item_data = {
                    "item_code": item_code,
                    "qty": qty,
                    "uom": uom,
                    "description": description,
                    "warehouse": sales_person_details.warehouse,
                    "cost_center": sales_person_details.cost_center,
                    "discount_percentage": discount_percentage,
                    "discount_amount": discount_amount,
                    "rate": discounted_rate,
                    "price_list_rate": price_list_rate,
                    "delivery_date": delivery_date,
                    "is_free_item": is_free_item
                }

                batch_no = item.get("batch_no")
                if batch_no:
                    item_data["use_serial_batch_fields"] = 1
                    item_data["batch_no"] = batch_no

                so.append("items", item_data)

        frappe.flags.ignore_permissions = True
        so.run_method("set_missing_values")
        frappe.flags.ignore_permissions = False

        if additional_discount_amount:
            so.apply_discount_on = "Net Total"
            so.discount_amount = additional_discount_amount

        so.run_method("calculate_taxes_and_totals")
        so.save(ignore_permissions=True)
        frappe.db.commit()

        return response(
            "Sales Order updated successfully",
            {
                "name": so.name,
                "docstatus": so.docstatus,
                "status": so.status,
                "customer": so.customer,
                "customer_name": so.customer_name,
                "total": so.total,
                "grand_total": so.grand_total,
                "items": [
                    {
                        "item_code": item.item_code,
                        "item_name": item.item_name,
                        "qty": item.qty,
                        "rate": item.rate,
                        "amount": item.amount
                    } for item in so.items
                ],
            },
            True,
            200,
        )
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Update Sales Order Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def submit_sales_order(params):
    """Submit a Draft Sales Order."""
    try:
        if isinstance(params, str):
            params = json.loads(params)

        so_name = params.get("name")
        if not so_name:
            return response("Sales Order name is required", {}, False, 400)

        if not frappe.db.exists("Sales Order", so_name):
            return response(f"Sales Order {so_name} not found", {}, False, 404)

        so = frappe.get_doc("Sales Order", so_name)
        if so.docstatus != 0:
            return response("Sales Order is not in Draft state", {}, False, 400)

        so.submit()
        frappe.db.commit()

        return response(
            "Sales Order submitted successfully",
            {
                "name": so.name,
                "docstatus": so.docstatus,
                "status": so.status,
                "grand_total": so.grand_total,
            },
            True,
            200,
        )
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Submit Sales Order Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def cancel_sales_order(params):
    """Cancel a submitted Sales Order."""
    try:
        if isinstance(params, str):
            params = json.loads(params)

        so_name = params.get("name")
        if not so_name:
            return response("Sales Order name is required", {}, False, 400)

        if not frappe.db.exists("Sales Order", so_name):
            return response(f"Sales Order {so_name} not found", {}, False, 404)

        so = frappe.get_doc("Sales Order", so_name)
        if so.docstatus != 1:
            return response("Only submitted Sales Orders can be cancelled", {}, False, 400)

        so.cancel()
        frappe.db.commit()

        return response(
            "Sales Order cancelled successfully",
            {"name": so.name, "docstatus": so.docstatus, "status": so.status},
            True,
            200,
        )
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Cancel Sales Order Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def delete_sales_order(params):
    """Delete a Draft or Cancelled Sales Order."""
    try:
        if isinstance(params, str):
            params = json.loads(params)

        so_name = params.get("name")
        if not so_name:
            return response("Sales Order name is required", {}, False, 400)

        if not frappe.db.exists("Sales Order", so_name):
            return response(f"Sales Order {so_name} not found", {}, False, 404)

        so = frappe.get_doc("Sales Order", so_name)
        if so.docstatus == 1:
            return response("Cannot delete a submitted Sales Order. Cancel it first.", {}, False, 400)

        frappe.delete_doc("Sales Order", so_name, ignore_permissions=True)
        frappe.db.commit()

        return response(f"Sales Order {so_name} deleted successfully", {}, True, 200)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Delete Sales Order Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_sales_order_list(name=None, customer=None, status=None, search=None, from_date=None, to_date=None, route=None, page_number=1, page_size=20, limit_start=None, limit_page_length=None):
    """
    List Sales Orders with filters.

    Query params:
        name: Filter by exact SO name
        customer: Filter by customer
        status: Filter by status (Draft, To Deliver and Bill, Completed, Cancelled, etc.)
        search: Search by SO name or customer name
        from_date / to_date: Date range filter on transaction_date
        route: Filter by Customer Route
        page_number / page_size: Pagination
    """
    try:
        # Support both pagination styles
        _page_size = int(limit_page_length or page_size or 20)
        if limit_start is not None:
            _offset = int(limit_start)
        else:
            _offset = (int(page_number or 1) - 1) * _page_size

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")

        filters = {}
        if name:
            filters["name"] = name
        if customer:
            filters["customer"] = customer
        if status:
            if status == "Draft":
                filters["docstatus"] = 0
            elif status == "Cancelled":
                filters["docstatus"] = 2
            else:
                filters["docstatus"] = 1
                filters["status"] = status

        if from_date and to_date:
            filters["transaction_date"] = ["between", [from_date, to_date]]
        elif from_date:
            filters["transaction_date"] = [">=", from_date]
        elif to_date:
            filters["transaction_date"] = ["<=", to_date]

        # Route filter - filter by customers that belong to a route
        if route:
            route_customers = frappe.get_all(
                "Customer",
                filters={"custom_route": route},
                pluck="name"
            )
            if route_customers:
                filters["customer"] = ["in", route_customers]
            else:
                return response("No customers found for this route", {"sales_orders": [], "total_count": 0, "page_number": int(page_number or 1), "page_size": _page_size}, True, 200)

        or_filters = None
        if search:
            or_filters = [
                ["name", "like", f"%{search}%"],
                ["customer_name", "like", f"%{search}%"],
            ]

        fields = [
            "name", "customer", "customer_name", "transaction_date", "delivery_date",
            "docstatus", "status", "total", "net_total", "grand_total", "currency",
            "per_delivered", "per_billed", "customer_address",
            "creation", "owner"
        ]

        # Add optional columns if they exist
        for optional_field in ["remarks", "custom_route"]:
            if frappe.db.has_column("Sales Order", optional_field):
                fields.append(optional_field)

        sales_orders = frappe.get_all(
            "Sales Order",
            filters=filters,
            or_filters=or_filters,
            fields=fields,
            order_by="creation desc",
            limit_start=_offset,
            limit_page_length=_page_size
        )

        total_count = frappe.db.count("Sales Order", filters=filters)

        for so in sales_orders:
            # Get customer address details
            if so.get("customer_address"):
                addr = frappe.db.get_value("Address", so["customer_address"],
                    ["address_line1", "address_line2", "city", "state", "pincode", "country"],
                    as_dict=True)
                so["address_display"] = addr if addr else None
            else:
                so["address_display"] = None

            # Get customer route
            if not so.get("custom_route"):
                so["custom_route"] = frappe.db.get_value("Customer", so["customer"], "custom_route") or None

            # Created info
            so["created_date"] = str(so.get("creation", ""))[:10] if so.get("creation") else None
            so["created_by"] = frappe.db.get_value("User", so.get("owner"), "full_name") or so.get("owner")

            so["items"] = frappe.get_all(
                "Sales Order Item",
                filters={"parent": so["name"]},
                fields=[
                    "item_code", "item_name", "qty", "uom", "rate", "amount",
                    "price_list_rate", "discount_percentage", "discount_amount",
                    "conversion_factor", "stock_uom"
                ]
            )

        return response(
            "Sales Orders fetched successfully",
            {
                "sales_orders": sales_orders,
                "total_count": total_count,
                "page_number": int(page_number or 1),
                "page_size": _page_size,
            },
            True,
            200,
        )
    except Exception as e:
        frappe.log_error(title="Get Sales Orders Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_sales_order_detail(name=None):
    """Get single Sales Order with full details."""
    try:
        if not name:
            return response("Sales Order name is required", {}, False, 400)

        if not frappe.db.exists("Sales Order", name):
            return response(f"Sales Order {name} not found", {}, False, 404)

        so = frappe.get_doc("Sales Order", name)

        # Customer address
        address_display = None
        if so.customer_address:
            address_display = frappe.db.get_value("Address", so.customer_address,
                ["address_line1", "address_line2", "city", "state", "pincode", "country"],
                as_dict=True)

        # Customer route
        customer_route = None
        if hasattr(so, "custom_route") and so.custom_route:
            customer_route = so.custom_route
        else:
            customer_route = frappe.db.get_value("Customer", so.customer, "custom_route") or None

        # Build items with UOM details and pricing
        items_data = []
        for item in so.items:
            ic = item.item_code
            stock_uom = item.stock_uom or "Nos"

            uom_details = frappe.db.sql("""
                SELECT uom, conversion_factor
                FROM `tabUOM Conversion Detail`
                WHERE parent = %s AND parenttype = 'Item'
                ORDER BY idx ASC
            """, ic, as_dict=True)
            if not uom_details:
                uom_details = [{"uom": stock_uom, "conversion_factor": 1.0}]

            # MRP from "MRP" price list based on default UOM
            mrp_result = frappe.db.sql("""
                SELECT price_list_rate FROM `tabItem Price`
                WHERE selling = 1 AND item_code = %s AND price_list = 'MRP'
                AND (uom = %s OR uom IS NULL OR uom = '')
                ORDER BY valid_from DESC, creation DESC LIMIT 1
            """, (ic, stock_uom), as_dict=True)
            mrp = mrp_result[0]["price_list_rate"] if mrp_result else 0.0

            # Standard Selling Price based on default UOM
            std_result = frappe.db.sql("""
                SELECT price_list_rate FROM `tabItem Price`
                WHERE selling = 1 AND item_code = %s AND price_list = 'Standard Selling'
                AND (uom = %s OR uom IS NULL OR uom = '')
                ORDER BY valid_from DESC, creation DESC LIMIT 1
            """, (ic, stock_uom), as_dict=True)
            standard_selling_price = std_result[0]["price_list_rate"] if std_result else 0.0

            # Last Customer Rate from SO and SI (whichever is most recent)
            # Also get the price list name and price list rate from that transaction
            last_customer_rate = 0.0
            last_customer_price_list = None
            last_customer_price_list_rate = 0.0
            if so.customer:
                last_rate = frappe.db.sql("""
                    SELECT rate, price_list, price_list_rate, txn_date FROM (
                        SELECT soi.rate, so2.selling_price_list as price_list,
                               soi.price_list_rate, so2.transaction_date as txn_date, so2.creation
                        FROM `tabSales Order Item` soi
                        JOIN `tabSales Order` so2 ON so2.name = soi.parent
                        WHERE so2.customer = %s AND soi.item_code = %s
                        AND so2.docstatus != 2
                        UNION ALL
                        SELECT sii.rate, si.selling_price_list as price_list,
                               sii.price_list_rate, si.posting_date as txn_date, si.creation
                        FROM `tabSales Invoice Item` sii
                        JOIN `tabSales Invoice` si ON si.name = sii.parent
                        WHERE si.customer = %s AND sii.item_code = %s
                        AND si.docstatus = 1 AND si.is_return = 0
                    ) combined
                    ORDER BY txn_date DESC, creation DESC LIMIT 1
                """, (so.customer, ic, so.customer, ic), as_dict=True)
                if last_rate:
                    last_customer_rate = last_rate[0].get("rate") or 0.0
                    last_customer_price_list = last_rate[0].get("price_list") or None
                    last_customer_price_list_rate = last_rate[0].get("price_list_rate") or 0.0

            items_data.append({
                "name": item.name,
                "item_code": ic,
                "item_name": item.item_name,
                "description": item.description,
                "qty": item.qty,
                "uom": item.uom,
                "stock_uom": stock_uom,
                "conversion_factor": item.conversion_factor,
                "rate": item.rate,
                "price_list_rate": item.price_list_rate,
                "discount_percentage": item.discount_percentage,
                "discount_amount": item.discount_amount,
                "amount": item.amount,
                "warehouse": item.warehouse,
                "delivery_date": str(item.delivery_date) if item.delivery_date else None,
                "currency": so.currency,
                "is_free_item": getattr(item, "is_free_item", 0),
                "batch_no": getattr(item, "batch_no", None),
                "uoms": [{"uom": u.get("uom"), "conversion_factor": u.get("conversion_factor", 1.0)} for u in uom_details],
                "mrp": mrp,
                "standard_selling_price": standard_selling_price,
                "last_customer_rate": last_customer_rate,
                "last_customer_price_list": last_customer_price_list,
                "last_customer_price_list_rate": last_customer_price_list_rate,
            })

        return response(
            "Sales Order fetched successfully",
            {
                "name": so.name,
                "customer": so.customer,
                "customer_name": so.customer_name,
                "customer_address": so.customer_address,
                "address_display": address_display,
                "customer_route": customer_route,
                "transaction_date": str(so.transaction_date),
                "delivery_date": str(so.delivery_date),
                "docstatus": so.docstatus,
                "status": so.status,
                "company": so.company,
                "currency": so.currency,
                "selling_price_list": so.selling_price_list,
                "total": so.total,
                "net_total": so.net_total,
                "total_taxes_and_charges": so.total_taxes_and_charges,
                "grand_total": so.grand_total,
                "per_delivered": so.per_delivered,
                "per_billed": so.per_billed,
                "po_no": so.po_no,
                "remarks": getattr(so, "remarks", None),
                "created_date": str(so.creation)[:10] if so.creation else None,
                "created_by": frappe.db.get_value("User", so.owner, "full_name") or so.owner,
                "items": items_data,
                "taxes": [
                    {
                        "charge_type": tax.charge_type,
                        "account_head": tax.account_head,
                        "description": tax.description,
                        "rate": tax.rate,
                        "tax_amount": tax.tax_amount,
                        "total": tax.total,
                        "included_in_print_rate": tax.included_in_print_rate
                    } for tax in so.taxes
                ] if so.taxes else [],
                "sales_team": [
                    {
                        "sales_person": st.sales_person,
                        "allocated_percentage": st.allocated_percentage,
                    } for st in so.sales_team
                ] if so.sales_team else [],
            },
            True,
            200,
        )
    except Exception as e:
        frappe.log_error(title="Get Sales Order Detail Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def convert_so_to_si(params):
    """
    Convert one or more submitted Sales Orders into a single Sales Invoice.
    Draft Sales Orders will be automatically submitted first.

    Args (via params dict):
        sales_orders: list of Sales Order names ["SO-001", "SO-002"] (required)
        payments: list of payment entries [{"mode_of_payment": "Cash", "amount": 100}] (optional)
    """
    try:
        if isinstance(params, str):
            params = json.loads(params)

        sales_orders = params.get("sales_orders")
        payments = params.get("payments")

        if not sales_orders or not isinstance(sales_orders, list) or len(sales_orders) == 0:
            return response("sales_orders list is required", {}, False, 400)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        settings = get_basket4me_settings()
        sales_person_details = None
        for detail in settings.sales_person_details:
            if detail.sales_person == sales_person:
                sales_person_details = detail

        if not sales_person_details:
            return response(f"No Basket4Me Settings found for sales person {sales_person}", {}, False, 400)

        # Validate all SOs exist and belong to same customer
        first_so = frappe.get_doc("Sales Order", sales_orders[0])
        for so_name in sales_orders:
            if not frappe.db.exists("Sales Order", so_name):
                return response(f"Sales Order {so_name} not found", {}, False, 404)
            so = frappe.get_doc("Sales Order", so_name)
            if so.customer != first_so.customer:
                return response(f"All Sales Orders must belong to same customer. {so_name} has different customer.", {}, False, 400)
            if so.status == "Completed" or so.per_billed >= 100:
                return response(f"Sales Order {so_name} is already fully billed", {}, False, 400)
            if so.per_billed > 0:
                return response(f"Sales Order {so_name} is partially billed ({so.per_billed}%)", {}, False, 400)

        # Submit draft SOs
        for so_name in sales_orders:
            so = frappe.get_doc("Sales Order", so_name)
            if so.docstatus == 0:
                so.submit()

        # Reload first SO after submission
        first_so = frappe.get_doc("Sales Order", sales_orders[0])

        # Create Sales Invoice
        si = frappe.new_doc("Sales Invoice")
        si.customer = first_so.customer
        si.customer_name = first_so.customer_name
        si.company = sales_person_details.company
        si.posting_date = nowdate()
        si.due_date = nowdate()
        si.currency = first_so.currency
        si.selling_price_list = first_so.selling_price_list
        si.cost_center = sales_person_details.cost_center
        si.update_stock = 1
        si.custom_mobile_app = 1

        # Copy taxes from first SO
        for tax in first_so.taxes or []:
            tax_amount = tax.tax_amount if tax.charge_type == "Actual" else 0
            si.append("taxes", {
                "charge_type": tax.charge_type,
                "account_head": tax.account_head,
                "description": tax.description,
                "rate": tax.rate,
                "cost_center": tax.cost_center,
                "included_in_print_rate": tax.included_in_print_rate,
                "tax_amount": tax_amount
            })

        # Add items from all SOs with SO linkage
        for so_name in sales_orders:
            so = frappe.get_doc("Sales Order", so_name)
            for item in so.items:
                si.append("items", {
                    "item_code": item.item_code,
                    "item_name": item.item_name,
                    "description": item.description,
                    "qty": item.qty,
                    "uom": item.uom,
                    "rate": item.rate,
                    "price_list_rate": item.price_list_rate,
                    "discount_percentage": item.discount_percentage,
                    "discount_amount": item.discount_amount,
                    "warehouse": item.warehouse,
                    "cost_center": sales_person_details.cost_center,
                    "sales_order": so.name,
                    "so_detail": item.name,
                })

        # Add payments
        if payments and isinstance(payments, list):
            for payment in payments:
                si.append("payments", {
                    "mode_of_payment": payment.get("mode_of_payment"),
                    "amount": payment.get("amount", 0)
                })

        si.append("sales_team", {
            "sales_person": sales_person,
            "allocated_percentage": 100
        })

        si.insert(ignore_permissions=True)
        si.submit()
        frappe.db.commit()

        return response(
            "Sales Invoice created from Sales Order(s)",
            {
                "sales_invoice": si.name,
                "sales_orders_linked": sales_orders,
                "customer": si.customer,
                "customer_name": si.customer_name,
                "grand_total": si.grand_total,
                "docstatus": si.docstatus,
                "items": [
                    {
                        "item_code": item.item_code,
                        "item_name": item.item_name,
                        "qty": item.qty,
                        "rate": item.rate,
                        "amount": item.amount,
                        "sales_order": item.sales_order
                    } for item in si.items
                ],
            },
            True,
            200,
        )
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Convert SO to SI Error", message=str(e))
        return response(str(e), {}, False, 500)


# ==================== DASHBOARD APIs ====================

@frappe.whitelist(methods="GET")
def get_dashboard_summary(period="daily", from_date=None, to_date=None):
    """
    Dashboard summary for Van Sales workflow.

    Args:
        period: "daily" | "weekly" | "monthly" (ignored if from_date/to_date provided)
        from_date / to_date: Custom date range
    Returns:
        Total Order Value, Total Customers, Total Collection, Total Invoices,
        Total Returns, Total Visited Customers
    """
    try:
        today = nowdate()
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        if not sales_person:
            return response("No Sales Person linked to the logged-in user", {}, False, 400)

        if from_date and to_date:
            start, end = from_date, to_date
        elif period == "weekly":
            from frappe.utils import add_days, get_first_day_of_week
            start = get_first_day_of_week(today)
            end = today
        elif period == "monthly":
            from frappe.utils import get_first_day
            start = get_first_day(today)
            end = today
        else:
            start = end = today

        override = should_override_sales_team()

        # Sales Order totals
        so_join = "LEFT JOIN" if override else "JOIN"
        so_cond = "(st.sales_person = %s OR st.sales_person IS NULL)" if override else "st.sales_person = %s"
        so_sql = f"""
            SELECT COALESCE(SUM(so.grand_total), 0) as total_value,
                   COUNT(DISTINCT so.name) as total_count,
                   COUNT(DISTINCT so.customer) as total_customers
            FROM `tabSales Order` so
            {so_join} `tabSales Team` st ON so.name = st.parent
            WHERE so.docstatus = 1 AND {so_cond}
            AND so.transaction_date BETWEEN %s AND %s
        """
        so_row = frappe.db.sql(so_sql, (sales_person, start, end), as_dict=True)[0]

        # Sales Invoice totals (non-return)
        si_join = "LEFT JOIN" if override else "JOIN"
        si_cond = "(st.sales_person = %s OR st.sales_person IS NULL)" if override else "st.sales_person = %s"
        si_sql = f"""
            SELECT COALESCE(SUM(si.grand_total), 0) as total_value,
                   COUNT(DISTINCT si.name) as total_count,
                   COUNT(DISTINCT si.customer) as total_customers
            FROM `tabSales Invoice` si
            {si_join} `tabSales Team` st ON si.name = st.parent
            WHERE si.docstatus = 1 AND si.is_return = 0 AND {si_cond}
            AND si.posting_date BETWEEN %s AND %s
        """
        si_row = frappe.db.sql(si_sql, (sales_person, start, end), as_dict=True)[0]

        # Return totals
        ret_sql = f"""
            SELECT COALESCE(SUM(ABS(si.grand_total)), 0) as total_value,
                   COUNT(DISTINCT si.name) as total_count,
                   COUNT(DISTINCT si.customer) as total_customers
            FROM `tabSales Invoice` si
            {si_join} `tabSales Team` st ON si.name = st.parent
            WHERE si.docstatus = 1 AND si.is_return = 1 AND {si_cond}
            AND si.posting_date BETWEEN %s AND %s
        """
        ret_row = frappe.db.sql(ret_sql, (sales_person, start, end), as_dict=True)[0]

        # Collection totals
        coll_sql = """
            SELECT COALESCE(SUM(pe.paid_amount), 0) as total_value,
                   COUNT(DISTINCT pe.name) as total_count,
                   COUNT(DISTINCT pe.party) as total_customers
            FROM `tabPayment Entry` pe
            WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive'
            AND pe.posting_date BETWEEN %s AND %s
            AND pe.custom_sales_person = %s
        """
        coll_row = frappe.db.sql(coll_sql, (start, end, sales_person), as_dict=True)[0]

        # Visited customers (using Activity Log or custom tracking)
        visited_customers = 0
        if frappe.db.exists("DocType", "Customer Visit"):
            visited_customers = frappe.db.count("Customer Visit", {
                "sales_person": sales_person,
                "visit_date": ["between", [start, end]],
                "docstatus": ["!=", 2]
            })

        return response(
            "Dashboard summary fetched",
            {
                "period": period,
                "from_date": str(start),
                "to_date": str(end),
                "sales_person": sales_person,
                "orders": {
                    "total_value": so_row.total_value,
                    "total_count": so_row.total_count,
                    "total_customers": so_row.total_customers,
                },
                "invoices": {
                    "total_value": si_row.total_value,
                    "total_count": si_row.total_count,
                    "total_customers": si_row.total_customers,
                },
                "returns": {
                    "total_value": ret_row.total_value,
                    "total_count": ret_row.total_count,
                    "total_customers": ret_row.total_customers,
                },
                "collections": {
                    "total_value": coll_row.total_value,
                    "total_count": coll_row.total_count,
                    "total_customers": coll_row.total_customers,
                },
                "visited_customers": visited_customers,
            },
            True, 200,
        )
    except Exception as e:
        frappe.log_error(title="Dashboard Summary Error", message=str(e))
        return response(str(e), {}, False, 500)


# ==================== CUSTOMER VISIT APIs ====================

@frappe.whitelist(methods="POST")
def mark_customer_visit(params):
    """
    Record a customer visit.

    Args (via params):
        customer: Customer name (required)
        latitude / longitude: GPS coordinates (optional)
        remarks: Visit notes (optional)
    """
    try:
        if isinstance(params, str):
            params = json.loads(params)

        customer = params.get("customer")
        if not customer:
            return response("Customer is required", {}, False, 400)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        if not sales_person:
            return response("No Sales Person linked", {}, False, 400)

        # Use Comment as a lightweight visit log (no custom doctype needed)
        comment = frappe.get_doc({
            "doctype": "Comment",
            "comment_type": "Info",
            "reference_doctype": "Customer",
            "reference_name": customer,
            "content": json.dumps({
                "type": "customer_visit",
                "sales_person": sales_person,
                "visit_date": nowdate(),
                "visit_time": frappe.utils.now_datetime().strftime("%H:%M:%S"),
                "latitude": params.get("latitude"),
                "longitude": params.get("longitude"),
                "remarks": params.get("remarks", ""),
            }),
            "comment_email": frappe.session.user,
        })
        comment.insert(ignore_permissions=True)
        frappe.db.commit()

        return response(
            "Customer visit recorded",
            {
                "customer": customer,
                "sales_person": sales_person,
                "visit_date": nowdate(),
            },
            True, 200,
        )
    except Exception as e:
        frappe.log_error(title="Mark Customer Visit Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_customer_visits(customer=None, from_date=None, to_date=None):
    """Get customer visit history for the logged-in salesperson."""
    try:
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        if not sales_person:
            return response("No Sales Person linked", {}, False, 400)

        today = nowdate()
        start = from_date or today
        end = to_date or today

        visits = frappe.db.sql("""
            SELECT c.reference_name as customer, c.content, c.creation
            FROM `tabComment` c
            WHERE c.comment_type = 'Info'
            AND c.reference_doctype = 'Customer'
            AND c.comment_email = %s
            AND c.content LIKE '%%customer_visit%%'
            AND DATE(c.creation) BETWEEN %s AND %s
            ORDER BY c.creation DESC
        """, (frappe.session.user, start, end), as_dict=True)

        result = []
        for v in visits:
            try:
                data = json.loads(v.content)
                data["customer"] = v.reference_name
                data["customer_name"] = frappe.db.get_value("Customer", v.reference_name, "customer_name")
                data["creation"] = str(v.creation)
                result.append(data)
            except Exception:
                pass

        # Get visited customer names for this date range
        visited_customers = list(set([r["customer"] for r in result]))

        return response(
            "Customer visits fetched",
            {
                "visits": result,
                "visited_count": len(visited_customers),
                "visited_customers": visited_customers,
            },
            True, 200,
        )
    except Exception as e:
        frappe.log_error(title="Get Customer Visits Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_customer_list_v2(name=None, mobile_no=None, territory=None, route=None,
                         visit_status=None, page_number=1, page_size=20,
                         limit_start=None, limit_page_length=None):
    """
    Enhanced customer list with route/territory, visit status, outstanding balance.

    Args:
        name: Search by name/ID
        mobile_no: Search by mobile
        territory: Filter by territory (route)
        route: Alias for territory
        visit_status: "visited" | "not_visited" | None (all)
        page_number: Page number (default: 1)
        page_size: Records per page (default: 20)
        limit_start / limit_page_length: Legacy pagination (overrides page_number/page_size if provided)
    """
    try:
        # Support both pagination styles
        _page_size = int(limit_page_length or page_size or 20)
        if limit_start is not None:
            _offset = int(limit_start)
        else:
            _offset = (int(page_number or 1) - 1) * _page_size

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        override_enabled = should_override_sales_team()

        filters = {}
        or_filters = None

        if name:
            or_filters = [
                ["customer_name", "like", f"%{name}%"],
                ["name", "like", f"%{name}%"],
            ]
        if mobile_no:
            filters["mobile_no"] = ["like", f"%{mobile_no}%"]

        route_filter = route or territory
        if route_filter:
            filters["territory"] = route_filter

        fields = [
            "name", "customer_name", "mobile_no", "territory",
            "default_price_list", "customer_group",
            "custom_latitude", "custom_longitude",
        ]

        # Add custom fields if they exist
        for f in ["custom_route", "custom_visit_sequence"]:
            if frappe.db.has_column("Customer", f):
                fields.append(f)

        customers = frappe.get_all(
            "Customer",
            filters=filters,
            or_filters=or_filters,
            fields=fields,
            ignore_permissions=override_enabled,
            limit_start=_offset,
            limit_page_length=_page_size,
            order_by="customer_name asc",
        )

        # Get today's visited customers
        today = nowdate()
        visited_set = set()
        visit_rows = frappe.db.sql("""
            SELECT DISTINCT reference_name
            FROM `tabComment`
            WHERE comment_type = 'Info'
            AND reference_doctype = 'Customer'
            AND comment_email = %s
            AND content LIKE '%%customer_visit%%'
            AND DATE(creation) = %s
        """, (frappe.session.user, today))
        for row in visit_rows:
            visited_set.add(row[0])

        result = []
        for c in customers:
            c["visit_status"] = "visited" if c["name"] in visited_set else "not_visited"
            # Outstanding balance
            c["outstanding_balance"] = frappe.db.sql("""
                SELECT COALESCE(SUM(outstanding_amount), 0)
                FROM `tabSales Invoice`
                WHERE customer = %s AND docstatus = 1 AND outstanding_amount > 0
            """, c["name"])[0][0] or 0

            # Apply visit_status filter
            if visit_status:
                if visit_status == "visited" and c["visit_status"] != "visited":
                    continue
                if visit_status == "not_visited" and c["visit_status"] != "not_visited":
                    continue
            result.append(c)

        total_count = frappe.db.count("Customer", filters=filters or {})
        return response(
            "Customer list fetched",
            {
                "customers": result,
                "total_count": total_count,
                "page_number": int(page_number or 1),
                "page_size": _page_size,
            },
            True, 200,
        )
    except Exception as e:
        frappe.log_error(title="Get Customer List V2 Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_customer_balance_summary(customer=None):
    """
    Get customer balance summary: old balance, current balance, total.
    """
    try:
        if not customer:
            return response("Customer is required", {}, False, 400)

        # Total outstanding
        total_outstanding = frappe.db.sql("""
            SELECT COALESCE(SUM(outstanding_amount), 0)
            FROM `tabSales Invoice`
            WHERE customer = %s AND docstatus = 1
        """, customer)[0][0] or 0

        # Current period (today's invoices outstanding)
        today = nowdate()
        current_balance = frappe.db.sql("""
            SELECT COALESCE(SUM(outstanding_amount), 0)
            FROM `tabSales Invoice`
            WHERE customer = %s AND docstatus = 1 AND posting_date = %s
        """, (customer, today))[0][0] or 0

        old_balance = flt(total_outstanding) - flt(current_balance)

        # Credit limit
        credit_limit = frappe.db.get_value("Customer Credit Limit",
            {"parent": customer, "parenttype": "Customer"},
            "credit_limit") or 0

        return response(
            "Customer balance summary",
            {
                "customer": customer,
                "old_balance": old_balance,
                "current_balance": current_balance,
                "total_balance": total_outstanding,
                "credit_limit": credit_limit,
            },
            True, 200,
        )
    except Exception as e:
        frappe.log_error(title="Customer Balance Summary Error", message=str(e))
        return response(str(e), {}, False, 500)


# ==================== ITEM ENHANCED APIs ====================

@frappe.whitelist(methods="GET")
def get_item_stock(item_code=None, warehouse=None):
    """
    Get stock qty for an item across warehouses (or specific warehouse).

    Returns actual_qty, reserved_qty, projected_qty per warehouse.
    """
    try:
        if not item_code:
            return response("item_code is required", {}, False, 400)

        filters = {"item_code": item_code}
        if warehouse:
            filters["warehouse"] = warehouse

        bins = frappe.get_all(
            "Bin",
            filters=filters,
            fields=["warehouse", "actual_qty", "reserved_qty", "projected_qty", "ordered_qty"],
        )

        return response(
            "Item stock fetched",
            {"item_code": item_code, "warehouses": bins},
            True, 200,
        )
    except Exception as e:
        frappe.log_error(title="Get Item Stock Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_item_pricing_details(item_code=None, customer=None, uom=None):
    """
    Get comprehensive pricing for an item:
    - MRP (standard_rate from Item master)
    - Last Sales Price (from last Sales Invoice)
    - Price List price (from customer's effective price list)
    - All available UOMs with conversion factors
    """
    try:
        if not item_code:
            return response("item_code is required", {}, False, 400)

        item = frappe.get_doc("Item", item_code)
        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)

        # MRP
        mrp = item.standard_rate or 0

        # Last Sales Price for this item
        last_price_row = frappe.db.sql("""
            SELECT sii.rate, sii.uom, si.posting_date, si.customer
            FROM `tabSales Invoice Item` sii
            JOIN `tabSales Invoice` si ON sii.parent = si.name
            WHERE sii.item_code = %s AND si.docstatus = 1 AND si.is_return = 0
            ORDER BY si.posting_date DESC, si.creation DESC
            LIMIT 1
        """, item_code, as_dict=True)
        last_sales_price = last_price_row[0].rate if last_price_row else 0
        last_sales_info = last_price_row[0] if last_price_row else {}

        # Price List Price (for the target UOM or default)
        target_uom = uom or item.stock_uom
        price_list_rate = 0
        price_row = frappe.db.sql("""
            SELECT price_list_rate, uom
            FROM `tabItem Price`
            WHERE item_code = %s AND price_list = %s AND uom = %s
            ORDER BY creation DESC LIMIT 1
        """, (item_code, effective_price_list, target_uom), as_dict=True)
        if price_row:
            price_list_rate = price_row[0].price_list_rate

        # If no UOM-specific price, try without UOM filter
        if not price_list_rate:
            price_row2 = frappe.db.sql("""
                SELECT price_list_rate, uom
                FROM `tabItem Price`
                WHERE item_code = %s AND price_list = %s
                ORDER BY creation DESC LIMIT 1
            """, (item_code, effective_price_list), as_dict=True)
            if price_row2:
                price_list_rate = price_row2[0].price_list_rate

        # UOM details with conversion factors
        uoms = []
        for uom_row in item.uoms or []:
            uoms.append({
                "uom": uom_row.uom,
                "conversion_factor": uom_row.conversion_factor,
            })
        # Ensure stock UOM is included
        if not any(u["uom"] == item.stock_uom for u in uoms):
            uoms.insert(0, {"uom": item.stock_uom, "conversion_factor": 1})

        # Tax rate
        tax_rate = get_item_tax_rate(item_code) if 'get_item_tax_rate' in dir() else 0

        return response(
            "Item pricing details",
            {
                "item_code": item_code,
                "item_name": item.item_name,
                "stock_uom": item.stock_uom,
                "mrp": mrp,
                "last_sales_price": last_sales_price,
                "last_sales_info": {
                    "rate": last_sales_info.get("rate", 0),
                    "uom": last_sales_info.get("uom", ""),
                    "date": str(last_sales_info.get("posting_date", "")),
                    "customer": last_sales_info.get("customer", ""),
                } if last_sales_info else {},
                "price_list": effective_price_list,
                "price_list_rate": price_list_rate,
                "uoms": uoms,
                "tax_rate": tax_rate,
            },
            True, 200,
        )
    except Exception as e:
        frappe.log_error(title="Get Item Pricing Details Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_item_uom_details(item_code=None):
    """Get all UOMs with conversion factors for an item."""
    try:
        if not item_code:
            return response("item_code is required", {}, False, 400)

        item = frappe.get_doc("Item", item_code)
        uoms = [{"uom": item.stock_uom, "conversion_factor": 1}]
        for u in item.uoms or []:
            if u.uom != item.stock_uom:
                uoms.append({"uom": u.uom, "conversion_factor": u.conversion_factor})

        return response(
            "Item UOM details",
            {"item_code": item_code, "stock_uom": item.stock_uom, "uoms": uoms},
            True, 200,
        )
    except Exception as e:
        return response(str(e), {}, False, 500)


# ==================== DELIVERY NOTE CRUD APIs ====================

@frappe.whitelist(methods="POST")
def create_delivery_note(params):
    """
    Create a Delivery Note in Draft mode.

    Args (via params):
        customer, items[], delivery_date, po_no, remarks,
        custom_payment_type, additional_discount_amount
    """
    try:
        if isinstance(params, str):
            params = json.loads(params)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        customer = params.get("customer")
        settings = get_basket4me_settings()
        sales_person_details = None
        for detail in settings.sales_person_details:
            if detail.sales_person == sales_person:
                sales_person_details = detail

        if not sales_person_details:
            return response(f"No Basket4Me Settings found for sales person {sales_person}", {}, False, 400)

        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)
        items = params.get("items")
        delivery_date = params.get("delivery_date") or nowdate()
        posting_date = params.get("posting_date") or nowdate()
        additional_discount_amount = flt(params.get("additional_discount_amount") or 0)

        if not customer:
            return response("Customer is required", {}, False, 400)
        if not items or not isinstance(items, list):
            return response("At least one item is required", {}, False, 400)

        dn = frappe.new_doc("Delivery Note")
        dn.customer = customer
        dn.customer_name = params.get("customer_name")
        dn.company = sales_person_details.company
        dn.posting_date = posting_date
        dn.set_warehouse = sales_person_details.warehouse
        dn.cost_center = sales_person_details.cost_center
        dn.selling_price_list = effective_price_list
        if params.get("custom_payment_type"):
            dn.custom_payment_type = params.get("custom_payment_type")
        dn.custom_mobile_app = 1

        response_items = []
        for item in items:
            item_code = item.get("item_code")
            qty = item.get("qty", 1)
            uom = item.get("uom")
            description = item.get("description")
            if description:
                description = strip_html_tags(description)
            provided_rate = item.get("rate")
            is_free_item = item.get("is_free_item", False)

            item_price = frappe.db.sql("""
                SELECT price_list_rate FROM `tabItem Price`
                WHERE item_code = %s AND uom = %s AND price_list = %s
                ORDER BY uom DESC, creation DESC LIMIT 1
            """, (item_code, uom, effective_price_list), as_dict=True)
            latest_item_price = item_price[0]["price_list_rate"] if item_price else None

            if is_free_item:
                rate = 0
            elif latest_item_price is not None:
                rate = latest_item_price
            else:
                rate = provided_rate

            price_list_rate = flt(rate)
            disc_pct = flt(item.get("discount_percentage", 0))
            disc_amt = flt(item.get("discount_amount", 0))
            if disc_pct and price_list_rate:
                discount_percentage = disc_pct
                discount_amount = (price_list_rate * disc_pct) / 100
            elif disc_amt and price_list_rate:
                discount_amount = disc_amt
                discount_percentage = (disc_amt / price_list_rate) * 100
            else:
                discount_percentage = 0
                discount_amount = 0
            discounted_rate = price_list_rate - discount_amount

            item_data = {
                "item_code": item_code, "qty": qty, "uom": uom,
                "description": description,
                "warehouse": sales_person_details.warehouse,
                "cost_center": sales_person_details.cost_center,
                "discount_percentage": discount_percentage,
                "discount_amount": discount_amount,
                "rate": discounted_rate,
                "price_list_rate": price_list_rate,
                "is_free_item": is_free_item,
            }
            # SO linkage if provided
            if item.get("against_sales_order"):
                item_data["against_sales_order"] = item.get("against_sales_order")
            if item.get("so_detail"):
                item_data["so_detail"] = item.get("so_detail")
            batch_no = item.get("batch_no")
            if batch_no:
                item_data["use_serial_batch_fields"] = 1
                item_data["batch_no"] = batch_no

            dn.append("items", item_data)
            response_items.append({
                "item_code": item_code, "qty": qty, "uom": uom,
                "rate": discounted_rate, "price_list_rate": price_list_rate,
                "discount_percentage": discount_percentage, "discount_amount": discount_amount,
                "is_free_item": is_free_item,
            })

        frappe.flags.ignore_permissions = True
        dn.run_method("set_missing_values")
        frappe.flags.ignore_permissions = False

        if additional_discount_amount:
            dn.apply_discount_on = "Net Total"
            dn.discount_amount = additional_discount_amount

        dn.run_method("calculate_taxes_and_totals")

        if params.get("po_no"):
            dn.po_no = params.get("po_no")
        if params.get("remarks"):
            dn.remarks = params.get("remarks")

        if hasattr(dn, 'sales_team'):
            dn.append("sales_team", {"sales_person": sales_person, "allocated_percentage": 100})

        dn.insert(ignore_permissions=True)
        frappe.db.commit()

        return response(
            "Delivery Note created successfully",
            {
                "name": dn.name, "docstatus": dn.docstatus, "status": dn.status,
                "customer": dn.customer, "customer_name": dn.customer_name,
                "posting_date": str(dn.posting_date),
                "total": dn.total, "grand_total": dn.grand_total,
                "items": response_items,
            },
            True, 200,
        )
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Create Delivery Note Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def update_delivery_note(params):
    """Update a Draft Delivery Note (replaces items)."""
    try:
        if isinstance(params, str):
            params = json.loads(params)

        dn_name = params.get("name")
        if not dn_name:
            return response("Delivery Note name is required", {}, False, 400)
        if not frappe.db.exists("Delivery Note", dn_name):
            return response(f"Delivery Note {dn_name} not found", {}, False, 404)

        dn = frappe.get_doc("Delivery Note", dn_name)
        if dn.docstatus != 0:
            return response("Can only update Delivery Note in Draft state", {}, False, 400)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        settings = get_basket4me_settings()
        sales_person_details = None
        for detail in settings.sales_person_details:
            if detail.sales_person == sales_person:
                sales_person_details = detail
        if not sales_person_details:
            return response(f"No Basket4Me Settings found for sales person {sales_person}", {}, False, 400)

        customer = params.get("customer") or dn.customer
        effective_price_list = get_effective_price_list(customer=customer, sales_person=sales_person)
        items = params.get("items")

        if params.get("customer"):
            dn.customer = params["customer"]
        if params.get("customer_name"):
            dn.customer_name = params["customer_name"]
        if params.get("po_no"):
            dn.po_no = params["po_no"]
        if params.get("remarks"):
            dn.remarks = params["remarks"]

        additional_discount_amount = flt(params.get("additional_discount_amount") or 0)

        if items and isinstance(items, list):
            dn.items = []
            dn.selling_price_list = effective_price_list
            for item in items:
                item_code = item.get("item_code")
                qty = item.get("qty", 1)
                uom = item.get("uom")
                provided_rate = item.get("rate")
                is_free_item = item.get("is_free_item", False)

                item_price = frappe.db.sql("""
                    SELECT price_list_rate FROM `tabItem Price`
                    WHERE item_code = %s AND uom = %s AND price_list = %s
                    ORDER BY uom DESC, creation DESC LIMIT 1
                """, (item_code, uom, effective_price_list), as_dict=True)
                latest = item_price[0]["price_list_rate"] if item_price else None
                if is_free_item:
                    rate = 0
                elif latest is not None:
                    rate = latest
                else:
                    rate = provided_rate
                price_list_rate = flt(rate)
                disc_pct = flt(item.get("discount_percentage", 0))
                disc_amt = flt(item.get("discount_amount", 0))
                if disc_pct and price_list_rate:
                    discount_percentage = disc_pct
                    discount_amount = (price_list_rate * disc_pct) / 100
                elif disc_amt and price_list_rate:
                    discount_amount = disc_amt
                    discount_percentage = (disc_amt / price_list_rate) * 100
                else:
                    discount_percentage = 0
                    discount_amount = 0
                discounted_rate = price_list_rate - discount_amount

                item_data = {
                    "item_code": item_code, "qty": qty, "uom": uom,
                    "warehouse": sales_person_details.warehouse,
                    "cost_center": sales_person_details.cost_center,
                    "discount_percentage": discount_percentage,
                    "discount_amount": discount_amount,
                    "rate": discounted_rate,
                    "price_list_rate": price_list_rate,
                    "is_free_item": is_free_item,
                }
                if item.get("against_sales_order"):
                    item_data["against_sales_order"] = item["against_sales_order"]
                if item.get("so_detail"):
                    item_data["so_detail"] = item["so_detail"]
                batch_no = item.get("batch_no")
                if batch_no:
                    item_data["use_serial_batch_fields"] = 1
                    item_data["batch_no"] = batch_no
                dn.append("items", item_data)

        frappe.flags.ignore_permissions = True
        dn.run_method("set_missing_values")
        frappe.flags.ignore_permissions = False
        if additional_discount_amount:
            dn.apply_discount_on = "Net Total"
            dn.discount_amount = additional_discount_amount
        dn.run_method("calculate_taxes_and_totals")
        dn.save(ignore_permissions=True)
        frappe.db.commit()

        return response(
            "Delivery Note updated successfully",
            {
                "name": dn.name, "docstatus": dn.docstatus,
                "customer": dn.customer, "total": dn.total, "grand_total": dn.grand_total,
                "items": [{"item_code": i.item_code, "item_name": i.item_name, "qty": i.qty, "rate": i.rate, "amount": i.amount} for i in dn.items],
            },
            True, 200,
        )
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Update Delivery Note Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def submit_delivery_note(params):
    """Submit a Draft Delivery Note."""
    try:
        if isinstance(params, str):
            params = json.loads(params)
        dn_name = params.get("name")
        if not dn_name:
            return response("Delivery Note name is required", {}, False, 400)
        if not frappe.db.exists("Delivery Note", dn_name):
            return response(f"Delivery Note {dn_name} not found", {}, False, 404)
        dn = frappe.get_doc("Delivery Note", dn_name)
        if dn.docstatus != 0:
            return response("Delivery Note is not in Draft state", {}, False, 400)
        dn.submit()
        frappe.db.commit()
        return response("Delivery Note submitted", {"name": dn.name, "docstatus": dn.docstatus, "status": dn.status, "grand_total": dn.grand_total}, True, 200)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Submit Delivery Note Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def cancel_delivery_note(params):
    """Cancel a submitted Delivery Note."""
    try:
        if isinstance(params, str):
            params = json.loads(params)
        dn_name = params.get("name")
        if not dn_name:
            return response("Delivery Note name is required", {}, False, 400)
        if not frappe.db.exists("Delivery Note", dn_name):
            return response(f"Delivery Note {dn_name} not found", {}, False, 404)
        dn = frappe.get_doc("Delivery Note", dn_name)
        if dn.docstatus != 1:
            return response("Only submitted Delivery Notes can be cancelled", {}, False, 400)
        dn.cancel()
        frappe.db.commit()
        return response("Delivery Note cancelled", {"name": dn.name, "docstatus": dn.docstatus, "status": dn.status}, True, 200)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Cancel Delivery Note Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def delete_delivery_note(params):
    """Delete a Draft or Cancelled Delivery Note."""
    try:
        if isinstance(params, str):
            params = json.loads(params)
        dn_name = params.get("name")
        if not dn_name:
            return response("Delivery Note name is required", {}, False, 400)
        if not frappe.db.exists("Delivery Note", dn_name):
            return response(f"Delivery Note {dn_name} not found", {}, False, 404)
        dn = frappe.get_doc("Delivery Note", dn_name)
        if dn.docstatus == 1:
            return response("Cannot delete submitted Delivery Note. Cancel first.", {}, False, 400)
        frappe.delete_doc("Delivery Note", dn_name, ignore_permissions=True)
        frappe.db.commit()
        return response(f"Delivery Note {dn_name} deleted", {}, True, 200)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Delete Delivery Note Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_delivery_note_list(name=None, customer=None, status=None, search=None,
                           from_date=None, to_date=None, limit_start=0, limit_page_length=20):
    """List Delivery Notes with filters."""
    try:
        filters = {}
        if name:
            filters["name"] = name
        if customer:
            filters["customer"] = customer
        if status:
            if status == "Draft":
                filters["docstatus"] = 0
            elif status == "Cancelled":
                filters["docstatus"] = 2
            else:
                filters["docstatus"] = 1
                if status != "Submitted":
                    filters["status"] = status
        if from_date and to_date:
            filters["posting_date"] = ["between", [from_date, to_date]]
        elif from_date:
            filters["posting_date"] = [">=", from_date]
        elif to_date:
            filters["posting_date"] = ["<=", to_date]

        or_filters = None
        if search:
            or_filters = [["name", "like", f"%{search}%"], ["customer_name", "like", f"%{search}%"]]

        dns = frappe.get_all(
            "Delivery Note", filters=filters, or_filters=or_filters,
            fields=["name", "customer", "customer_name", "posting_date", "docstatus",
                     "status", "total", "grand_total", "currency", "per_billed"],
            order_by="creation desc",
            limit_start=int(limit_start), limit_page_length=int(limit_page_length),
        )
        total_count = frappe.db.count("Delivery Note", filters=filters)

        # Summaries
        total_value = sum(d["grand_total"] or 0 for d in dns)
        total_customers = len(set(d["customer"] for d in dns))

        for d in dns:
            d["items"] = frappe.get_all("Delivery Note Item", filters={"parent": d["name"]},
                fields=["item_code", "item_name", "qty", "uom", "rate", "amount"])

        return response("Delivery Notes fetched", {
            "delivery_notes": dns, "total_count": total_count,
            "total_value": total_value, "total_customers": total_customers,
        }, True, 200)
    except Exception as e:
        frappe.log_error(title="Get Delivery Note List Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_delivery_note_detail(name=None):
    """Get single Delivery Note with full details."""
    try:
        if not name:
            return response("Delivery Note name is required", {}, False, 400)
        if not frappe.db.exists("Delivery Note", name):
            return response(f"Delivery Note {name} not found", {}, False, 404)

        dn = frappe.get_doc("Delivery Note", name)
        return response("Delivery Note fetched", {
            "name": dn.name, "customer": dn.customer, "customer_name": dn.customer_name,
            "posting_date": str(dn.posting_date), "docstatus": dn.docstatus,
            "status": dn.status, "company": dn.company, "currency": dn.currency,
            "selling_price_list": dn.selling_price_list,
            "total": dn.total, "net_total": dn.net_total,
            "grand_total": dn.grand_total, "per_billed": dn.per_billed,
            "items": [{
                "name": i.name, "item_code": i.item_code, "item_name": i.item_name,
                "qty": i.qty, "uom": i.uom, "rate": i.rate,
                "price_list_rate": i.price_list_rate,
                "discount_percentage": i.discount_percentage,
                "discount_amount": i.discount_amount,
                "amount": i.amount, "warehouse": i.warehouse,
                "against_sales_order": i.against_sales_order,
                "so_detail": i.so_detail,
            } for i in dn.items],
            "taxes": [{
                "charge_type": t.charge_type, "account_head": t.account_head,
                "description": t.description, "rate": t.rate,
                "tax_amount": t.tax_amount, "total": t.total,
                "included_in_print_rate": t.included_in_print_rate,
            } for t in dn.taxes] if dn.taxes else [],
        }, True, 200)
    except Exception as e:
        frappe.log_error(title="Get Delivery Note Detail Error", message=str(e))
        return response(str(e), {}, False, 500)


# ==================== CONVERSION APIs ====================

@frappe.whitelist(methods="GET")
def get_pending_so_for_dn(customer=None):
    """
    Get submitted Sales Orders that are pending delivery for a customer.
    Returns SOs where per_delivered < 100.
    """
    try:
        if not customer:
            return response("Customer is required", {}, False, 400)

        sos = frappe.get_all(
            "Sales Order",
            filters={"customer": customer, "docstatus": 1, "per_delivered": ["<", 100], "status": ["not in", ["Cancelled", "Closed"]]},
            fields=["name", "customer", "customer_name", "transaction_date", "delivery_date",
                     "status", "total", "grand_total", "per_delivered", "per_billed"],
            order_by="transaction_date desc",
        )

        for so in sos:
            so["items"] = frappe.get_all(
                "Sales Order Item",
                filters={"parent": so["name"]},
                fields=["name", "item_code", "item_name", "qty", "delivered_qty",
                         "uom", "rate", "amount", "warehouse"],
            )
            # Calculate pending qty
            for item in so["items"]:
                item["pending_qty"] = flt(item["qty"]) - flt(item["delivered_qty"])

        return response("Pending Sales Orders for delivery", {"sales_orders": sos}, True, 200)
    except Exception as e:
        frappe.log_error(title="Get Pending SO for DN Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def create_dn_from_so(params):
    """
    Create Delivery Note from one or more Sales Orders.
    Draft SOs will be submitted first.

    Args (via params):
        sales_orders: list of SO names (required)
    """
    try:
        if isinstance(params, str):
            params = json.loads(params)

        sales_orders = params.get("sales_orders")
        if not sales_orders or not isinstance(sales_orders, list):
            return response("sales_orders list is required", {}, False, 400)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        settings = get_basket4me_settings()
        sp_detail = None
        for d in settings.sales_person_details:
            if d.sales_person == sales_person:
                sp_detail = d
                break
        if not sp_detail:
            return response(f"No settings for sales person {sales_person}", {}, False, 400)

        first_so = frappe.get_doc("Sales Order", sales_orders[0])

        # Validate & submit drafts
        for so_name in sales_orders:
            so = frappe.get_doc("Sales Order", so_name)
            if so.customer != first_so.customer:
                return response(f"All SOs must belong to same customer. {so_name} differs.", {}, False, 400)
            if so.per_delivered >= 100:
                return response(f"SO {so_name} is already fully delivered", {}, False, 400)
            if so.docstatus == 0:
                so.submit()

        dn = frappe.new_doc("Delivery Note")
        dn.customer = first_so.customer
        dn.company = sp_detail.company
        dn.posting_date = nowdate()
        dn.set_warehouse = sp_detail.warehouse
        dn.cost_center = sp_detail.cost_center
        dn.selling_price_list = first_so.selling_price_list
        dn.custom_mobile_app = 1

        for tax in first_so.taxes or []:
            dn.append("taxes", {
                "charge_type": tax.charge_type, "account_head": tax.account_head,
                "description": tax.description, "rate": tax.rate,
                "cost_center": tax.cost_center,
                "included_in_print_rate": tax.included_in_print_rate,
                "tax_amount": tax.tax_amount if tax.charge_type == "Actual" else 0,
            })

        for so_name in sales_orders:
            so = frappe.get_doc("Sales Order", so_name)
            for item in so.items:
                pending = flt(item.qty) - flt(item.delivered_qty)
                if pending <= 0:
                    continue
                dn.append("items", {
                    "item_code": item.item_code, "item_name": item.item_name,
                    "description": item.description, "qty": pending,
                    "uom": item.uom, "rate": item.rate,
                    "price_list_rate": item.price_list_rate,
                    "discount_percentage": item.discount_percentage,
                    "discount_amount": item.discount_amount,
                    "warehouse": item.warehouse or sp_detail.warehouse,
                    "cost_center": sp_detail.cost_center,
                    "against_sales_order": so.name,
                    "so_detail": item.name,
                })

        if hasattr(dn, 'sales_team'):
            dn.append("sales_team", {"sales_person": sales_person, "allocated_percentage": 100})

        dn.insert(ignore_permissions=True)
        frappe.db.commit()

        return response("Delivery Note created from SO(s)", {
            "name": dn.name, "sales_orders_linked": sales_orders,
            "customer": dn.customer, "grand_total": dn.grand_total,
            "items": [{"item_code": i.item_code, "qty": i.qty, "rate": i.rate, "amount": i.amount, "against_sales_order": i.against_sales_order} for i in dn.items],
        }, True, 200)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Create DN from SO Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_uninvoiced_dn(customer=None):
    """
    Get submitted Delivery Notes that are not yet invoiced (per_billed < 100).
    """
    try:
        if not customer:
            return response("Customer is required", {}, False, 400)

        dns = frappe.get_all(
            "Delivery Note",
            filters={"customer": customer, "docstatus": 1, "per_billed": ["<", 100], "status": ["not in", ["Cancelled", "Closed"]]},
            fields=["name", "customer", "customer_name", "posting_date", "status",
                     "total", "grand_total", "per_billed"],
            order_by="posting_date desc",
        )

        for d in dns:
            d["items"] = frappe.get_all(
                "Delivery Note Item",
                filters={"parent": d["name"]},
                fields=["name", "item_code", "item_name", "qty", "billed_amt",
                         "uom", "rate", "amount", "against_sales_order", "so_detail"],
            )

        return response("Uninvoiced Delivery Notes", {"delivery_notes": dns}, True, 200)
    except Exception as e:
        frappe.log_error(title="Get Uninvoiced DN Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="POST")
def create_si_from_dn(params):
    """
    Create Sales Invoice from one or more submitted Delivery Notes.

    Args (via params):
        delivery_notes: list of DN names (required)
        payments: list of {"mode_of_payment", "amount"} (optional)
    """
    try:
        if isinstance(params, str):
            params = json.loads(params)

        delivery_notes = params.get("delivery_notes")
        payments = params.get("payments")
        if not delivery_notes or not isinstance(delivery_notes, list):
            return response("delivery_notes list is required", {}, False, 400)

        sales_person = frappe.db.get_value("Sales Person", {"custom_user": frappe.session.user}, "name")
        settings = get_basket4me_settings()
        sp_detail = None
        for d in settings.sales_person_details:
            if d.sales_person == sales_person:
                sp_detail = d
                break
        if not sp_detail:
            return response(f"No settings for sales person {sales_person}", {}, False, 400)

        first_dn = frappe.get_doc("Delivery Note", delivery_notes[0])

        for dn_name in delivery_notes:
            dn = frappe.get_doc("Delivery Note", dn_name)
            if dn.docstatus != 1:
                return response(f"DN {dn_name} is not submitted", {}, False, 400)
            if dn.customer != first_dn.customer:
                return response(f"All DNs must belong to same customer", {}, False, 400)
            if dn.per_billed >= 100:
                return response(f"DN {dn_name} is already fully billed", {}, False, 400)

        si = frappe.new_doc("Sales Invoice")
        si.customer = first_dn.customer
        si.company = sp_detail.company
        si.posting_date = nowdate()
        si.due_date = nowdate()
        si.currency = first_dn.currency
        si.selling_price_list = first_dn.selling_price_list
        si.cost_center = sp_detail.cost_center
        si.update_stock = 0  # Stock already reduced by DN
        si.custom_mobile_app = 1

        for tax in first_dn.taxes or []:
            si.append("taxes", {
                "charge_type": tax.charge_type, "account_head": tax.account_head,
                "description": tax.description, "rate": tax.rate,
                "cost_center": tax.cost_center,
                "included_in_print_rate": tax.included_in_print_rate,
                "tax_amount": tax.tax_amount if tax.charge_type == "Actual" else 0,
            })

        for dn_name in delivery_notes:
            dn = frappe.get_doc("Delivery Note", dn_name)
            for item in dn.items:
                si.append("items", {
                    "item_code": item.item_code, "item_name": item.item_name,
                    "description": item.description, "qty": item.qty,
                    "uom": item.uom, "rate": item.rate,
                    "price_list_rate": item.price_list_rate,
                    "discount_percentage": item.discount_percentage,
                    "discount_amount": item.discount_amount,
                    "warehouse": item.warehouse,
                    "cost_center": sp_detail.cost_center,
                    "delivery_note": dn.name,
                    "dn_detail": item.name,
                    "sales_order": item.against_sales_order,
                    "so_detail": item.so_detail,
                })

        if payments and isinstance(payments, list):
            for p in payments:
                si.append("payments", {"mode_of_payment": p.get("mode_of_payment"), "amount": p.get("amount", 0)})

        si.append("sales_team", {"sales_person": sales_person, "allocated_percentage": 100})

        si.insert(ignore_permissions=True)
        si.submit()
        frappe.db.commit()

        return response("Sales Invoice created from DN(s)", {
            "sales_invoice": si.name, "delivery_notes_linked": delivery_notes,
            "customer": si.customer, "grand_total": si.grand_total, "docstatus": si.docstatus,
            "items": [{"item_code": i.item_code, "qty": i.qty, "rate": i.rate, "amount": i.amount, "delivery_note": i.delivery_note} for i in si.items],
        }, True, 200)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Create SI from DN Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_invoices_for_return(customer=None):
    """
    Get submitted Sales Invoices (non-return) for a customer that can be used for return.
    """
    try:
        if not customer:
            return response("Customer is required", {}, False, 400)

        invoices = frappe.get_all(
            "Sales Invoice",
            filters={"customer": customer, "docstatus": 1, "is_return": 0, "status": ["not in", ["Cancelled"]]},
            fields=["name", "customer", "customer_name", "posting_date", "status",
                     "total", "grand_total", "outstanding_amount"],
            order_by="posting_date desc",
        )

        for inv in invoices:
            inv["items"] = frappe.get_all(
                "Sales Invoice Item",
                filters={"parent": inv["name"]},
                fields=["name", "item_code", "item_name", "qty", "uom", "rate", "amount"],
            )
            # Get already returned qty
            for item in inv["items"]:
                returned = frappe.db.sql("""
                    SELECT COALESCE(SUM(ABS(sii.qty)), 0)
                    FROM `tabSales Invoice Item` sii
                    JOIN `tabSales Invoice` si ON sii.parent = si.name
                    WHERE si.is_return = 1 AND si.docstatus = 1
                    AND si.return_against = %s AND sii.item_code = %s
                """, (inv["name"], item["item_code"]))[0][0] or 0
                item["returned_qty"] = returned
                item["returnable_qty"] = flt(item["qty"]) - flt(returned)

        return response("Invoices for return", {"invoices": invoices}, True, 200)
    except Exception as e:
        frappe.log_error(title="Get Invoices For Return Error", message=str(e))
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_territories():
    """Get list of territories (routes) for filtering."""
    try:
        territories = frappe.get_all(
            "Territory",
            filters={"is_group": 0},
            fields=["name", "parent_territory"],
            order_by="name asc",
        )
        return response("Territories fetched", {"territories": territories}, True, 200)
    except Exception as e:
        return response(str(e), {}, False, 500)


@frappe.whitelist(methods="GET")
def get_transaction_history(item_code=None, customer=None, doctype=None,
                            from_date=None, to_date=None, page_number=1, page_size=20):
    """
    Get transaction history (SO/SI/Sales Return) by item, customer, or doctype.

    Query params:
        item_code: Filter by item code
        customer: Filter by customer
        doctype: Filter by type - "Sales Order", "Sales Invoice", "Sales Return" or None (all)
        from_date / to_date: Date range filter
        page_number / page_size: Pagination
    """
    try:
        _page_size = int(page_size or 20)
        _offset = (int(page_number or 1) - 1) * _page_size

        if not item_code and not customer:
            return response("item_code or customer is required", {}, False, 400)

        # Determine which doctypes to query
        doc_types = []
        if doctype == "Sales Order":
            doc_types = [("Sales Order", "Sales Order Item", "transaction_date")]
        elif doctype == "Sales Invoice":
            doc_types = [("Sales Invoice", "Sales Invoice Item", "posting_date")]
        elif doctype == "Sales Return":
            doc_types = [("Sales Invoice", "Sales Invoice Item", "posting_date")]
        else:
            doc_types = [
                ("Sales Order", "Sales Order Item", "transaction_date"),
                ("Sales Invoice", "Sales Invoice Item", "posting_date"),
            ]

        all_transactions = []

        for parent_dt, child_dt, date_field in doc_types:
            conditions = ["p.docstatus != 0"]
            values = []

            # For Sales Return, filter is_return=1; for Sales Invoice, is_return=0
            if parent_dt == "Sales Invoice":
                if doctype == "Sales Return":
                    conditions.append("p.is_return = 1")
                elif doctype == "Sales Invoice":
                    conditions.append("p.is_return = 0")
                # If doctype is None (all), include both regular SI and returns

            if customer:
                conditions.append("p.customer = %s")
                values.append(customer)

            if item_code:
                conditions.append("c.item_code = %s")
                values.append(item_code)

            if from_date:
                conditions.append(f"p.{date_field} >= %s")
                values.append(from_date)

            if to_date:
                conditions.append(f"p.{date_field} <= %s")
                values.append(to_date)

            where_clause = " AND ".join(conditions)

            # Check if columns exist on this doctype
            price_list_field = "p.selling_price_list" if frappe.db.has_column(parent_dt, "selling_price_list") else "'' as selling_price_list"

            # is_return only exists on Sales Invoice, not Sales Order
            if parent_dt == "Sales Invoice":
                transaction_type_expr = "CASE WHEN p.is_return = 1 THEN 'Sales Return' ELSE 'Sales Invoice' END"
            else:
                transaction_type_expr = f"'{parent_dt}'"

            query = f"""
                SELECT
                    p.name as ref_no,
                    '{parent_dt}' as doc_type,
                    {transaction_type_expr} as transaction_type,
                    p.{date_field} as date,
                    p.customer,
                    p.customer_name,
                    p.docstatus,
                    p.status,
                    {price_list_field},
                    c.item_code,
                    c.item_name,
                    c.qty,
                    c.uom,
                    c.stock_uom,
                    c.conversion_factor,
                    c.rate,
                    c.amount,
                    c.price_list_rate,
                    c.discount_percentage,
                    c.discount_amount
                FROM `tab{parent_dt}` p
                INNER JOIN `tab{child_dt}` c ON c.parent = p.name
                WHERE {where_clause}
                ORDER BY p.{date_field} DESC, p.name DESC
            """

            rows = frappe.db.sql(query, values, as_dict=True)
            all_transactions.extend(rows)

        # Sort all combined results by date desc
        all_transactions.sort(key=lambda x: str(x.get("date", "")), reverse=True)

        total_count = len(all_transactions)

        # Apply pagination
        paginated = all_transactions[_offset:_offset + _page_size]

        # Summary
        summary = {
            "total_qty": sum(flt(t.get("qty", 0)) for t in all_transactions),
            "total_amount": sum(flt(t.get("amount", 0)) for t in all_transactions),
            "total_discount": sum(flt(t.get("discount_amount", 0)) for t in all_transactions),
            "so_count": len([t for t in all_transactions if t.get("transaction_type") == "Sales Order"]),
            "si_count": len([t for t in all_transactions if t.get("transaction_type") == "Sales Invoice"]),
            "return_count": len([t for t in all_transactions if t.get("transaction_type") == "Sales Return"]),
        }

        return response("Transaction history fetched", {
            "transactions": paginated,
            "summary": summary,
            "total_count": total_count,
            "page_number": int(page_number or 1),
            "page_size": _page_size,
        }, True, 200)
    except Exception as e:
        frappe.log_error(title="Get Transaction History Error", message=str(e))
        return response(str(e), {}, False, 500)

