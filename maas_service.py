"""
Single-file Python FastAPI service implementing the Java ``MaasServiceApi``
contract (Feign client) for MOSMETRO integration.

Endpoints implemented:
    POST /user/info        -> UserInfoResponse
    POST /user/register    -> UserRegistrationResponse
    POST /user/key         -> UserKeyUpdateResponse  (returns /payment/html url)
    POST /cp/activate      -> 204 No Content
    POST /cp/deactivate    -> 204 No Content
    POST /pay/status       -> PaymentStatusResponse
    GET  /payment/html     -> HTML page with success / cancel / decline buttons

User data (phone, city_plus_enabled, card info) is persisted in
``users.yaml`` next to this script.
"""

from __future__ import annotations

import os
import random
import threading
import uuid
from typing import Any, Dict, Optional

import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ConfigDict

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_YAML_PATH = os.path.join(BASE_DIR, "users.yaml")

# Base URL used to build absolute payment HTML links. Override via env var
# when the service sits behind a tunnel/proxy (e.g. ngrok) so the response
# does not contain the tunnel hostname.
BASE_URL = os.environ.get("MAAS_BASE_URL", "").rstrip("/")


def _base_url(http_request: Optional[Request] = None) -> str:
    if BASE_URL:
        return BASE_URL
    if http_request is not None:
        return str(http_request.base_url).rstrip("/")
    return ""

# ---------------------------------------------------------------------------
# YAML persistence (thread-safe)
# ---------------------------------------------------------------------------
_lock = threading.Lock()


def _load_yaml() -> Dict[str, Any]:
    if not os.path.exists(USERS_YAML_PATH):
        return {"users": {}, "payments": {}}
    with open(USERS_YAML_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        data = {}
    # Be defensive: keys may exist with null values (e.g. users: null).
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    if not isinstance(data.get("payments"), dict):
        data["payments"] = {}
    return data


def _save_yaml(data: Dict[str, Any]) -> None:
    with open(USERS_YAML_PATH, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)


def get_users() -> Dict[str, Any]:
    with _lock:
        return _load_yaml()


def save_users(data: Dict[str, Any]) -> None:
    with _lock:
        _save_yaml(data)


def _mark_payment_success(payment_id: str) -> None:
    """Background callback: transition a payment to SUCCESS after a delay
    and populate the linked user's card info."""
    with _lock:
        data = _load_yaml()
        payment = data["payments"].get(payment_id)
        if not payment:
            return

        maas_user_id = payment.get("maas_user_id")
        user = data["users"].get(maas_user_id) if maas_user_id else None

        if user:
            ci = user.setdefault("card_info", {})
            ci["hash_key"] = str(uuid.uuid4())
            ci["pay_system"] = "MC"
            ci["masked_pan"] = "".join(random.choice("0123456789") for _ in range(4))
            ci["card_uid"] = str(uuid.uuid4())
            # Preserve any card id / instrument type captured during /user/key.
            if "card_id" not in ci:
                ci.pop("card_id", None)

        payment["status"] = "SUCCESS"
        auth = payment.get("auth") or {}
        auth_status = auth.get("status") or {}
        auth_status["status"] = "SUCCESS"
        auth["status"] = auth_status
        payment["auth"] = auth
        _save_yaml(data)


# ---------------------------------------------------------------------------
# Pydantic DTOs (JSON keys mirror the Jackson @JsonProperty names in Java)
# ---------------------------------------------------------------------------
class RedirectUrls(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    success_url: Optional[str] = Field(default=None, alias="succeed")
    declined_url: Optional[str] = Field(default=None, alias="declined")
    canceled_url: Optional[str] = Field(default=None, alias="canceled")


class PayData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    transaction_id: Optional[str] = Field(default=None, alias="tran_id")
    price: Optional[float] = Field(default=None, alias="price")
    payment_method: Optional[str] = Field(default=None, alias="payment_method")
    redirect_urls: Optional[RedirectUrls] = Field(
        default=None, alias="redirect_url"
    )
    payment_token: Optional[str] = Field(default=None, alias="payment_token")
    card_id: Optional[str] = Field(default=None, alias="id")


class UserInfoRequest(BaseModel):
    maas_user_id: str


class UserInfoResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="ACTIVE", alias="status")
    city_plus_option: Optional[bool] = Field(default=None, alias="city_plus_option")
    known_mm_lkp_user_id: Optional[bool] = Field(
        default=None, alias="known_mm_lkp_user_id"
    )
    key_change_limit: Optional[int] = Field(default=None, alias="key_change_limit")
    key_change_left: Optional[int] = Field(default=None, alias="key_change_left")
    reason: Optional[Any] = Field(default=None, alias="reason")
    hash_key: Optional[str] = Field(default=None, alias="hash_key")
    key_biometry_option: Optional[bool] = Field(
        default=None, alias="key_biometry_option"
    )
    type: Optional[str] = Field(default=None, alias="type")
    pay_system: Optional[str] = Field(default=None, alias="pay_system")
    masked_pan: Optional[str] = Field(default=None, alias="masked_pan")
    card_uid: Optional[str] = Field(default=None, alias="card_uid")
    payg_id: Optional[str] = Field(default=None, alias="payg_id")
    subscription_id: Optional[str] = Field(default=None, alias="subscription_id")


class UserRegistrationRequest(BaseModel):
    phone: str


class UserRegistrationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    maas_user_id: str = Field(alias="maas_user_id")
    known_mm_lkp_user_id: str = Field(
        default="false", alias="known_mm_lkp_user_id"
    )


class UserKeyUpdateRequest(BaseModel):
    maas_user_id: str
    pay_data: Optional[PayData] = None


class PaymentAuthInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: Optional[Any] = Field(default=None, alias="status")
    date: Optional[str] = Field(default=None, alias="date")
    rnn: Optional[str] = Field(default=None, alias="rnn")
    authorization_code: Optional[str] = Field(default=None, alias="code")
    card_info: Optional[Any] = Field(default=None, alias="card")
    receipt_urls: Optional[list] = Field(default=None, alias="receipt_urls")


class PaymentInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    url: Optional[str] = Field(default=None, alias="url")
    auth: Optional[Any] = Field(default=None, alias="auth")


class UserKeyUpdateResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    payment_id: str = Field(alias="payment_id")
    payment: PaymentInfo = Field(alias="payment")


class CityPlusActivationRequest(BaseModel):
    maas_user_id: str


class CityPlusDeactivationRequest(BaseModel):
    maas_user_id: str


class PaymentStatusRequest(BaseModel):
    payment_id: str


class PaymentStatusResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    payment: PaymentInfo = Field(alias="payment")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="MaasServiceApi (Python FastAPI stub)")


