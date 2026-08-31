"""Fail-closed VIP feature activation preflight (T025)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VipRolloutFlags:
    account_connection: bool = False
    credential_preference: bool = False
    private_media: bool = False
    billing: bool = False


@dataclass(frozen=True, slots=True)
class VipReadiness:
    enabled: bool
    reasons: tuple[str, ...]


def evaluate_vip_readiness(
    flags: VipRolloutFlags, *, provider_selected: bool, operator_attested: bool
) -> VipReadiness:
    reasons: list[str] = []
    if flags.credential_preference and not operator_attested:
        reasons.append("operator_public_attestation_required")
    if flags.private_media and not flags.credential_preference:
        reasons.append("credential_preference_required")
    if flags.billing and not provider_selected:
        reasons.append("payment_provider_not_selected")
    return VipReadiness(enabled=not reasons, reasons=tuple(reasons))


__all__ = ["VipReadiness", "VipRolloutFlags", "evaluate_vip_readiness"]
