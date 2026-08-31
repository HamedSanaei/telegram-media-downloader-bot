from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry


def test_metrics_render_fixed_prometheus_contract() -> None:
    metrics = MetricsRegistry()
    metrics.record_job(outcome="failed", source='bad"label', error="internal")
    metrics.observe_duration(1.5)
    metrics.add_bytes(10)
    metrics.record_workspace_cleanup(
        files_deleted=2,
        directories_deleted=1,
        bytes_reclaimed=20,
        failed_paths=1,
        duration_seconds=0.25,
    )
    metrics.set_queue_depth(2)
    metrics.record_audit_delivery(outcome="succeeded", category="user_submission")
    metrics.set_audit_outbox(pending=3, uncertain=1, oldest_pending_seconds=45)
    rendered = metrics.render()
    assert 'outcome="failed",source="bad_label",error="internal"' in rendered
    assert "media_bot_job_duration_seconds_sum 1.500000" in rendered
    assert "media_bot_delivered_bytes_total 10" in rendered
    assert "media_bot_workspace_reclaimed_bytes_total 20" in rendered
    assert "media_bot_workspace_cleanup_failures_total 1" in rendered
    assert "media_bot_workspace_deleted_files_total 2" in rendered
    assert "media_bot_workspace_deleted_directories_total 1" in rendered
    assert "media_bot_workspace_cleanup_duration_seconds_total 0.250000" in rendered
    assert "media_bot_queue_depth 2" in rendered
    assert 'outcome="succeeded",category="user_submission"} 1' in rendered
    assert "media_bot_audit_outbox_pending 3" in rendered
    assert "media_bot_audit_outbox_uncertain 1" in rendered
    assert "media_bot_audit_outbox_oldest_pending_seconds 45" in rendered
