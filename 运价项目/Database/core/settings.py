"""Non-secret deployment settings.

Secrets stay outside this module: configure them in the server environment or
the company's secret store, never in source code or this status response.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_DEPLOYMENT_MODE = "self_managed"
DEFAULT_RELEASE_MODE = "review"
RELEASE_MODE_REVIEW = "review"
RELEASE_MODE_GUARDED_AUTO = "guarded_auto"
VALID_RELEASE_MODES = frozenset({RELEASE_MODE_REVIEW, RELEASE_MODE_GUARDED_AUTO})


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DeploymentSettings:
    deployment_mode: str
    release_mode: str
    backup_target_configured: bool
    dingtalk_sso_enabled: bool

    @classmethod
    def from_env(cls) -> "DeploymentSettings":
        release_mode = os.environ.get("FREIGHT_RELEASE_MODE", DEFAULT_RELEASE_MODE).strip().lower()
        if release_mode not in VALID_RELEASE_MODES:
            release_mode = DEFAULT_RELEASE_MODE
        return cls(
            deployment_mode=os.environ.get("FREIGHT_DEPLOYMENT_MODE", DEFAULT_DEPLOYMENT_MODE).strip() or DEFAULT_DEPLOYMENT_MODE,
            release_mode=release_mode,
            backup_target_configured=_enabled(os.environ.get("FREIGHT_BACKUP_TARGET_CONFIGURED")),
            dingtalk_sso_enabled=_enabled(os.environ.get("FREIGHT_DINGTALK_SSO_ENABLED")),
        )
