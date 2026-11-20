"""Runtime wiring for the Lambda handlers (env vars + Secrets Manager)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from . import csp, scm
from .aws import AwsOps, extract_d_authcode

log = logging.getLogger(__name__)


def _bool(v: Optional[str], default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    api_secret_id: str
    asg_name: str = ""
    auth_code: str = ""  # explicit D- code; else read from the bootstrap secret
    bootstrap_secret_id: str = ""
    decommission_folder: str = "Decommissioned"
    mgmt_device_index: int = 1
    sns_topic_arn: str = ""
    dry_run: bool = True
    csp_token_url: str = csp.DEFAULT_TOKEN_URL
    csp_base_url: str = csp.DEFAULT_BASE_URL
    csp_token_auth_style: str = "post"
    scm_token_url: str = scm.DEFAULT_TOKEN_URL
    scm_base_url: str = scm.DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        return cls(
            api_secret_id=e["API_SECRET_ID"],
            asg_name=e.get("ASG_NAME", ""),
            auth_code=e.get("AUTH_CODE", ""),
            bootstrap_secret_id=e.get("BOOTSTRAP_SECRET_ID", ""),
            decommission_folder=e.get("DECOMMISSION_FOLDER", "Decommissioned"),
            mgmt_device_index=int(e.get("MGMT_DEVICE_INDEX", "1")),
            sns_topic_arn=e.get("SNS_TOPIC_ARN", ""),
            dry_run=_bool(e.get("DRY_RUN"), True),
            csp_token_url=e.get("CSP_TOKEN_URL") or csp.DEFAULT_TOKEN_URL,
            csp_base_url=e.get("CSP_BASE_URL") or csp.DEFAULT_BASE_URL,
            csp_token_auth_style=e.get("CSP_TOKEN_AUTH_STYLE") or "post",
            scm_token_url=e.get("SCM_TOKEN_URL") or scm.DEFAULT_TOKEN_URL,
            scm_base_url=e.get("SCM_BASE_URL") or scm.DEFAULT_BASE_URL,
        )


@dataclass
class Deps:
    settings: Settings
    aws: AwsOps
    csp: csp.CspLicensingClient
    scm: Optional[scm.ScmClient]
    auth_code: str

    @classmethod
    def build(cls, settings: Optional[Settings] = None, aws: Optional[AwsOps] = None) -> "Deps":
        settings = settings or Settings.from_env()
        aws = aws or AwsOps()
        creds = aws.get_secret_json(settings.api_secret_id)

        csp_client = csp.CspLicensingClient(
            creds["csp_client_id"], creds["csp_client_secret"],
            token_url=settings.csp_token_url, base_url=settings.csp_base_url,
            token_auth_style=settings.csp_token_auth_style,
        )
        scm_client = None
        if creds.get("scm_client_id") and creds.get("scm_tsg_id"):
            scm_client = scm.ScmClient(
                creds["scm_client_id"], creds["scm_client_secret"], creds["scm_tsg_id"],
                token_url=settings.scm_token_url, base_url=settings.scm_base_url,
            )
        else:
            log.warning("SCM credentials not configured; SCM lookup/decommission disabled")

        auth_code = settings.auth_code
        if not auth_code and settings.bootstrap_secret_id:
            boot = aws.get_secret_json(settings.bootstrap_secret_id)
            auth_code = extract_d_authcode(boot.get("authcodes", "")) or ""
        if not auth_code:
            raise RuntimeError("no Software NGFW auth code: set AUTH_CODE or 'authcodes' in the bootstrap secret")
        return cls(settings, aws, csp_client, scm_client, auth_code)


_cached: Optional[Deps] = None


def get_deps() -> Deps:
    """Build once per Lambda container so OAuth tokens are reused across warm invocations."""
    global _cached
    if _cached is None:
        _cached = Deps.build()
    return _cached
