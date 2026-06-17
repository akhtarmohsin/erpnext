# Copyright (c) 2013, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt, today

from erpnext.accounts.report.general_ledger.general_ledger import get_accounts_with_children
from erpnext.accounts.report.trial_balance.trial_balance import validate_filters
from erpnext.accounts.report.utils import convert, get_currency
from erpnext.accounts.utils import get_account_currency


def execute(filters=None):
	validate_filters(filters)

	filters = set_account_currency(filters)

	show_party_name = is_party_name_visible(filters)

	columns = get_columns(filters, show_party_name)
	data = get_data(filters, show_party_name)

	return columns, data


def set_account_currency(filters):
	"""
	Detect presentation currency from selected account or party, mirroring General Ledger logic.
	Sets filters['company_currency'], filters['account_currency'], and
	filters['presentation_currency'].
	"""
	filters["company_currency"] = frappe.get_cached_value("Company", filters.company, "default_currency")
	account_currency = None

	if filters.get("account"):
		accounts = filters.get("account")
		if isinstance(accounts, str):
			import json

			try:
				accounts = json.loads(accounts)
			except Exception:
				accounts = [accounts]

		if len(accounts) == 1:
			account_currency = get_account_currency(accounts[0])
		else:
			currency = get_account_currency(accounts[0])
			is_same = all(get_account_currency(a) == currency for a in accounts)
			if is_same:
				account_currency = currency

	elif filters.get("party") and filters.get("party_type"):
		party = filters.get("party")
		party_type = filters.get("party_type")

		gle_currency = frappe.db.get_value(
			"GL Entry",
			{"party_type": party_type, "party": party, "company": filters.company},
			"account_currency",
		)

		if gle_currency:
			account_currency = gle_currency
		elif party_type not in ("Employee", "Shareholder", "Member"):
			account_currency = frappe.get_cached_value(party_type, party, "default_currency")

	filters["account_currency"] = account_currency or filters["company_currency"]

	if not filters.get("presentation_currency"):
		if filters["account_currency"] != filters["company_currency"]:
			filters["presentation_currency"] = filters["account_currency"]
		else:
			filters["presentation_currency"] = filters["company_currency"]

	return filters


def get_data(filters, show_party_name):
	if filters.get("party_type") in ("Customer", "Supplier", "Employee", "Member"):
		party_name_field = "{}_name".format(frappe.scrub(filters.get("party_type")))
	elif filters.get("party_type") == "Shareholder":
		party_name_field = "title"
	else:
		party_name_field = "name"

	party_filters = {"name": filters.get("party")} if filters.get("party") else {}
	parties = frappe.get_all(
		filters.get("party_type"),
		fields=["name", party_name_field],
		filters=party_filters,
		order_by="name",
	)

	account_filter = []
	if filters.get("account"):
		account_filter = get_accounts_with_children(filters.get("account"))

	# Build currency_map exactly as General Ledger does via get_currency()
	currency_map = get_currency(filters)
	presentation_currency = currency_map["presentation_currency"]
	company_currency = currency_map["company_currency"]
	# Cap report_date at today so exchange rate API is not called with a future date.
	# GL avoids this because its date ranges are typically in the past.
	report_date = min(str(currency_map["report_date"]), today())

	opening_balances = get_opening_balances(filters, account_filter)
	balances_within_period = get_balances_within_period(filters, account_filter)

	data = []
	total_row = frappe._dict(
		{
			"opening_debit": 0,
			"opening_credit": 0,
			"debit": 0,
			"credit": 0,
			"closing_debit": 0,
			"closing_credit": 0,
		}
	)

	for party in parties:
		row = {"party": party.name}
		if show_party_name:
			row["party_name"] = party.get(party_name_field)

		opening = opening_balances.get(party.name, {})
		period = balances_within_period.get(party.name, {})

		opening_debit, opening_credit = _resolve_amounts(
			opening, presentation_currency, company_currency, report_date
		)
		opening_debit, opening_credit = toggle_debit_credit(opening_debit, opening_credit)
		row.update({"opening_debit": opening_debit, "opening_credit": opening_credit})

		debit, credit = _resolve_amounts(period, presentation_currency, company_currency, report_date)
		row.update({"debit": debit, "credit": credit})

		closing_debit, closing_credit = toggle_debit_credit(opening_debit + debit, opening_credit + credit)
		row.update({"closing_debit": closing_debit, "closing_credit": closing_credit})
		row.update({"currency": presentation_currency})

		has_value = bool(
			opening_debit or opening_credit or debit or credit or closing_debit or closing_credit
		)

		if filters.get("exclude_zero_balance_parties") and not closing_debit and not closing_credit:
			continue

		if cint(filters.show_zero_values) or has_value:
			data.append(row)
			for col in total_row:
				total_row[col] += row.get(col, 0)

	total_row.update({"party": "'" + _("Totals") + "'", "currency": presentation_currency})
	data.append(total_row)

	return data


