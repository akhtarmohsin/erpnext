# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

import unittest

import frappe
from frappe.utils import today

from erpnext.accounts.report.trial_balance_for_party.trial_balance_for_party import (
	execute,
	set_account_currency,
)
from erpnext.accounts.utils import get_fiscal_year


def _get_site_data():
	"""
	Discover real master data from the site at runtime so the tests are not
	tied to a specific company / customer / item name.
	Returns a dict with the fields needed to create a Sales Invoice and run
	the report.
	"""
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	# Fiscal year that covers today
	fy = get_fiscal_year(today(), company=company)

	# A customer whose default_currency != company_currency (foreign customer),
	# together with the matching receivable account. Fall back to any customer
	# and the plain INR Debtors account when no foreign customer is found.
	foreign_row = frappe.db.sql(
		"""
		SELECT c.name AS customer, c.default_currency AS currency,
		       a.name AS debit_to
		FROM   tabCustomer c
		JOIN   tabAccount  a
		       ON  a.company        = %(company)s
		       AND a.account_type   = 'Receivable'
		       AND a.account_currency = c.default_currency
		WHERE  c.default_currency IS NOT NULL
		  AND  c.default_currency != %(company_currency)s
		LIMIT  1
		""",
		{"company": company, "company_currency": company_currency},
		as_dict=True,
	)

	if foreign_row:
		customer = foreign_row[0].customer
		customer_currency = foreign_row[0].currency
		debit_to = foreign_row[0].debit_to
	else:
		# Fallback: any customer with the company currency
		customer = frappe.db.get_value("Customer", {}, "name")
		customer_currency = company_currency
		debit_to = frappe.db.get_value(
			"Account",
			{"company": company, "account_type": "Receivable", "account_currency": company_currency},
			"name",
		)

	# Any submitted item
	item_code = frappe.db.get_value("Item", {"disabled": 0}, "name")

	# Income account and cost centre
	income_account = frappe.db.get_value(
		"Account",
		{"company": company, "root_type": "Income", "is_group": 0},
		"name",
	)
	cost_center = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")

	return frappe._dict(
		company=company,
		company_currency=company_currency,
		fiscal_year=fy[0],
		from_date=str(fy[1]),
		to_date=str(fy[2]),
		customer=customer,
		customer_currency=customer_currency,
		debit_to=debit_to,
		item_code=item_code,
		income_account=income_account,
		cost_center=cost_center,
	)


def _make_sales_invoice(d):
	"""Create and submit a minimal Sales Invoice without importing ERPNextTestSuite."""
	si = frappe.new_doc("Sales Invoice")
	si.posting_date = today()
	si.company = d.company
	si.customer = d.customer
	si.debit_to = d.debit_to
	si.currency = d.customer_currency
	# Use a round exchange rate if the currency differs; 1 when same
	si.conversion_rate = 84 if d.customer_currency != d.company_currency else 1
	si.naming_series = "SINV-"
	si.append(
		"items",
		{
			"item_code": d.item_code,
			"qty": 1,
			"rate": 100,
			"income_account": d.income_account,
			"cost_center": d.cost_center,
		},
	)
	si.save()
	si.submit()
	frappe.db.commit()
	return si


