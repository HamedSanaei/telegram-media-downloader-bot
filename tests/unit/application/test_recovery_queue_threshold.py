from __future__ import annotations

import pytest
from pydantic import ValidationError

from telegram_media_bot.bootstrap.config import RecoverySection


def test_threshold_derives_from_default_worker_concurrency() -> None:
    recovery = RecoverySection(queue_backlog_per_worker_slot=4)
    # queue.max_jobs = 3 (project default) => 3 * 4 = 12 outstanding ARQ entries threshold.
    # Up to 3 of those may be running; the rest are waiting/deferred entries.
    assert recovery.effective_queue_pressure_threshold(3) == 12


def test_threshold_derives_from_single_slot_concurrency() -> None:
    recovery = RecoverySection(queue_backlog_per_worker_slot=4)
    assert recovery.effective_queue_pressure_threshold(1) == 4


def test_threshold_derives_from_larger_worker() -> None:
    recovery = RecoverySection(queue_backlog_per_worker_slot=4)
    assert recovery.effective_queue_pressure_threshold(10) == 40


def test_explicit_threshold_overrides_multiplier() -> None:
    recovery = RecoverySection(queue_backlog_per_worker_slot=4, queue_pressure_threshold=50)
    assert recovery.effective_queue_pressure_threshold(3) == 50


@pytest.mark.parametrize("multiplier", [0, -1])
def test_invalid_multiplier_is_rejected(multiplier: int) -> None:
    with pytest.raises(ValidationError):
        RecoverySection(queue_backlog_per_worker_slot=multiplier)


def test_explicit_threshold_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        RecoverySection(queue_pressure_threshold=0)


def test_defaults_match_operations_docs() -> None:
    recovery = RecoverySection()
    assert recovery.queue_backlog_per_worker_slot == 4
    assert recovery.queue_pressure_threshold is None
