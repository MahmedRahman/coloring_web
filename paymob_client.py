"""Paymob Intention API helpers (Egypt Accept)."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Dict, List

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PAYMOB_BASE_URL = os.environ.get("PAYMOB_BASE_URL", "https://accept.paymob.com").rstrip("/")
PAYMOB_SECRET_KEY = os.environ.get("PAYMOB_SECRET_KEY", "")
PAYMOB_PUBLIC_KEY = os.environ.get("PAYMOB_PUBLIC_KEY", "")
PAYMOB_HMAC_SECRET = os.environ.get("PAYMOB_HMAC_SECRET", "")
PAYMOB_API_KEY = os.environ.get("PAYMOB_API_KEY", "")
PAYMOB_INTEGRATION_ID = os.environ.get("PAYMOB_INTEGRATION_ID", "").strip()
PAYMOB_INTEGRATION_ID_WALLET = os.environ.get("PAYMOB_INTEGRATION_ID_WALLET", "").strip()
# Optional comma-separated override, e.g. "5791155,1234567"
PAYMOB_INTEGRATION_IDS = os.environ.get("PAYMOB_INTEGRATION_IDS", "").strip()

BOOK_PACK_PRICE_EGP = float(os.environ.get("BOOK_PACK_PRICE_EGP", "49"))
BOOK_PACK_CREDITS = int(os.environ.get("BOOK_PACK_CREDITS", "10"))


def paymob_configured() -> bool:
    return bool(PAYMOB_SECRET_KEY and PAYMOB_PUBLIC_KEY and PAYMOB_HMAC_SECRET)


def wallet_enabled() -> bool:
    return bool(PAYMOB_INTEGRATION_ID_WALLET or (
        PAYMOB_INTEGRATION_IDS and len([x for x in PAYMOB_INTEGRATION_IDS.split(",") if x.strip()]) > 1
    ))


def amount_cents() -> int:
    return int(round(BOOK_PACK_PRICE_EGP * 100))


def _paymob_str(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _as_method_id(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def payment_methods(preferred: str = "all") -> List[Any]:
    """
    Card Integration IDs for Intention / Unified Checkout.
    Wallet (CASH) is NOT supported by Intention API — use pay_with_wallet_classic().
    """
    preferred = (preferred or "all").strip().lower()
    methods: List[Any] = []

    card = _as_method_id(PAYMOB_INTEGRATION_ID) if PAYMOB_INTEGRATION_ID else None
    if PAYMOB_INTEGRATION_IDS:
        # Only keep the first/card-like IDs listed; wallet handled separately
        for part in PAYMOB_INTEGRATION_IDS.split(","):
            mid = _as_method_id(part)
            if mid is not None and str(mid) != str(PAYMOB_INTEGRATION_ID_WALLET):
                methods.append(mid)
    elif card is not None and preferred in ("all", "card"):
        methods.append(card)

    seen = set()
    uniq = []
    for m in methods:
        key = str(m)
        if key not in seen:
            seen.add(key)
            uniq.append(m)
    if uniq:
        return uniq
    return ["card"]


def normalize_egypt_phone(phone: str) -> str:
    """Normalize local numbers to +20… for Paymob wallet/card billing."""
    raw = (phone or "").strip().replace(" ", "").replace("-", "")
    if not raw:
        return "+201000000000"
    if raw.startswith("+20"):
        return raw
    if raw.startswith("0020"):
        return "+" + raw[2:]
    if raw.startswith("20") and len(raw) >= 12:
        return "+" + raw
    if raw.startswith("0") and len(raw) == 11:
        return "+20" + raw[1:]
    if raw.startswith("1") and len(raw) == 10:
        return "+20" + raw
    return raw


def local_wallet_phone(phone: str) -> str:
    """Wallet API expects 01xxxxxxxxx (local), not +20."""
    raw = (phone or "").strip().replace(" ", "").replace("-", "")
    if raw.startswith("+20"):
        raw = "0" + raw[3:]
    elif raw.startswith("0020"):
        raw = "0" + raw[4:]
    elif raw.startswith("20") and len(raw) >= 12:
        raw = "0" + raw[2:]
    return raw


def _auth_token() -> str:
    if not PAYMOB_API_KEY:
        raise RuntimeError("PAYMOB_API_KEY مطلوب لدفع المحفظة.")
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            f"{PAYMOB_BASE_URL}/api/auth/tokens",
            json={"api_key": PAYMOB_API_KEY},
        )
        r.raise_for_status()
        token = r.json().get("token")
        if not token:
            raise RuntimeError("Paymob مرجوعش auth token.")
        return token


def pay_with_wallet_classic(
    *,
    special_reference: str,
    phone: str,
    customer: Dict[str, str],
    redirection_url: str,
) -> dict:
    """
    Classic Accept API for CASH / mobile wallet integrations.
    Intention API rejects wallet (CASH) IDs — this path is required.
    """
    if not PAYMOB_INTEGRATION_ID_WALLET:
        raise RuntimeError("مفيش Wallet Integration ID متضبط.")

    wallet_id = int(PAYMOB_INTEGRATION_ID_WALLET)
    cents = amount_cents()
    phone_local = local_wallet_phone(phone)
    phone_intl = normalize_egypt_phone(phone)
    token = _auth_token()

    with httpx.Client(timeout=45.0) as client:
        order_res = client.post(
            f"{PAYMOB_BASE_URL}/api/ecommerce/orders",
            json={
                "auth_token": token,
                "delivery_needed": False,
                "amount_cents": cents,
                "currency": "EGP",
                "merchant_order_id": special_reference,
                "items": [],
            },
        )
        order_res.raise_for_status()
        order = order_res.json()
        order_id = order.get("id")
        if not order_id:
            raise RuntimeError(f"فشل إنشاء طلب المحفظة: {order}")

        key_res = client.post(
            f"{PAYMOB_BASE_URL}/api/acceptance/payment_keys",
            json={
                "auth_token": token,
                "amount_cents": cents,
                "expiration": 3600,
                "order_id": order_id,
                "billing_data": {
                    "apartment": "NA",
                    "email": customer.get("email") or "customer@example.com",
                    "floor": "NA",
                    "first_name": customer.get("first_name") or "Customer",
                    "street": "NA",
                    "building": "NA",
                    "phone_number": phone_local,
                    "shipping_method": "NA",
                    "postal_code": "NA",
                    "city": "Cairo",
                    "country": "EG",
                    "last_name": customer.get("last_name") or "User",
                    "state": "Cairo",
                },
                "currency": "EGP",
                "integration_id": wallet_id,
                "lock_order_when_paid": True,
                "redirection_url": redirection_url,
            },
        )
        key_res.raise_for_status()
        payment_token = key_res.json().get("token")
        if not payment_token:
            raise RuntimeError("فشل إنشاء payment key للمحفظة.")

        pay_res = client.post(
            f"{PAYMOB_BASE_URL}/api/acceptance/payments/pay",
            json={
                "source": {
                    "identifier": phone_local,
                    "subtype": "WALLET",
                },
                "payment_token": payment_token,
            },
        )
        # Paymob may return 200 with success=false
        try:
            pay = pay_res.json()
        except Exception:
            raise RuntimeError(f"رد المحفظة مش مفهوم: {pay_res.text[:300]}")

    success = str(pay.get("success", "")).lower() in ("true", "1")
    pending = str(pay.get("pending", "")).lower() in ("true", "1")
    message = (
        pay.get("data.message")
        or (pay.get("data") or {}).get("message")
        or pay.get("message")
        or ""
    )
    redirect = pay.get("redirect_url") or pay.get("redirection_url") or ""

    return {
        "raw": pay,
        "success": success,
        "pending": pending,
        "message": message,
        "redirect_url": redirect,
        "order_id": str(order_id),
        "txn_id": str(pay.get("id") or ""),
        "phone": phone_local,
        "phone_intl": phone_intl,
    }


def create_intention(
    *,
    special_reference: str,
    customer: Dict[str, str],
    notification_url: str,
    redirection_url: str,
    preferred_method: str = "all",
) -> dict:
    if not paymob_configured():
        raise RuntimeError("Paymob مش متضبط — حط المفاتيح في البيئة.")

    cents = amount_cents()
    phone = normalize_egypt_phone(customer.get("phone") or "")
    methods = payment_methods(preferred_method)
    payload = {
        "amount": cents,
        "currency": "EGP",
        "payment_methods": methods,
        "items": [
            {
                "name": f"باقة {BOOK_PACK_CREDITS} كتب تلوين",
                "amount": cents,
                "description": f"{BOOK_PACK_CREDITS} extra coloring books",
                "quantity": 1,
            }
        ],
        "special_reference": special_reference,
        "billing_data": {
            "first_name": customer.get("first_name") or "Customer",
            "last_name": customer.get("last_name") or "User",
            "email": customer.get("email") or "customer@example.com",
            "phone_number": phone,
            "apartment": "NA",
            "floor": "NA",
            "street": "NA",
            "building": "NA",
            "shipping_method": "NA",
            "postal_code": "NA",
            "city": "Cairo",
            "state": "Cairo",
            "country": "EGY",
        },
        "customer": {
            "first_name": customer.get("first_name") or "Customer",
            "last_name": customer.get("last_name") or "User",
            "email": customer.get("email") or "customer@example.com",
        },
        "notification_url": notification_url,
        "redirection_url": redirection_url,
        "extras": {
            "product": "book_pack",
            "credits": BOOK_PACK_CREDITS,
            "preferred_method": preferred_method,
        },
    }

    headers = {
        "Authorization": f"Token {PAYMOB_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30.0) as client:
        r = client.post(f"{PAYMOB_BASE_URL}/v1/intention/", json=payload, headers=headers)
        if r.status_code >= 400:
            detail = r.text
            try:
                detail = r.json()
            except Exception:
                pass
            raise RuntimeError(f"Paymob intention failed ({r.status_code}): {detail}")
        return r.json()


def checkout_url(client_secret: str) -> str:
    return (
        f"{PAYMOB_BASE_URL}/unifiedcheckout/"
        f"?publicKey={PAYMOB_PUBLIC_KEY}&clientSecret={client_secret}"
    )


def verify_transaction_post_hmac(obj: dict, received_hmac: str) -> bool:
    if not PAYMOB_HMAC_SECRET or not received_hmac:
        return False
    try:
        fields = [
            obj["amount_cents"],
            obj["created_at"],
            obj["currency"],
            obj["error_occured"],
            obj["has_parent_transaction"],
            obj["id"],
            obj["integration_id"],
            obj["is_3d_secure"],
            obj["is_auth"],
            obj["is_capture"],
            obj["is_refunded"],
            obj["is_standalone_payment"],
            obj["is_voided"],
            obj["order"]["id"],
            obj["owner"],
            obj["pending"],
            obj["source_data"]["pan"],
            obj["source_data"]["sub_type"],
            obj["source_data"]["type"],
            obj["success"],
        ]
    except (KeyError, TypeError):
        return False
    concat = "".join(_paymob_str(f) for f in fields)
    computed = hmac.new(
        PAYMOB_HMAC_SECRET.encode(),
        concat.encode(),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(computed, received_hmac.lower())


def verify_redirect_hmac(args: Dict[str, str]) -> bool:
    """Verify Paymob GET redirect hmac (flattened query params)."""
    received = (args.get("hmac") or "").lower()
    if not PAYMOB_HMAC_SECRET or not received:
        return False

    def _get(*keys: str) -> str:
        for k in keys:
            if k in args and args[k] is not None and args[k] != "":
                return str(args[k])
        return ""

    # Paymob redirect uses dotted source_data.* keys (not underscores)
    values = [
        _get("amount_cents"),
        _get("created_at"),
        _get("currency"),
        _get("error_occured"),
        _get("has_parent_transaction"),
        _get("id"),
        _get("integration_id"),
        _get("is_3d_secure"),
        _get("is_auth"),
        _get("is_capture"),
        _get("is_refunded"),
        _get("is_standalone_payment"),
        _get("is_voided"),
        _get("order"),
        _get("owner"),
        _get("pending"),
        _get("source_data.pan", "source_data_pan"),
        _get("source_data.sub_type", "source_data_sub_type"),
        _get("source_data.type", "source_data_type"),
        _get("success"),
    ]
    concat = "".join(values)
    computed = hmac.new(
        PAYMOB_HMAC_SECRET.encode(),
        concat.encode(),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(computed, received)
