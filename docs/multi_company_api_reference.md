# Multi-Company API Reference

Frontend-facing contract for the multi-company endpoints. Backward compatible: single-company users see no behavior change.

**Base URL:** `https://<site>/api/method/`
**Auth:** `Authorization: token <api_key>:<api_secret>` on every request.

---

## 1. Discover allowed companies for a user

### `GET basket4me_pwa.api.get_user_companies`

**Query params**

| name | required | description |
|---|---|---|
| `user_id` | no | defaults to session user |

**Response (`data`)**

```json
{
  "user_id": "btladmin@btl.in",
  "companies": [
    {
      "name": "BALAJI TRADE LINKS",
      "company_name": "BALAJI TRADE LINKS",
      "company_address": "12 ABC Road, Cochin, KL, 682001, India",
      "company_gst_no": "32ABCDE1234F1Z5",
      "company_logo": "https://btlerp.m.frappe.cloud/files/btl-logo.png",
      "company_default_price_list": "Standard Selling",
      "company_bank_name": "Canara Bank",
      "company_bank_acc_no": "1234567890",
      "company_bank_ifsc": "CNRB0001234",
      "company_bank_branch": "CNRB0001"
    },
    {
      "name": "BTL HOLDINGS",
      "company_name": "BTL HOLDINGS",
      "company_address": "…",
      "company_gst_no": null,
      "company_logo": null,
      "company_default_price_list": "Standard Selling",
      "company_bank_name": null,
      "company_bank_acc_no": null,
      "company_bank_ifsc": null,
      "company_bank_branch": null
    }
  ],
  "company_names": ["BALAJI TRADE LINKS", "BTL HOLDINGS"],
  "default_company": "BALAJI TRADE LINKS"
}
```

- `companies[]` → list of dicts, full metadata. Use this to render the picker (logo, address, GST, bank info, default price list).
- `company_names[]` → convenience list of names in same order.
- `default_company` → first row in Basket4Me Settings → Sales Person Details (where `sales_person == user.sales_person`). Use as the initial picker selection.

**Status codes**

| code | meaning |
|---|---|
| 200 | OK (even if no companies configured — `companies` is `[]`) |
| 500 | unexpected — check `error log` |

---

## 2. Auth.get_user_details also returns the picker data

The existing login enrichment endpoint now carries the same fields, so frontends can populate the picker without a second call.

`GET basket4me_pwa.auth.get_user_details?sid=…&user_id=…`

**Response (`user`) — new fields only**

```json
{
  "company_name": "BALAJI TRADE LINKS",      // = default_company (backward compat)
  "companies": ["BALAJI TRADE LINKS", "BTL HOLDINGS"],   // plain list of names
  "default_company": "BALAJI TRADE LINKS",
  …
}
```

The per-company metadata (`company_address`, `company_gst_no`, `company_logo`, `company_default_price_list`, `company_bank_*`) is returned ONLY for `company_name` / `default_company`. For full metadata on other companies, call `get_user_companies`.

---

## 3. Passing `company` on every request

Once the frontend has a selected company, send it on every API call. Two equivalent ways:

**Option A — body param (mutations):**
```json
{
  "params": {
    "customer": "1008 Bazar Vaduthala",
    "items": [...],
    "company": "BALAJI TRADE LINKS"
  }
}
```

**Option B — header (reads & mutations):**
```
X-Basket4me-Company: BALAJI TRADE LINKS
```

If both are sent, the body param wins. If neither is sent and the user has more than one company, mutations return 400; reads default to "all allowed companies".

---

## 4. Mutation endpoints — `company` is required when user has >1 company

All of these accept `company` in `params`. Server validates it's in the user's allowed set (403 otherwise) and looks up the matching Basket4Me Settings row (warehouse / cost center / mode of payment / price list / deduction account scoped to that company).

| endpoint | notes |
|---|---|
| `POST basket4me_pwa.api.create_sales_invoice` | `params.company` |
| `POST basket4me_pwa.api.update_sales_invoice` | company derived from existing doc; not changeable on update |
| `POST basket4me_pwa.api.submit_sales_invoice` | no new param |
| `POST basket4me_pwa.api.cancel_sales_invoice` | no new param |
| `POST basket4me_pwa.api.delete_sales_invoice` | no new param |
| `POST basket4me_pwa.api.create_sales_return` | alias of `create_sales_invoice_return` |
| `POST basket4me_pwa.api.create_sales_invoice_return` | `params.company`; if `return_against` is set, company MUST match original SI |
| `POST basket4me_pwa.api.update_sales_return` | wrapper around `update_sales_invoice` |
| `POST basket4me_pwa.api.submit_sales_return` | wrapper around `submit_sales_invoice` |
| `POST basket4me_pwa.api.cancel_sales_return` | wrapper around `cancel_sales_invoice` |
| `POST basket4me_pwa.api.delete_sales_return` | wrapper around `delete_sales_invoice` |
| `POST basket4me_pwa.api.create_sales_order` | `params.company` |
| `POST basket4me_pwa.api.update_sales_order` | company derived from existing doc |
| `POST basket4me_pwa.api.submit_sales_order` | no new param |
| `POST basket4me_pwa.api.cancel_sales_order` | no new param |
| `POST basket4me_pwa.api.delete_sales_order` | no new param |
| `POST basket4me_pwa.api.create_payment_entry` | `params.company`; MOP account row picked by company |
| `POST basket4me_pwa.api.convert_so_to_si` | all SOs must share same company; derived from first SO |
| `POST basket4me_pwa.api.create_dn_from_so` | same |
| `POST basket4me_pwa.api.update_invoice_unpaid_status` | no new param |

