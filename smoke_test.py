#!/usr/bin/env python3
"""
Smoke test for Basket4Me PWA APIs against the live Frappe Cloud site.

Exercises the read-side surface (login + ~25 GET / read-only POST endpoints).
Prints a PASS/FAIL summary and exits non-zero if any check fails.

Usage:
    python3 smoke_test.py                        # default site + default creds
    BASE_URL=https://...m.frappe.cloud SITE_USR=... SITE_PWD=... python3 smoke_test.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any
from urllib import error, parse, request

BASE_URL = os.environ.get("BASE_URL", "https://basket4meerp.m.frappe.cloud").rstrip("/")
SITE_USR = os.environ.get("SITE_USR", "aswathy@dev.in")
SITE_PWD = os.environ.get("SITE_PWD", "Frappe@123")
TEST_CUSTOMER = os.environ.get("TEST_CUSTOMER", "Vimal Store Cherthala")
TEST_PRICE_LIST = os.environ.get("TEST_PRICE_LIST", "Standard Selling")

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def http(method: str, path: str, *, params=None, body=None, token=None, timeout=30):
    url = BASE_URL + path
    if params:
        url += "?" + parse.urlencode(params)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            code = resp.getcode()
    except error.HTTPError as e:
        raw = e.read()
        code = e.code
    except Exception as e:
        return -1, {"_error": str(e)}
    try:
        return code, json.loads(raw.decode())
    except Exception:
        return code, {"_raw": raw[:300].decode(errors="replace")}


def login() -> str:
    code, body = http(
        "POST",
        "/api/method/basket4me_pwa.auth.user_login",
        body={"usr": SITE_USR, "pwd": SITE_PWD},
    )
    if code != 200:
        raise SystemExit(f"Login failed HTTP {code}: {body}")
    msg = body.get("message") or {}
    api_key = msg.get("api_key")
    api_secret = msg.get("api_secret")
    if not (api_key and api_secret):
        raise SystemExit(f"Login response missing api_key/secret: {body}")
    return f"token {api_key}:{api_secret}"


# ── Check definitions ───────────────────────────────────────────────────────

def make_checks(token: str):
    """Return a list of (name, fn) where fn returns (ok: bool, detail: str)."""
    checks = []

    def add(name, method, path, *, params=None, body=None, expect_envelope=True, validator=None, allow_404_on_data=False):
        def run():
            code, b = http(method, path, params=params, body=body, token=token)
            if code < 0:
                return False, f"connection error: {b.get('_error')}"
            if code >= 500:
                return False, f"HTTP {code}: {str(b)[:200]}"
            if expect_envelope:
                if not isinstance(b, dict):
                    return False, "not a dict"
                if "success" not in b and "message" not in b:
                    return False, f"unexpected shape: {str(b)[:200]}"
            if validator:
                ok, why = validator(code, b)
                if not ok:
                    return False, why
            return True, f"HTTP {code}"
        checks.append((name, run))

    # ── Auth / user
    add(
        "auth.get_user_details",
        "POST",
        "/api/method/basket4me_pwa.auth.get_user_details",
        body={"user_id": SITE_USR},
        validator=lambda c, b: (
            isinstance(b.get("message"), dict) and "user" in b["message"],
            "no user in response",
        ),
    )

    # ── Dashboard
    add(
        "get_dashboard_summary",
        "GET",
        "/api/method/basket4me_pwa.api.get_dashboard_summary",
        params={"period": "daily"},
    )

    # ── Customers
    add(
        "get_customer_list_v2",
        "GET",
        "/api/method/basket4me_pwa.api.get_customer_list_v2",
        params={"page_number": 1, "page_size": 5},
        validator=lambda c, b: (
            isinstance(b.get("data", {}).get("customers"), list),
            "data.customers missing",
        ),
    )
    add(
        "get_customer_outstanding (single)",
        "GET",
        "/api/method/basket4me_pwa.api.get_customer_outstanding",
        params={"customer": TEST_CUSTOMER},
    )
    add(
        "get_customer_outstanding (bulk)",
        "POST",
        "/api/method/basket4me_pwa.api.get_customer_outstanding",
        body={"customers": [TEST_CUSTOMER]},
    )
    add(
        "get_customer_balance_summary",
        "GET",
        "/api/method/basket4me_pwa.api.get_customer_balance_summary",
        params={"customer": TEST_CUSTOMER},
    )
    add(
        "get_customer_route_list",
        "GET",
        "/api/method/basket4me_pwa.api.get_customer_route_list",
    )

    # ── Items / pricing
    add(
        "get_item_list",
        "GET",
        "/api/method/basket4me_pwa.api.get_item_list",
        params={"page_number": 1, "page_size": 5},
        validator=lambda c, b: (
            isinstance(b.get("data", {}).get("items"), list),
            "data.items missing",
        ),
    )
    add(
        "get_price_list",
        "GET",
        "/api/method/basket4me_pwa.api.get_price_list",
    )
    add(
        "get_price_list_details",
        "GET",
        "/api/method/basket4me_pwa.api.get_price_list_details",
        params={"page_size": 3},
    )
    # The lemon-search bug repro — must return >= 1 item now.
    add(
        "get_price_list_items (search=lemon)",
        "GET",
        "/api/method/basket4me_pwa.api.get_price_list_items",
        params={"price_list": TEST_PRICE_LIST, "search": "lemon", "page_size": 5},
        validator=lambda c, b: (
            len(b.get("data", {}).get("items") or []) > 0,
            "0 items returned for search=lemon",
        ),
    )
    # Generic: page 1 should not be empty for a typical price list.
    add(
        "get_price_list_items (no filter)",
        "GET",
        "/api/method/basket4me_pwa.api.get_price_list_items",
        params={"price_list": TEST_PRICE_LIST, "page_size": 3},
        validator=lambda c, b: (
            len(b.get("data", {}).get("items") or []) > 0,
            "no items in price list",
        ),
    )
    add(
        "get_price_list_items_bulk",
        "POST",
        "/api/method/basket4me_pwa.api.get_price_list_items_bulk",
        body={"price_list": TEST_PRICE_LIST, "item_codes": ["AF00001", "AF00002"]},
        validator=lambda c, b: (
            len(b.get("data", {}).get("items") or []) >= 1,
            "bulk lookup returned 0",
        ),
    )

    # ── MoP
    add(
        "get_available_modes_of_payment",
        "GET",
        "/api/method/basket4me_pwa.api.get_available_modes_of_payment",
        validator=lambda c, b: (
            isinstance(b.get("data", {}).get("modes_of_payment"), list),
            "modes_of_payment missing",
        ),
    )

    # ── Sales documents (read-only)
    add(
        "get_sales_order_list",
        "GET",
        "/api/method/basket4me_pwa.api.get_sales_order_list",
        params={"page_number": 1, "page_size": 3},
    )
    add(
        "get_invoice_list",
        "GET",
        "/api/method/basket4me_pwa.api.get_invoice_list",
        params={"page_number": 1, "page_size": 3},
    )
    add(
        "get_delivery_note_list",
        "GET",
        "/api/method/basket4me_pwa.api.get_delivery_note_list",
        params={"limit_page_length": 3},
    )
    add(
        "get_receipt_list",
        "GET",
        "/api/method/basket4me_pwa.api.get_receipt_list",
        params={"page_size": 3},
    )
    add(
        "get_pending_sales_order_items",
        "GET",
        "/api/method/basket4me_pwa.api.get_pending_sales_order_items",
        params={"customer": TEST_CUSTOMER},
    )

    # ── Reporting
    add(
        "get_transaction_history",
        "GET",
        "/api/method/basket4me_pwa.api.get_transaction_history",
        params={"customer": TEST_CUSTOMER, "page_size": 5},
    )
    add(
        "get_sales_team_override_status",
        "GET",
        "/api/method/basket4me_pwa.api.get_sales_team_override_status",
    )
    add(
        "check_basket4me_settings_status",
        "GET",
        "/api/method/basket4me_pwa.api.check_basket4me_settings_status",
    )

    # ── cost_center coverage — list endpoints must EXPOSE the keys (values may
    # be None for legacy rows; we just verify the key is present so the
    # frontend can rely on the shape).
    def _list_has_cc_keys(rows_key: str):
        def validator(c, b):
            rows = (b.get("data") or {}).get(rows_key) or []
            if not rows:
                return True, "no rows (skipped)"
            missing = [k for k in ("cost_center", "cost_center_name") if k not in rows[0]]
            if missing:
                return False, f"missing keys: {missing}"
            return True, f"{len(rows)} rows OK"
        return validator

    add(
        "get_cost_center_list (new endpoint)",
        "GET",
        "/api/method/basket4me_pwa.api.get_cost_center_list",
        params={"page_size": 5},
        validator=lambda c, b: (
            isinstance((b.get("data") or {}).get("cost_centers"), list),
            "data.cost_centers missing",
        ),
    )
    add(
        "get_invoice_list — cost_center / cost_center_name keys",
        "GET",
        "/api/method/basket4me_pwa.api.get_invoice_list",
        params={"page_size": 3},
        validator=lambda c, b: _list_has_cc_keys("invoices")(c, b),
    )
    add(
        "get_sales_order_list — cost_center / cost_center_name keys",
        "GET",
        "/api/method/basket4me_pwa.api.get_sales_order_list",
        params={"page_size": 3},
        validator=lambda c, b: _list_has_cc_keys("sales_orders")(c, b),
    )
    add(
        "get_receipt_list — cost_center / cost_center_name keys",
        "GET",
        "/api/method/basket4me_pwa.api.get_receipt_list",
        params={"page_size": 3},
        validator=lambda c, b: _list_has_cc_keys("receipts")(c, b),
    )
    add(
        "get_delivery_note_list — cost_center / cost_center_name keys",
        "GET",
        "/api/method/basket4me_pwa.api.get_delivery_note_list",
        params={"limit_page_length": 3},
        validator=lambda c, b: _list_has_cc_keys("delivery_notes")(c, b),
    )
    add(
        "get_return_invoice_list — cost_center / cost_center_name keys",
        "GET",
        "/api/method/basket4me_pwa.api.get_return_invoice_list",
        params={"page_size": 3},
        validator=lambda c, b: _list_has_cc_keys("invoices")(c, b),
    )

    return checks


# ── Runner ──────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"{YELLOW}Smoke test against {BASE_URL} as {SITE_USR}{RESET}\n")
    t0 = time.time()
    try:
        token = login()
    except SystemExit as e:
        print(str(e))
        return 1
    print(f"{GREEN}✓{RESET} login")

    checks = make_checks(token)
    passed = failed = 0
    fail_names = []
    for name, fn in checks:
        start = time.time()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"exception: {e}"
        ms = int((time.time() - start) * 1000)
        mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        print(f"{mark} {name:50s} {ms:5d}ms  {detail}")
        if ok:
            passed += 1
        else:
            failed += 1
            fail_names.append(name)

    total = passed + failed
    elapsed = time.time() - t0
    print()
    print(f"  Total: {total}   Pass: {GREEN}{passed}{RESET}   Fail: {RED}{failed}{RESET}   Time: {elapsed:.1f}s")
    if fail_names:
        print(f"  Failures: {', '.join(fail_names)}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
