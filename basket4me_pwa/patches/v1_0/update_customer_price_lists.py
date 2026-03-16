import frappe

def execute():
    """
    Migration script to set up customer-based price lists
    """
    try:
        # Check if Basket4Me Settings exists
        if not frappe.db.exists("Basket4Me Settings", "Basket4Me Settings"):
            frappe.log_error("Basket4Me Settings not found, skipping customer price list migration")
            return
        
        # Enable customer-based pricing if not already set
        basket4me_settings = frappe.get_doc("Basket4Me Settings")
        if not basket4me_settings.get("enable_customer_based_price_list"):
            basket4me_settings.enable_customer_based_price_list = 1
            basket4me_settings.save()
            frappe.log_error("Enabled customer-based price list in Basket4Me Settings")
        
        # Set default price lists for existing customers based on their sales team
        customers = frappe.get_all("Customer", fields=["name"])
        
        updated_customers = 0
        for customer in customers:
            customer_doc = frappe.get_doc("Customer", customer.name)
            
            # Skip if customer already has a default price list
            if customer_doc.get("default_price_list"):
                continue
            
            # Get the first sales person's price list as default
            if customer_doc.sales_team:
                sales_person = customer_doc.sales_team[0].sales_person
                
                # Find this sales person in Basket4Me Settings
                for detail in basket4me_settings.sales_person_details:
                    if detail.sales_person == sales_person:
                        customer_doc.default_price_list = detail.price_list
                        customer_doc.save()
                        updated_customers += 1
                        break
        
        frappe.log_error(f"Updated {updated_customers} customers with default price lists")
        
    except Exception as e:
        frappe.log_error(f"Error in customer price list migration: {str(e)}")