class TestTrialBalanceForParty(unittest.TestCase):
	"""
	Tests for the currency / presentation_currency filter feature in
	Trial Balance for Party report. Master data is discovered dynamically
	from the site so the suite works against any ERPNext installation.
	"""

	@classmethod
	def setUpClass(cls):
		cls.d = _get_site_data()
		cls.si = _make_sales_invoice(cls.d)

	@classmethod
	def tearDownClass(cls):
		if cls.si and frappe.db.exists("Sales Invoice", cls.si.name):
			doc = frappe.get_doc("Sales Invoice", cls.si.name)
			if doc.docstatus == 1:
				doc.cancel()
		frappe.db.commit()

	def _base_filters(self, **kwargs):
		d = self.d
		filters = frappe._dict(
			{
				"company": d.company,
				"fiscal_year": d.fiscal_year,
				"from_date": d.from_date,
				"to_date": d.to_date,
				"party_type": "Customer",
				"show_zero_values": 1,
				"exclude_zero_balance_parties": 0,
			}
		)
		filters.update(kwargs)
		return filters

	# --- Tests ---

	def test_report_runs_without_currency_filter(self):
		"""Report executes successfully without a presentation_currency filter."""
		columns, data = execute(self._base_filters())
		self.assertTrue(len(columns) > 0)
		self.assertTrue(len(data) > 0)

	def test_currency_defaults_to_company_currency(self):
		"""set_account_currency defaults presentation_currency to company currency when unset."""
		filters = set_account_currency(self._base_filters())
		self.assertEqual(filters["presentation_currency"], self.d.company_currency)

	def test_currency_column_in_every_data_row(self):
		"""Every data row carries a 'currency' field matching the presentation_currency."""
		filters = self._base_filters(presentation_currency=self.d.company_currency)
		columns, data = execute(filters)
		for row in data:
			self.assertEqual(row.get("currency"), self.d.company_currency)

	def test_totals_row_carries_currency(self):
		"""The totals row carries the correct presentation currency."""
		filters = self._base_filters(presentation_currency=self.d.company_currency)
		columns, data = execute(filters)
		self.assertEqual(data[-1].get("currency"), self.d.company_currency)

	def test_foreign_currency_presentation_no_exchange_rate_error(self):
		"""
		When a foreign customer is selected and presentation_currency = account_currency,
		the report must return amounts without throwing an exchange-rate error.
		(GL reads debit_in_account_currency directly — no conversion needed.)
		"""
		if self.d.customer_currency == self.d.company_currency:
			self.skipTest("No foreign-currency customer found on this site")

		filters = self._base_filters(
			party=self.d.customer,
			presentation_currency=self.d.customer_currency,
		)
		# Must not raise; amounts should be in account currency (USD etc.)
		columns, data = execute(filters)
		party_rows = [r for r in data if r.get("party") != "'Totals'"]
		self.assertTrue(len(party_rows) > 0)
		for row in data:
			self.assertEqual(row.get("currency"), self.d.customer_currency)

	def test_set_account_currency_from_foreign_receivable_account(self):
		"""set_account_currency detects account_currency from a foreign-currency receivable account."""
		usd_account = frappe.db.get_value(
			"Account",
			{
				"company": self.d.company,
				"account_currency": ["!=", self.d.company_currency],
				"account_type": "Receivable",
			},
			"name",
		)
		if not usd_account:
			self.skipTest("No foreign-currency Receivable account on this site")

		expected_currency = frappe.get_cached_value("Account", usd_account, "account_currency")
		filters = set_account_currency(self._base_filters(account=[usd_account]))
		self.assertEqual(filters["account_currency"], expected_currency)
		self.assertEqual(filters["presentation_currency"], expected_currency)

	def test_set_account_currency_from_party_gle(self):
		"""set_account_currency resolves currency from a party's existing GL entries."""
		filters = set_account_currency(self._base_filters(party=self.d.customer))
		self.assertIn("account_currency", filters)
		self.assertIn("presentation_currency", filters)
		self.assertTrue(filters["account_currency"])

	def test_explicit_presentation_currency_not_overridden(self):
		"""An explicitly provided presentation_currency is not overridden by set_account_currency."""
		filters = self._base_filters(presentation_currency=self.d.company_currency)
		filters = set_account_currency(filters)
		self.assertEqual(filters["presentation_currency"], self.d.company_currency)

	def test_closing_balance_consistency(self):
		"""closing net == opening net + period net on the totals row."""
		columns, data = execute(self._base_filters())
		total = data[-1]
		net_opening = _flt(total.get("opening_debit")) - _flt(total.get("opening_credit"))
		net_period = _flt(total.get("debit")) - _flt(total.get("credit"))
		net_closing = _flt(total.get("closing_debit")) - _flt(total.get("closing_credit"))
		self.assertAlmostEqual(net_closing, net_opening + net_period, places=2)

	def test_filter_by_single_party(self):
		"""When party filter is set, only that party appears in non-totals rows."""
		filters = self._base_filters(party=self.d.customer, show_zero_values=1)
		columns, data = execute(filters)
		party_rows = [r for r in data if r.get("party") != "'Totals'"]
		for row in party_rows:
			self.assertEqual(row["party"], self.d.customer)

	def test_exclude_zero_balance_parties(self):
		"""exclude_zero_balance_parties removes parties whose closing balance is zero."""
		_, data_all = execute(self._base_filters(exclude_zero_balance_parties=0))
		_, data_filtered = execute(self._base_filters(exclude_zero_balance_parties=1))
		self.assertLessEqual(len(data_filtered), len(data_all))

	def test_currency_column_present_in_columns_metadata(self):
		"""The hidden 'currency' column must appear in the report's column definitions."""
		columns, _ = execute(self._base_filters())
		fieldnames = [c["fieldname"] for c in columns]
		self.assertIn("currency", fieldnames)

	def test_foreign_currency_party_uses_account_currency_field(self):
		"""
		Regression test for the core currency bug: when presentation_currency ==
		party's account_currency, the report must use debit_in_account_currency
		(not debit which is in company currency). Verified by comparing the
		party-filtered result against a direct GL query.
		"""
		if self.d.customer_currency == self.d.company_currency:
			self.skipTest("Need a foreign-currency customer on this site")

		# Fetch GL sum directly for this party in account currency
		gl_sum = frappe.db.sql(
			"""
			SELECT SUM(debit_in_account_currency) as d, SUM(credit_in_account_currency) as c
			FROM `tabGL Entry`
			WHERE party = %(party)s AND company = %(company)s AND is_cancelled = 0
			  AND posting_date BETWEEN %(from_date)s AND %(to_date)s
			  AND is_opening = 'No'
			""",
			{
				"party": self.d.customer,
				"company": self.d.company,
				"from_date": self.d.from_date,
				"to_date": self.d.to_date,
			},
			as_dict=True,
		)
		if not gl_sum or not (gl_sum[0].d or gl_sum[0].c):
			self.skipTest("No GL entries for this customer in the period")

		expected_debit = _flt(gl_sum[0].d)
		expected_credit = _flt(gl_sum[0].c)

		_, data = execute(
			self._base_filters(
				party=self.d.customer,
				presentation_currency=self.d.customer_currency,
			)
		)
		party_rows = [r for r in data if r.get("party") == self.d.customer]
		self.assertTrue(party_rows, "Party row not found in report data")

		self.assertAlmostEqual(party_rows[0].get("debit", 0), expected_debit, places=2)
		self.assertAlmostEqual(party_rows[0].get("credit", 0), expected_credit, places=2)


def _flt(val):
	try:
		return float(val or 0)
	except (TypeError, ValueError):
		return 0.0