### Error contract

**Missing `company` when user has multiple:**

```json
{
  "message": "company is required (user is configured for multiple companies)",
  "data": { "allowed_companies": ["BALAJI TRADE LINKS", "BTL HOLDINGS"] },
  "success": false
}
```

**`company` not in user's allowed list (403):**

```
PermissionError: Not permitted for company 'X'. Allowed: BALAJI TRADE LINKS, BTL HOLDINGS
```

**`company` mismatch with `return_against`:**

```json
{
  "message": "company 'X' does not match return_against SI company 'Y'",
  "data": {},
  "success": false
}
```

---

## 5. Read endpoints — `company` is optional

All listing/summary endpoints accept an optional `company` param. When omitted, the response spans **all** companies the user is configured for. When provided, the value is validated against the user's allowed set.

| endpoint | new param |
|---|---|
| `GET basket4me_pwa.api.get_invoice_list` | `company` |
| `GET basket4me_pwa.api.get_return_invoice_list` | `company` |
| `GET basket4me_pwa.api.get_sales_order_list` | `company` |
| `GET basket4me_pwa.api.get_delivery_note_list` | `company` |
| `GET basket4me_pwa.api.get_receipt_list` | `company` |
| `GET basket4me_pwa.api.get_dashboard_summary` | `company` |
| `GET basket4me_pwa.api.last_invoice_cust_receipt` | `company` |

List responses now include `company` on each row. Detail responses already do.

### Example — list invoices for one company only

```
GET /api/method/basket4me_pwa.api.get_invoice_list?company=BALAJI%20TRADE%20LINKS&page_number=1&page_size=20
```

### Example — dashboard summary across all allowed companies (default)

```
GET /api/method/basket4me_pwa.api.get_dashboard_summary?period=daily
```

### Example — pin dashboard to one company

```
GET /api/method/basket4me_pwa.api.get_dashboard_summary?period=daily&company=BTL%20HOLDINGS
```

---

## 6. Permission scoping (no client change needed)

The `permission_query_conditions` hooks for `Sales Invoice`, `Payment Entry`, and `Customer` automatically restrict list views to the user's allowed companies, in addition to the existing sales-team filter.

- `view_all_transaction_role` (Basket4Me Settings) → bypasses both filters.
- `override_sales_team_in_customer` (Basket4Me Settings) → bypasses sales-team filter, multi-company filter still applies.
- `Administrator` → bypasses both.

---

## 7. Mode of Payment selection (Phase 4)

`POST basket4me_pwa.api.create_payment_entry` now picks the `Mode of Payment` → `accounts[]` row whose `company` matches the resolved company. The previous behavior (always `accounts[0]`) silently used the wrong account when a single MOP had per-company rows.

If no exact-company match is found, falls back to `accounts[0]` to keep single-company setups working.

---

## 8. Frontend implementation checklist

1. On login (`auth.user_login`), then `auth.get_user_details`:
   - Read `companies` + `default_company`.
2. Optional: call `get_user_companies` for the full per-company metadata if not already in `get_user_details`.
3. If `companies.length > 1`: render a company picker (header / drawer). Persist selection in local storage.
4. On every API call, include either `params.company` (mutations) or header `X-Basket4me-Company` (any request).
5. On a `403 PermissionError: Not permitted for company …` response → re-fetch `get_user_companies` (the user's allowed set may have changed) and re-prompt.
6. On a `400 "company is required …"` response with `allowed_companies` in the data → show the picker.

---

## 9. Migration / rollout

- **Schema:** no migration needed. Basket4Me Settings → Sales Person Details already supports `(sales_person, company)` rows (just add additional rows in the UI for multi-company users).
- **Deploy:** push the latest `main` to Frappe Cloud → trigger deploy → `bench build` (for JS) → `bench restart`. No `bench migrate` required.
- **Backward compat:** single-company users see no change. APIs accept the new param/header but don't require it for single-company.

---

## 10. Related files

- [basket4me_pwa/api.py](basket4me_pwa/api.py) — helpers, endpoint, mutation + read API changes
- [basket4me_pwa/auth.py](basket4me_pwa/auth.py) — `get_user_details` enrichment
- [basket4me_pwa/permission/sales_invoice.py](basket4me_pwa/permission/sales_invoice.py) — SI permission scoping
- [basket4me_pwa/permission/customer.py](basket4me_pwa/permission/customer.py) — PE permission scoping
- [docs/multi_company_api_changes.md](docs/multi_company_api_changes.md) — change-spec / scope doc (internal)

## 11. Commit trail (for verifying deployment)

| commit | scope |
|---|---|
| `1dccee9` | Foundation — helpers, endpoint, permission scoping, login enrichment |
| `5e8207b` | Phase 2+4 — mutation APIs use `resolve_company` + MOP account row matches company |
| `41aab58` | Phase 3 — read APIs accept optional `company` filter |
| `c47ef0a` | `get_user_companies` returns per-company metadata for the picker |

Verify on bench: `cd ~/frappe-bench/apps/basket4me_pwa && git log -1 --oneline` should show `c47ef0a` or newer.