def _user_not_found(maas_user_id: str):
    raise HTTPException(status_code=404, detail=f"user {maas_user_id} not found")


def _payment_not_found(payment_id: str):
    raise HTTPException(status_code=404, detail=f"payment {payment_id} not found")


# ---------------------------------------------------------------------------
# POST /user/info
# ---------------------------------------------------------------------------
@app.post("/user/info", response_model=UserInfoResponse)
def get_user_info(request: UserInfoRequest):
    data = get_users()
    user = data["users"].get(request.maas_user_id)
    if not user:
        _user_not_found(request.maas_user_id)

    card_info = user.get("card_info") or {}
    return UserInfoResponse(
        status="ACTIVE",
        city_plus_option=user.get("city_plus_enabled", False),
        known_mm_lkp_user_id=user.get("known_mm_lkp_user_id", False),
        key_change_limit=user.get("key_change_limit"),
        key_change_left=user.get("key_change_left"),
        reason=None,
        hash_key=card_info.get("hash_key"),
        key_biometry_option=user.get("key_biometry_option", False),
        type=card_info.get("type"),
        pay_system=card_info.get("pay_system"),
        masked_pan=card_info.get("masked_pan"),
        card_uid=card_info.get("card_uid"),
        payg_id=card_info.get("payg_id"),
        subscription_id=card_info.get("subscription_id"),
    )


# ---------------------------------------------------------------------------
# POST /user/register
# ---------------------------------------------------------------------------
@app.post("/user/register", response_model=UserRegistrationResponse)
def register_user(request: UserRegistrationRequest):
    data = get_users()

    # Return existing user if one with the same phone already exists.
    for maas_user_id, user in data["users"].items():
        if user.get("phone") == request.phone:
            return UserRegistrationResponse(
                maas_user_id=maas_user_id,
                known_mm_lkp_user_id="false",
            )

    maas_user_id = str(uuid.uuid4())
    data["users"][maas_user_id] = {
        "phone": request.phone,
        "city_plus_enabled": False,
        "known_mm_lkp_user_id": False,
        "key_change_limit": 5,
        "key_change_left": 5,
        "key_biometry_option": False,
        "card_info": {},
    }
    save_users(data)
    return UserRegistrationResponse(
        maas_user_id=maas_user_id,
        known_mm_lkp_user_id="false",
    )


