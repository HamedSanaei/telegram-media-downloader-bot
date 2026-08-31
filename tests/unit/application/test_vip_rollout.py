from telegram_media_bot.application.services.vip_rollout import (
    VipRolloutFlags,
    evaluate_vip_readiness,
)


def test_billing_stays_blocked_without_selected_provider() -> None:
    result = evaluate_vip_readiness(
        VipRolloutFlags(billing=True), provider_selected=False, operator_attested=False
    )
    assert result.enabled is False
    assert result.reasons == ("payment_provider_not_selected",)


def test_private_media_requires_attested_operator_preference_gate() -> None:
    result = evaluate_vip_readiness(
        VipRolloutFlags(credential_preference=True, private_media=True),
        provider_selected=False,
        operator_attested=False,
    )
    assert result.enabled is False
    assert "operator_public_attestation_required" in result.reasons
