// Copyright (c) 2013, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Trial Balance for Party"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "fiscal_year",
			label: __("Fiscal Year"),
			fieldtype: "Link",
			options: "Fiscal Year",
			default: erpnext.utils.get_fiscal_year(frappe.datetime.get_today()),
			reqd: 1,
			on_change: function (query_report) {
				var fiscal_year = query_report.get_values().fiscal_year;
				if (!fiscal_year) {
					return;
				}
				frappe.model.with_doc("Fiscal Year", fiscal_year, function (r) {
					var fy = frappe.model.get_doc("Fiscal Year", fiscal_year);
					frappe.query_report.set_filter_value({
						from_date: fy.year_start_date,
						to_date: fy.year_end_date,
					});
				});
			},
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: erpnext.utils.get_fiscal_year(frappe.datetime.get_today(), true)[1],
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: erpnext.utils.get_fiscal_year(frappe.datetime.get_today(), true)[2],
		},
		{
			fieldname: "party_type",
			label: __("Party Type"),
			fieldtype: "Link",
			options: "Party Type",
			default: "Customer",
			reqd: 1,
		},
		{
			fieldname: "party",
			label: __("Party"),
			fieldtype: "Dynamic Link",
			get_options: function () {
				var party_type = frappe.query_report.get_filter_value("party_type");
				var party = frappe.query_report.get_filter_value("party");
				if (party && !party_type) {
					frappe.throw(__("Please select Party Type first"));
				}
				return party_type;
			},
			on_change: function () {
				var party_type = frappe.query_report.get_filter_value("party_type");
				var party = frappe.query_report.get_filter_value("party");
				var company = frappe.query_report.get_filter_value("company");

				if (party && party_type && ["Customer", "Supplier"].includes(party_type)) {
					frappe.db.get_value(party_type, party, "default_currency", function (value) {
						var currency = value && value["default_currency"];
						if (currency) {
							frappe.query_report.set_filter_value("presentation_currency", currency);
						} else if (company) {
							frappe.db.get_value("Company", company, "default_currency", function (v) {
								frappe.query_report.set_filter_value(
									"presentation_currency",
									v["default_currency"] || ""
								);
							});
						}
					});
				}
			},
		},
		{
			fieldname: "account",
			label: __("Account"),
			fieldtype: "MultiSelectList",
			options: "Account",
			get_data: function (txt) {
				return frappe.db.get_link_options("Account", txt, {
					company: frappe.query_report.get_filter_value("company"),
				});
			},
			on_change: function () {
				var accounts = frappe.query_report.get_filter_value("account");
				if (accounts && accounts.length === 1) {
					frappe.db.get_value("Account", accounts[0], "account_currency", function (value) {
						var currency = value && value["account_currency"];
						if (currency) {
							frappe.query_report.set_filter_value("presentation_currency", currency);
						}
					});
				}
			},
		},
		{
			fieldname: "presentation_currency",
			label: __("Currency"),
			fieldtype: "Select",
			options: erpnext.get_presentation_currency_list(),
		},
		{
			fieldname: "show_zero_values",
			label: __("Show zero values"),
			fieldtype: "Check",
		},
		{
			fieldname: "exclude_zero_balance_parties",
			label: __("Exclude Zero Balance Parties"),
			fieldtype: "Check",
			default: 1,
		},
	],
};