def _resolve_amounts(balance_dict, presentation_currency, company_currency, report_date):
	"""
	Return (debit, credit) converted to presentation_currency.

	Mirrors convert_to_presentation_currency() in report/utils.py:

	Case 1 — no conversion needed (presentation == company currency):
	  Use debit/credit (company-currency fields) directly.

	Case 2 — party's account currency == presentation currency:
	  Use debit_in_account_currency / credit_in_account_currency directly.
	  The amounts are already in the target currency — no exchange rate lookup needed.

	Case 3 — party's account currency differs from presentation currency:
	  Call convert() inline per party (same as GL's convert_to_presentation_currency
	  does per entry), which fetches the rate fresh via get_rate_as_at each time,
	  avoiding stale cached fallback values.
	"""
	debit = flt(balance_dict.get("debit", 0))
	credit = flt(balance_dict.get("credit", 0))
	debit_acc = flt(balance_dict.get("debit_acc", 0))
	credit_acc = flt(balance_dict.get("credit_acc", 0))
	account_currency = balance_dict.get("account_currency", company_currency)

	# Case 1
	if presentation_currency == company_currency:
		return debit, credit

	# Case 2
	if account_currency == presentation_currency:
		return debit_acc, credit_acc

	# Case 3 — convert company-currency amounts to presentation currency
	return convert(debit, presentation_currency, company_currency, report_date), convert(
		credit, presentation_currency, company_currency, report_date
	)


def get_opening_balances(filters, account_filter=None):
	GL_Entry = frappe.qb.DocType("GL Entry")

	query = (
		frappe.qb.from_(GL_Entry)
		.select(
			GL_Entry.party,
			GL_Entry.account_currency,
			Sum(GL_Entry.debit).as_("debit"),
			Sum(GL_Entry.credit).as_("credit"),
			Sum(GL_Entry.debit_in_account_currency).as_("debit_acc"),
			Sum(GL_Entry.credit_in_account_currency).as_("credit_acc"),
		)
		.where(
			(GL_Entry.company == filters.company)
			& (GL_Entry.is_cancelled == 0)
			& (GL_Entry.party_type == filters.party_type)
			& (GL_Entry.party != "")
			& (
				(GL_Entry.posting_date < filters.from_date)
				| ((GL_Entry.is_opening == "Yes") & (GL_Entry.posting_date <= filters.to_date))
			)
		)
		.groupby(GL_Entry.party, GL_Entry.account_currency)
	)

	if account_filter:
		query = query.where(GL_Entry.account.isin(account_filter))

	gle = query.run(as_dict=True)

	opening = frappe._dict()
	for d in gle:
		opening[d.party] = {
			"debit": flt(d.debit),
			"credit": flt(d.credit),
			"debit_acc": flt(d.debit_acc),
			"credit_acc": flt(d.credit_acc),
			"account_currency": d.account_currency,
		}

	return opening


def get_balances_within_period(filters, account_filter=None):
	GL_Entry = frappe.qb.DocType("GL Entry")

	query = (
		frappe.qb.from_(GL_Entry)
		.select(
			GL_Entry.party,
			GL_Entry.account_currency,
			Sum(GL_Entry.debit).as_("debit"),
			Sum(GL_Entry.credit).as_("credit"),
			Sum(GL_Entry.debit_in_account_currency).as_("debit_acc"),
			Sum(GL_Entry.credit_in_account_currency).as_("credit_acc"),
		)
		.where(
			(GL_Entry.company == filters.company)
			& (GL_Entry.is_cancelled == 0)
			& (GL_Entry.party_type == filters.party_type)
			& (GL_Entry.party != "")
			& (GL_Entry.posting_date >= filters.from_date)
			& (GL_Entry.posting_date <= filters.to_date)
			& (GL_Entry.is_opening == "No")
		)
		.groupby(GL_Entry.party, GL_Entry.account_currency)
	)

	if account_filter:
		query = query.where(GL_Entry.account.isin(account_filter))

	gle = query.run(as_dict=True)

	balances = frappe._dict()
	for d in gle:
		balances[d.party] = {
			"debit": flt(d.debit),
			"credit": flt(d.credit),
			"debit_acc": flt(d.debit_acc),
			"credit_acc": flt(d.credit_acc),
			"account_currency": d.account_currency,
		}

	return balances


def toggle_debit_credit(debit, credit):
	if flt(debit) > flt(credit):
		debit = flt(debit) - flt(credit)
		credit = 0.0
	else:
		credit = flt(credit) - flt(debit)
		debit = 0.0

	return debit, credit


def get_columns(filters, show_party_name):
	columns = [
		{
			"fieldname": "party",
			"label": _(filters.party_type),
			"fieldtype": "Link",
			"options": filters.party_type,
			"width": 200,
		},
		{
			"fieldname": "opening_debit",
			"label": _("Opening (Dr)"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"fieldname": "opening_credit",
			"label": _("Opening (Cr)"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"fieldname": "debit",
			"label": _("Debit"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"fieldname": "credit",
			"label": _("Credit"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"fieldname": "closing_debit",
			"label": _("Closing (Dr)"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"fieldname": "closing_credit",
			"label": _("Closing (Cr)"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"fieldname": "currency",
			"label": _("Currency"),
			"fieldtype": "Link",
			"options": "Currency",
			"hidden": 1,
		},
	]

	if show_party_name:
		columns.insert(
			1,
			{
				"fieldname": "party_name",
				"label": _(filters.party_type) + " Name",
				"fieldtype": "Data",
				"width": 200,
			},
		)

	return columns


def is_party_name_visible(filters):
	show_party_name = False

	if filters.get("party_type") in ["Customer", "Supplier"]:
		if filters.get("party_type") == "Customer":
			party_naming_by = frappe.get_single_value("Selling Settings", "cust_master_name")
		else:
			party_naming_by = frappe.db.get_single_value("Buying Settings", "supp_master_name")

		if party_naming_by == "Naming Series":
			show_party_name = True
	else:
		show_party_name = True

	return show_party_name
