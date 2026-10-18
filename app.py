
import os
import re
import json
import time
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Set this to your personalized API URL, keeping its email,
# quizSign and questionId parameters, but removing path and page.
API_BASE = os.environ.get("LEDGER_API_BASE", "").rstrip("?&")

CACHE_SECONDS = 300
_cache = {"loaded_at": 0, "orders": None, "rates": None}
_cache_lock = threading.Lock()




def fetch_url(url, params=None):
    response = requests.get(url, params=params, timeout=8)
    response.raise_for_status()
    return response.json()


def get_api_index():
    if not API_BASE:
        raise RuntimeError("LEDGER_API_BASE is not configured")
    return fetch_url(API_BASE)


def fetch_data(path, page=None, index=None):
    # Reuse the API index when one is supplied.
    if index is None:
        index = get_api_index()

    url = index.get("links", {}).get(path)
    if not url:
        raise ValueError(f"API index has no link for {path}")

    params = {"page": page} if page is not None else None
    return fetch_url(url, params=params)


def fetch_page(page, index=None):
    return fetch_data("orders", page, index=index)


def load_ledger():
    """Download ledger pages, deduplicate orders, and cache currency rates."""
    with _cache_lock:
        now = time.time()

        if (
            _cache["orders"] is not None
            and now - _cache["loaded_at"] < CACHE_SECONDS
        ):
            return _cache["orders"], _cache["rates"]

        # Fetch the API index only once for this load.
        index = get_api_index()

        first = fetch_page(1, index=index)
        total_pages = int(first.get("pages", 1))
        all_orders = list(first.get("orders", []))

        if total_pages > 1:
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [
                    executor.submit(fetch_page, page, index)
                    for page in range(2, total_pages + 1)
                ]

                for future in as_completed(futures):
                    data = future.result()
                    all_orders.extend(data.get("orders", []))

        # Keep the latest updated version of each order.
        latest = {}

        for order in all_orders:
            order_id = order.get("id")
            if not order_id:
                continue

            previous = latest.get(order_id)

            if previous is None or str(order.get("updated_at", "")) > str(
                previous.get("updated_at", "")
            ):
                latest[order_id] = order

        rates_data = fetch_data("rates", index=index)
        rates = rates_data["usd_per_unit"]
        orders = list(latest.values())

        _cache.update({
            "loaded_at": time.time(),
            "orders": orders,
            "rates": rates,
        })

        app.logger.info(
            "Loaded %s records; %s unique orders",
            len(all_orders),
            len(orders),
        )
        return orders, rates
    
def amount_usd(order, rates):
    currency = str(order.get("currency", "USD")).upper()
    rate = rates.get(currency)
    if rate is None:
        raise ValueError(f"Missing exchange rate for {currency}")
    return float(order.get("amount", 0)) * float(rate)


def paid_orders(orders):
    return [o for o in orders if str(o.get("status", "")).lower() == "paid"]


def parse_month(question):
    """Recognize month names and YYYY-MM."""
    q = question.lower()
    match = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", q)
    if match:
        return match.group(0)

    months = {
        "january": "01", "jan": "01",
        "february": "02", "feb": "02",
        "march": "03", "mar": "03",
        "april": "04", "apr": "04",
        "may": "05",
        "june": "06", "jun": "06",
        "july": "07", "jul": "07",
        "august": "08", "aug": "08",
        "september": "09", "sep": "09", "sept": "09",
        "october": "10", "oct": "10",
        "november": "11", "nov": "11",
        "december": "12", "dec": "12",
    }

    for name, number in months.items():
        if re.search(r"\b" + re.escape(name) + r"\b", q):
            year = re.search(r"\b20\d{2}\b", q)
            if year:
                return f"{year.group(0)}-{number}"

    return None




