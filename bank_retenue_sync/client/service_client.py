"""Thin outbound client to the tej-bank-service microservice.

Reads its endpoint and bearer token from the "Bank Retenue Sync Settings" Single
DocType (never hard-coded — cf. HANDOFF.md). This is the minimal, real client used
to prove app-in-dev-mode -> tej-bank-service connectivity; request logging through
the `BRS Request Log` DocType is added later (Phase B), so for now we log through
`frappe.logger`.
"""

import requests

import frappe
from frappe import _

# The service worker is single-threaded and Playwright-backed: a sync kick returns
# a job id immediately, but the connection itself should not hang the web worker.
DEFAULT_TIMEOUT = 30

logger = frappe.logger("bank_retenue_sync")


class ServiceNotConfigured(frappe.ValidationError):
    pass


def get_settings():
    """Return the cached Settings Single. Caller uses `.get_password(...)` for secrets."""
    return frappe.get_cached_doc("Bank Retenue Sync Settings")


def _base_url() -> str:
    settings = get_settings()
    if not settings.enabled:
        frappe.throw(_("La synchronisation Bank Retenue est desactivee."), ServiceNotConfigured)
    if not settings.service_url:
        frappe.throw(_("service_url n'est pas configure dans les Settings."), ServiceNotConfigured)
    return settings.service_url.rstrip("/")


def _headers() -> dict:
    """Bearer auth. The token must match SERVICE_TOKEN on the container side."""
    token = get_settings().get_password("service_token", raise_exception=False)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(method: str, path: str, *, json: dict | None = None, timeout: int = DEFAULT_TIMEOUT):
    url = f"{_base_url()}{path}"
    logger.info(f"tej-bank-service {method} {path}")
    try:
        res = requests.request(method, url, headers=_headers(), json=json, timeout=timeout)
    except requests.RequestException as e:
        logger.error(f"tej-bank-service {method} {path} -> {e}")
        frappe.throw(_("Appel a tej-bank-service echoue : {0}").format(e))

    if res.status_code >= 400:
        logger.error(f"tej-bank-service {method} {path} -> HTTP {res.status_code}: {res.text[:500]}")
        frappe.throw(
            _("tej-bank-service a repondu HTTP {0} : {1}").format(res.status_code, res.text[:500])
        )

    logger.info(f"tej-bank-service {method} {path} -> HTTP {res.status_code}")
    return res


def health() -> dict:
    """GET /health — proves the app can reach the service devcontainer."""
    return _request("GET", "/health").json()


# `trigger_tej_sync` a ete retiree en 08/2026 : elle appelait `POST /jobs/tej/sync`, une route qui
# n'existe plus dans l'openapi du service, et personne ne l'appelait. Un code mort qui pointe une
# route disparue est pire qu'absent — il laisse croire que le flux existe. Les certificats de
# retenue passent desormais par `bank_retenue_sync/tej/certificats.py`, en mode pull, comme les
# quatre autres flux (`POST /jobs/tej/certificats-recus/export` puis lecture de l'export).


@frappe.whitelist()
def ping() -> dict:
    """Desk/console-callable connectivity check against the configured service."""
    return health()
