// Copyright (c) 2024, ZenBiz Technologies Pvt Ltd and contributors
// For license information, please see license.txt

frappe.ui.form.on('Basket4Me Settings', {
    setup(frm) {
        // Sales Person Details child table — filter by the row's own company
        frm.fields_dict['sales_person_details'].grid.get_field('mode_of_payment').get_query = function(doc, cdt, cdn) {
            var row = locals[cdt][cdn];
            return {
                filters: { company: row.company }
            };
        };
        frm.fields_dict['sales_person_details'].grid.get_field('warehouse').get_query = function(doc, cdt, cdn) {
            var row = locals[cdt][cdn];
            return {
                filters: { is_group: 0, company: row.company }
            };
        };
        frm.fields_dict['sales_person_details'].grid.get_field('cost_center').get_query = function(doc, cdt, cdn) {
            var row = locals[cdt][cdn];
            return {
                filters: { company: row.company }
            };
        };
        frm.fields_dict['sales_person_details'].grid.get_field('deduction_account').get_query = function(doc, cdt, cdn) {
            var row = locals[cdt][cdn];
            return {
                filters: { company: row.company }
            };
        };

        // Mode of Payment Details — filter MOP by row's company
        frm.fields_dict['mode_of_payment_details'].grid.get_field('mode_of_payment').get_query = function(doc, cdt, cdn) {
            var row = locals[cdt][cdn];
            return {
                filters: { company: row.company }
            };
        };

        // Top-level fields — no company filter
        frm.set_query("default_target_warehouse", function () {
            return {
                filters: { is_group: 0 }
            };
        });
    },
});
