# Multi-Company Support — API Change Spec

Today every API resolves a single (sales_person → company) tuple from the first matching row in `Basket4Me Settings.sales_person_details`. To support a salesperson serving N companies we need an explicit `company` selector on mutations + scoping on reads, plus a small set of new helpers/endpoints. Below is the scope grouped by layer.

---

## 1. Data model

No JSON schema change to `Basket4Me Settings → Sales Person Details`. The child table already supports multiple rows for the same sales_person with different `company` values. We just stop assuming "first match wins."

**Validation to add** (server-side, on Basket4Me Settings save):
- Reject duplicate `(sales_person, company)` pairs.
- Reject rows where `company` is empty.

---

## 2. New helpers (api.py)

| helper | signature | notes |
|---|---|---|
| `get_user_companies(user=None)` | returns `list[str]` | unique companies the user's sales_person is configured for; empty if none |
| `get_sales_person_details(sales_person, company)` | returns the matching settings row or `None` | replaces every `for detail in settings.sales_person_details: if detail.sales_person == sp:` loop |
| `resolve_company(params, user=None)` | returns the company to use, raises 400 if ambiguous/invalid | precedence: params.company → request header `X-Basket4me-Company` → user's single company → error |
| `assert_company_allowed(company, user=None)` | raises 403 if company ∉ get_user_companies | used by every mutation before doc creation |

These let the existing call sites change from "find a row" to "find the row for THIS company."

---

## 3. New whitelisted endpoint

`GET /api/method/basket4me_pwa.api.get_user_companies`
```json
{
  "user_id": "btladmin@btl.in",
  "companies": ["BALAJI TRADE LINKS", "BTL HOLDINGS"],
  "default_company": "BALAJI TRADE LINKS"
}
```
Frontend uses this on login to populate a company picker. `default_company` = first row OR a new `is_default` Check field on the child table (optional follow-up).

---

## 4. Mutation APIs — add `company` param (required when user has >1 company)

All of these currently read `sales_person_details.company` from the first matched row. After change: take `company` from params (validated against allowed list) and look up the matching settings row.

- `create_sales_invoice(params)` — `params.company`
- `update_sales_invoice(params)` — inherits company from existing doc; reject change after submit
- `submit_sales_invoice(params)` — no new param (uses doc.company)
- `cancel_sales_invoice(params)` — no new param
- `delete_sales_invoice(params)` — no new param
- `create_sales_invoice_return(params)` / `create_sales_return(params)` — `params.company`; for return_against, company MUST match the original SI's company
- `update_sales_return` / `submit_sales_return` / `cancel_sales_return` / `delete_sales_return` — same as their SI wrappers
- `create_sales_order(params)` — `params.company`
- `update_sales_order` / `submit_sales_order` / `cancel_sales_order` / `delete_sales_order` — same as SI
- `create_payment_entry(params)` — `params.company`; mode_of_payment account selected from that company
- `convert_so_to_si(params)` — derived from SO doc (validate SO.company is allowed)
- `create_dn_from_so(params)` — same
- `update_invoice_unpaid_status(params)` — no new param

**Error contract:**
- Missing `company` when user has multiple → 400 "company is required (allowed: …)"
- `company` not in user's allowed list → 403 "Not permitted for company X"
- `company` mismatch with `return_against` / `sales_order` / `against_sales_invoice` → 400 with specific message

---

## 5. Read APIs — optional `company` filter

Default behavior: return data for ALL companies the user is allowed in. Add an optional `company` param to scope down.

- `get_invoice_list`, `get_invoice_detail`
- `get_return_invoice_list`, `get_sales_return_detail`
- `get_sales_order_list`, `get_sales_order_detail`
- `get_delivery_note_list`, `get_delivery_note_detail`
- `get_receipt_list`
- `get_customer_list`, `get_customer_detail`, `get_customer_list_v2`, `get_customer_list_with_effective_price_list`
- `get_item_list`, `get_price_list_items`, `get_price_list_items_bulk`
- `get_invoices_for_return`
- `get_pending_sales_order_items`
- `get_dashboard_summary`, `get_sales_metrics`
- `get_customer_invoices`, `get_customer_invoice_aging`
- `last_invoice_cust_receipt`
- `get_transaction_history`

**Filter semantics:**
- `company` provided → strict equality
- `company` omitted → `IN (<user's allowed companies>)`

---

## 6. Permission query conditions

Update [permission/sales_invoice.py](basket4me_pwa/permission/sales_invoice.py) and [permission/customer.py](basket4me_pwa/permission/customer.py) to scope by user's allowed companies (union with existing sales_team filter):

```python
allowed = ",".join(repr(c) for c in get_user_companies(user))
return f"`tabSales Invoice`.company IN ({allowed}) AND (existing-sales-team-clause)"
```

Same shape for `get_permission_query_conditions_for_payment_entry`. Customer is multi-company by default in ERPNext; no change unless we want to hide customers not linked to user's companies.

---

## 7. Pricing / effective price list

`get_effective_price_list(customer=None, sales_person=None)` becomes:

```python
get_effective_price_list(customer=None, sales_person=None, company=None)
```

Lookup order unchanged but the sales-person-details row picked is the one matching `company`. Customer-based price list (when `enable_customer_based_price_list` is on) takes priority and ignores company.

---

## 8. Mode of Payment + accounts

[get_modes_of_payment_for_company](basket4me_pwa/api.py) already filters by company — keep as-is.

`create_payment_entry` already picks `paid_to_account = mode_doc.accounts[0].default_account`; change to pick the account row whose `company` equals the resolved `company`. Reject if none.

---

## 9. Login / user details

`auth.user_login` response unchanged; `auth.get_user_details` adds:

```json
"companies": ["BALAJI TRADE LINKS", "BTL HOLDINGS"],
"default_company": "BALAJI TRADE LINKS"
```

Existing `company_name` stays for backward compat (set to `default_company`).

---

## 10. Frontend contract (for the mobile team)

- On login, call `get_user_companies`; if `companies.length > 1`, show a company switcher in the app header.
- Persist selected company; pass it on every mutation as `params.company` and on every read as `?company=…` when filtering.
- Optional: send header `X-Basket4me-Company: <name>` so `resolve_company` can pick it up without every call site adding the param explicitly.

---

## 11. Migration steps

1. Code: add helpers + endpoints + per-API param additions (no schema change).
2. Fixture/optional: add `is_default` Check on `Sales Person Detail` child table.
3. Patch (post_model_sync): for sales persons with multiple rows, mark the first as `is_default=1`.
4. Frontend: ship company picker.

No backfill needed on existing transactional docs — they already have a `company` column populated.

---

## 12. Out of scope (call out separately if requested)

- Cross-company inventory transfers
- Cross-company consolidated dashboards (we just sum across allowed companies)
- Per-company custom_unpaid audit log (the existing fields are per-SI, which is already per-company)
- Per-company Frappe User Permissions (rely on Basket4Me Settings rows as source of truth instead)

---

## Estimated effort

- Helpers + endpoints + permission updates: ~1 day
- Per-API param additions + tests: ~2 days (most are mechanical)
- Frontend picker + plumbing: ~1 day
- QA: ~1 day

Total: ~1 working week for full rollout.