def answer_question(question, orders, rates):
    q = question.lower().strip()
    month = parse_month(q)

    # Apply date, region, product, and customer filters to ALL orders.
    filtered = list(orders)

    if month:
        filtered = [
            o for o in filtered
            if str(o.get("created_at", "")).startswith(month)
        ]

    regions = sorted({
        str(o.get("region", ""))
        for o in orders if o.get("region")
    })
    products = sorted({
        str(o.get("product", ""))
        for o in orders if o.get("product")
    })
    customers = sorted({
        str(o.get("customer", ""))
        for o in orders if o.get("customer")
    })

    selected_region = next(
        (r for r in regions if r.lower() in q), None
    )
    selected_product = next(
        (p for p in products if p.lower() in q), None
    )
    selected_customer = next(
        (c for c in customers if c.lower() in q), None
    )

    if selected_region:
        filtered = [
            o for o in filtered
            if str(o.get("region", "")).lower()
            == selected_region.lower()
        ]

    if selected_product:
        filtered = [
            o for o in filtered
            if str(o.get("product", "")).lower()
            == selected_product.lower()
        ]

    if selected_customer:
        filtered = [
            o for o in filtered
            if str(o.get("customer", "")).lower()
            == selected_customer.lower()
        ]

    # Detect requested status, including refunded orders.
    status_aliases = {
        "paid": ("paid",),
        "pending": ("pending",),
        "cancelled": ("cancelled", "canceled"),
        "void": ("void",),
        "refunded": ("refunded", "refund"),
    }

    selected_status = None
    for status, aliases in status_aliases.items():
        if any(re.search(r"\b" + re.escape(a) + r"\b", q)
               for a in aliases):
            selected_status = status
            break

    if selected_status:
        accepted_statuses = status_aliases[selected_status]
        filtered = [
            o for o in filtered
            if str(o.get("status", "")).lower()
            in accepted_statuses
        ]

    # For revenue, only paid orders count.
    paid = paid_orders(orders)
    paid_filtered = [
        o for o in filtered
        if str(o.get("status", "")).lower() == "paid"
    ]

    # Specific order lookup.
    order_id = re.search(r"\bORD-\d+\b", question, re.I)
    if order_id:
        found = [
            o for o in orders
            if str(o.get("id", "")).upper()
            == order_id.group(0).upper()
        ]
        return {"answer": found[0] if found else None}

    # Distinct customer count must respect all requested filters.
    if any(word in q for word in ("how many", "count", "number of")):
        if "customer" in q:
            matching_customers = {
                o.get("customer")
                for o in filtered
                if o.get("customer")
            }
            return {"answer": len(matching_customers)}

        if "product" in q and any(
            word in q for word in ("different", "distinct", "unique")
        ):
            matching_products = {
                o.get("product")
                for o in filtered
                if o.get("product")
            }
            return {"answer": len(matching_products)}

        return {"answer": len(filtered)}

    # Revenue and sales questions.
    if any(word in q for word in (
        "revenue", "sales", "turnover", "earned"
    )):
        total = sum(amount_usd(o, rates) for o in paid_filtered)
        return {"answer": round(total, 2), "currency": "USD"}

    # Average order value.
    if "average" in q or "avg" in q:
        if not paid_filtered:
            return {"answer": 0, "currency": "USD"}

        total = sum(amount_usd(o, rates) for o in paid_filtered)
        return {
            "answer": round(total / len(paid_filtered), 2),
            "currency": "USD",
        }

    # Top products by quantity.
    if "product" in q and any(
        word in q for word in ("top", "most", "best", "popular")
    ):
        totals = {}
        for o in paid_filtered:
            name = o.get("product", "Unknown")
            totals[name] = totals.get(name, 0) + int(o.get("qty", 0))

        ranked = sorted(
            totals.items(), key=lambda item: item[1], reverse=True
        )
        return {
            "answer": [
                {"product": name, "quantity": quantity}
                for name, quantity in ranked[:5]
            ]
        }

    # Regional revenue ranking.
    if "region" in q and any(
        word in q for word in ("top", "most", "best", "highest")
    ):
        totals = {}
        for o in paid:
            region = o.get("region", "Unknown")
            totals[region] = (
                totals.get(region, 0) + amount_usd(o, rates)
            )

        ranked = sorted(
            totals.items(), key=lambda item: item[1], reverse=True
        )
        return {
            "answer": [
                {"region": name, "revenue_usd": round(value, 2)}
                for name, value in ranked
            ]
        }

    # Status count, including refunded.
    if selected_status:
        return {
            "answer": len(filtered),
            "status": selected_status,
        }

    # General summary.
    if any(word in q for word in (
    "summary", "overview", "total orders"
    )):
        revenue = sum(amount_usd(o, rates) for o in paid)
        return {
            "total_orders": len(orders),
            "paid_orders": len(paid),
            "revenue_usd": round(revenue, 2),
            "products": len(products),
            "customers": len(customers),
        }

    return {
        "error": "I could not interpret that question.",
        "supported_queries": [
            "Number of orders by status, region, product, or month",
            "Number of distinct customers matching filters",
            "Total paid revenue in USD",
            "Average order value",
            "Top products by quantity",
            "Revenue by region",
            "Details for an order ID",
        ],
    }


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "endpoint": "/answer"})


@app.route("/answer", methods=["POST"])
def answer():
    payload = request.get_json(silent=True) or {}
    question = payload.get("question", "")

    if not isinstance(question, str) or not question.strip():
        return jsonify({"error": "Missing question"}), 400

    try:
        orders, rates = load_ledger()
        result = answer_question(question, orders, rates)
        return jsonify(result)

    except requests.RequestException:
        app.logger.exception("Ledger API request failed")
        return jsonify({"error": "Unable to retrieve ledger data"}), 502

    except (ValueError, KeyError, TypeError):
        app.logger.exception("Ledger processing failed")
        return jsonify({"error": "Unable to process ledger data"}), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
    )