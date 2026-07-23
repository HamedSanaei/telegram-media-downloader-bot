from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry


def test_metrics_render_fixed_prometheus_contract() -> None:
    metrics = MetricsRegistry()
    metrics.record_job(outcome="failed", source='bad"label', error="internal")
    metrics.observe_duration(1.5)
    metrics.add_bytes(10)
    metrics.set_queue_depth(2)
    rendered = metrics.render()
    assert 'outcome="failed",source="bad_label",error="internal"' in rendered
    assert "media_bot_job_duration_seconds_sum 1.500000" in rendered
    assert "media_bot_delivered_bytes_total 10" in rendered
    assert "media_bot_queue_depth 2" in rendered
