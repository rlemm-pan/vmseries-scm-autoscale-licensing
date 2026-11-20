"""Client for the Software NGFW (Flex credit) Licensing API on the Customer Support Portal.

Source: "VM-Series Activation and Onboarding" guide, sections
  - Software NGFW Licensing API (p.102)
  - Get Serial Numbers / Deactivate a VM-Series Firewall Using the API (p.105)
  - Manage / Create / Update Deployment Profiles Using the Licensing API (p.106-117)

All calls pass the OAuth access token in a header literally named ``token``
(not ``Authorization: Bearer``), per the guide's samples.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .http import ApiError, ClientCredentialsToken, HttpTransport

log = logging.getLogger(__name__)

# Token endpoint and request format are not in the activation guide; they come from
# KB "Missing authentication token needed in the API to use with OAuth access"
# (kA14u000000XhzQCAS): form POST with client_id/client_secret in the body.
DEFAULT_TOKEN_URL = "https://identity.paloaltonetworks.com/as/token.oauth2"
DEFAULT_BASE_URL = "https://api.paloaltonetworks.com/tms/v1"
DEFAULT_SCOPE = "fwflex-service"

# Values as they appear in the guide's sample requests / responses. The parameter
# table spells the flex value "10.0.4_or_above"; the samples use "10.0.4_or-above".
PANOS_FLEX = "10.0.4_or-above"
PANOS_FIXED = "10.0.3_or-below"


@dataclass
class DeploymentProfileSpec:
    """Body for POST/PATCH /tms/v1/deploymentProfile."""

    credit_pool_id: int
    name: str
    firewall_quantity: int
    vcpu_quantity: Optional[int] = None  # flexible vCPU model (recommended)
    vm_model: Optional[int] = None  # fixed model, e.g. 700
    pan_os: Optional[str] = None  # derived from model type when omitted
    type: str = "VM"
    vsys_quantity: int = 1
    subscriptions: List[str] = field(default_factory=list)
    panorama: List[str] = field(default_factory=list)
    features: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)  # raw overrides, merged last

    def __post_init__(self) -> None:
        if self.pan_os is None:
            self.pan_os = PANOS_FIXED if self.vm_model else PANOS_FLEX

    def validate(self) -> None:
        if self.firewall_quantity < 1:
            raise ValueError("firewall_quantity must be > 0")
        if self.vm_model is None:
            if self.vcpu_quantity is None or not 1 <= self.vcpu_quantity <= 64:
                raise ValueError("flex profiles need vcpu_quantity between 1 and 64")
        elif self.vm_model not in (50, 100, 300, 500, 700):
            raise ValueError("vm_model must be one of 50, 100, 300, 500, 700")

    def to_body(self) -> Dict[str, Any]:
        self.validate()
        body: Dict[str, Any] = {
            "creditPoolId": self.credit_pool_id,
            "name": self.name,
            "type": self.type,
            "panOS": self.pan_os,
            "firewallQuantity": self.firewall_quantity,
            "vSysQuantity": self.vsys_quantity,
        }
        if self.vcpu_quantity is not None:
            body["vCpuQuantity"] = self.vcpu_quantity
        if self.vm_model is not None:
            body["vmModel"] = self.vm_model
        if self.subscriptions:
            body["subscriptions"] = list(self.subscriptions)
        # Always send panorama, even empty: omitting it makes POST /deploymentProfile return
        # HTTP 500 "ENTFWFLEXUSDIUDIC" (observed 2026-11-19).
        body["panorama"] = list(self.panorama)
        if self.features:
            body["features"] = list(self.features)
        body.update(self.extra)
        return body

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DeploymentProfileSpec":
        allowed = set(cls.__dataclass_fields__)
        unknown = {k for k in d if k not in allowed and not k.startswith("_")}
        if unknown:
            raise ValueError(f"unknown profile spec keys: {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class DeactivateResult:
    auth_code: str
    requested: List[str]
    success: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    failure_reason: List[Any] = field(default_factory=list)
    already_inactive: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        done = set(self.success) | set(self.already_inactive)
        return not self.failed and set(self.requested) <= done


def _raise_embedded_error(method: str, url: str, body: Any) -> Any:
    """The TMS API can return HTTP 200 with an error envelope, e.g.
    {"status":400,"error":"Bad Request","message":"Invalid Token"} (observed 2026-11-17).
    Without this check an auth failure would look like an empty result."""
    if isinstance(body, dict) and "error" in body and isinstance(body.get("status"), int) \
            and body["status"] >= 400:
        msg = str(body.get("message", ""))
        status = 401 if "token" in msg.lower() else body["status"]
        raise ApiError(method, url, status, f"{body.get('error')}: {msg}")
    return body


def _chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class CspLicensingClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        token_url: str = DEFAULT_TOKEN_URL,
        base_url: str = DEFAULT_BASE_URL,
        scope: str = DEFAULT_SCOPE,
        token_auth_style: str = "post",
        http: Optional[HttpTransport] = None,
    ):
        self.http = http or HttpTransport()
        self.base_url = base_url.rstrip("/")
        self.token = ClientCredentialsToken(
            self.http, token_url, client_id, client_secret, scope=scope, auth_style=token_auth_style
        )

    # -- plumbing ---------------------------------------------------------
    def _call(self, method: str, path: str, *, params=None, json_body=None, retry: bool = True) -> Any:
        url = self.base_url + path
        for attempt in (1, 2):
            try:
                return _raise_embedded_error(method, url, self.http.request(
                    method, url, headers={"token": self.token.get()},
                    params=params, json_body=json_body, retry=retry,
                ).json())
            except ApiError as exc:
                if exc.status == 401 and attempt == 1:
                    self.token.invalidate()
                    continue
                raise

    # -- credit pools -----------------------------------------------------
    def list_credit_pools(self) -> List[Dict[str, Any]]:
        return self._call("GET", "/creditPool").get("data", [])

    def get_credit_pool(self, credit_pool_id: int) -> Dict[str, Any]:
        return self._call("GET", f"/creditPool/{credit_pool_id}").get("data", {})

    # -- deployment profiles (auth codes) ---------------------------------
    def list_profiles(self, credit_pool_id: int) -> List[Dict[str, Any]]:
        return self._call("GET", f"/creditPool/{credit_pool_id}/deploymentProfile").get("data", [])

    def get_profile(self, auth_code: str) -> Dict[str, Any]:
        return self._call("GET", f"/deploymentProfile/{auth_code}").get("data", {})

    def find_profile_by_name(self, credit_pool_id: int, name: str) -> Optional[Dict[str, Any]]:
        for p in self.list_profiles(credit_pool_id):
            if p.get("profileName") == name:
                return p
        return None

    def create_profile(self, spec: DeploymentProfileSpec) -> str:
        """Create a deployment profile and return its D- auth code. Not retried (non-idempotent)."""
        resp = self._call("POST", "/deploymentProfile", json_body=spec.to_body(), retry=False)
        auth_code = resp.get("auth_code")
        if not resp.get("success") or not auth_code:
            raise RuntimeError(f"deployment profile create failed: {resp}")
        log.info("created deployment profile %r -> %s", spec.name, auth_code)
        return auth_code

    def ensure_profile(self, spec: DeploymentProfileSpec) -> Tuple[str, bool]:
        """Idempotent create: reuse a profile with the same name in the pool. Returns (auth_code, created)."""
        existing = self.find_profile_by_name(spec.credit_pool_id, spec.name)
        if existing and existing.get("dAuthCode"):
            log.info("reusing deployment profile %r -> %s", spec.name, existing["dAuthCode"])
            return existing["dAuthCode"], False
        return self.create_profile(spec), True

    def update_profile(self, auth_code: str, spec: DeploymentProfileSpec) -> Dict[str, Any]:
        resp = self._call("PATCH", f"/deploymentProfile/{auth_code}", json_body=spec.to_body(), retry=False)
        if not resp.get("success"):
            raise RuntimeError(f"deployment profile update failed: {resp}")
        return resp

    def resize_profile(self, auth_code: str, spec: DeploymentProfileSpec, firewall_quantity: int) -> Dict[str, Any]:
        """Change planned firewall count. Lowering it releases allocated credits back to the pool."""
        deployed = int(self.get_profile(auth_code).get("fWsDeployed", 0))
        if firewall_quantity < max(deployed, 1):
            raise ValueError(f"cannot size below {max(deployed, 1)} (fWsDeployed={deployed})")
        spec.firewall_quantity = firewall_quantity
        return self.update_profile(auth_code, spec)

    def delete_profile(self, auth_code: str) -> bool:
        # The guide lists two URLs for this call; the curl sample's form matches the rest of the API.
        resp = self._call("DELETE", f"/deploymentProfile/{auth_code}", retry=False)
        return bool(resp.get("isDeleted"))

    # -- firewalls on an auth code ----------------------------------------
    def list_serials(self, auth_code: str) -> List[str]:
        resp = self._call("GET", "/firewallserialnumbers", params={"auth_code": auth_code})
        return list(resp.get("vm_series", []))

    def deactivate(self, auth_code: str, serials: List[str], *, skip_inactive: bool = True,
                   batch_size: int = 25) -> DeactivateResult:
        """Deactivate firewalls so their credits return to the deployment profile."""
        result = DeactivateResult(auth_code=auth_code, requested=list(serials))
        todo = list(dict.fromkeys(serials))
        if skip_inactive and todo:
            active = set(self.list_serials(auth_code))
            result.already_inactive = [s for s in todo if s not in active]
            todo = [s for s in todo if s in active]
        for batch in _chunks(todo, batch_size):
            resp = self._call(
                "DELETE", "/firewall/deactivate",
                params={"auth_code": auth_code, "serial_numbers": ",".join(batch)},
            )
            result.success += resp.get("success", []) or []
            result.failed += resp.get("failed", []) or []
            result.failure_reason += resp.get("failure_reason", []) or []
        if result.failed:
            log.error("deactivate failures on %s: %s %s", auth_code, result.failed, result.failure_reason)
        return result

    def teardown(self, auth_code: str, *, delete_profile: bool = True) -> Dict[str, Any]:
        """Deactivate every firewall on the auth code, then optionally delete the profile.

        The guide requires deactivation before a profile can be deleted.
        """
        serials = self.list_serials(auth_code)
        result = self.deactivate(auth_code, serials, skip_inactive=False) if serials else None
        summary: Dict[str, Any] = {
            "auth_code": auth_code,
            "serials": serials,
            "deactivated": result.success if result else [],
            "failed": result.failed if result else [],
            "profile_deleted": False,
        }
        if delete_profile:
            if result and not result.ok:
                raise RuntimeError(f"not deleting profile, deactivation incomplete: {summary}")
            summary["profile_deleted"] = self.delete_profile(auth_code)
        return summary