# ---------------------------------------------------------------------------
# POST /user/key
# ---------------------------------------------------------------------------
@app.post("/user/key", response_model=UserKeyUpdateResponse)
def update_key_card(http_request: Request, request: UserKeyUpdateRequest):
    data = get_users()
    user = data["users"].get(request.maas_user_id)
    if not user:
        _user_not_found(request.maas_user_id)

    pay_data = request.pay_data

    # Persist card id into user card_info if provided.
    if pay_data and pay_data.card_id:
        user.setdefault("card_info", {})["card_id"] = pay_data.card_id

    payment_id = str(uuid.uuid4())
    redirect_urls = pay_data.redirect_urls if pay_data else None

    data["payments"][payment_id] = {
        "maas_user_id": request.maas_user_id,
        "redirect_urls": (
            redirect_urls.model_dump(by_alias=True, exclude_none=True)
            if redirect_urls
            else {}
        ),
        "status": "CREATED",
        "auth": {
            "status": {"status": "CREATED", "response_code": None, "response_descr": None},
            "date": None,
            "code": None,
            "card": None,
            "receipt_urls": None,
        },
    }

    # Store card info from pay_data if present.
    if pay_data:
        ci = user.setdefault("card_info", {})
        if pay_data.card_id:
            ci["card_id"] = pay_data.card_id
        if pay_data.payment_method:
            ci["type"] = pay_data.payment_method

    save_users(data)

    # Auto-transition payment status to SUCCESS after 5 seconds.
    threading.Timer(5.0, _mark_payment_success, args=[payment_id]).start()

    base_url = _base_url(http_request)
    payment_url = f"{base_url}/payment/html?payment_id={payment_id}"
    return UserKeyUpdateResponse(
        payment_id=payment_id,
        payment=PaymentInfo(url=payment_url, auth=None),
    )


# ---------------------------------------------------------------------------
# POST /cp/activate
# ---------------------------------------------------------------------------
@app.post("/cp/activate", status_code=204)
def activate_city_plus(request: CityPlusActivationRequest):
    data = get_users()
    user = data["users"].get(request.maas_user_id)
    if not user:
        _user_not_found(request.maas_user_id)
    user["city_plus_enabled"] = True
    save_users(data)
    return None


# ---------------------------------------------------------------------------
# POST /cp/deactivate
# ---------------------------------------------------------------------------
@app.post("/cp/deactivate", status_code=204)
def deactivate_city_plus(request: CityPlusDeactivationRequest):
    data = get_users()
    user = data["users"].get(request.maas_user_id)
    if not user:
        _user_not_found(request.maas_user_id)
    user["city_plus_enabled"] = False
    save_users(data)
    return None


# ---------------------------------------------------------------------------
# POST /pay/status
# ---------------------------------------------------------------------------
@app.post("/pay/status", response_model=PaymentStatusResponse)
def get_payment_status(http_request: Request, request: PaymentStatusRequest):
    data = get_users()
    payment = data["payments"].get(request.payment_id)
    if not payment:
        _payment_not_found(request.payment_id)
    base_url = _base_url(http_request)
    return PaymentStatusResponse(
        payment=PaymentInfo(
            url=f"{base_url}/payment/html?payment_id={request.payment_id}",
            auth=payment.get("auth"),
        )
    )


# ---------------------------------------------------------------------------
# GET /payment/html
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Payment</title>
    <style>
        body { font-family: Arial, sans-serif; display: flex; flex-direction: column;
               align-items: center; justify-content: center; height: 100vh;
               margin: 0; background: #f5f5f5; }
        h1 { margin-bottom: 40px; color: #333; }
        .button-group { display: flex; gap: 16px; flex-wrap: wrap; justify-content: center; }
        button { padding: 14px 28px; font-size: 16px; border: none; border-radius: 6px;
                 cursor: pointer; color: #fff; min-width: 140px; }
        .success { background: #28a745; }
        .success:hover { background: #218838; }
        .cancel { background: #6c757d; }
        .cancel:hover { background: #5a6268; }
        .decline { background: #dc3545; }
        .decline:hover { background: #c82333; }
        .note { margin-top: 30px; font-size: 14px; color: #666; }
    </style>
</head>
<body>
    <h1>Payment Action</h1>
    <div class="button-group">
        <button class="success" onclick="location.href='{success_url}'">Success</button>
        <button class="cancel" onclick="location.href='{canceled_url}'">Cancel</button>
        <button class="decline" onclick="location.href='{declined_url}'">Decline</button>
    </div>
    <div class="note">Choose an outcome to redirect.</div>
</body>
</html>
"""


@app.get("/payment/html", response_class=HTMLResponse)
def payment_html(
    request: Request,
    payment_id: Optional[str] = Query(default=None),
):
    data = get_users()
    payment = data["payments"].get(payment_id) if payment_id else None
    if not payment:
        # Fallback page when no payment_id or unknown payment.
        return HTMLResponse(
            content=(
                _HTML_TEMPLATE
                .replace("{success_url}", "/")
                .replace("{canceled_url}", "/")
                .replace("{declined_url}", "/")
            ),
            status_code=404 if payment_id else 200,
        )

    redirect_urls = payment.get("redirect_urls") or {}
    success_url = redirect_urls.get("succeed") or redirect_urls.get("success_url") or "/"
    declined_url = redirect_urls.get("declined") or redirect_urls.get("declined_url") or "/"
    canceled_url = redirect_urls.get("canceled") or redirect_urls.get("canceled_url") or "/"

    html = (
        _HTML_TEMPLATE
        .replace("{success_url}", success_url)
        .replace("{declined_url}", declined_url)
        .replace("{canceled_url}", canceled_url)
    )
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
