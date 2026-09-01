"""Non-blocking privacy disclosure for submission mirroring (T031/RC2).

Submission mirroring is an operator-enabled audit feature: when the operator
attests the privacy policy, accepted download submissions may be copied to a
private operational logger channel and retained indefinitely. Users are never
asked to acknowledge this before a download is accepted; the disclosure is
informational only and MUST never block, delay, or reject a download.
"""

from __future__ import annotations

LOGGER_PRIVACY_DISCLOSURE_FA = (
    "برخی درخواست‌های دانلود ممکن است برای امنیت، پشتیبانی و بررسی خطا در کانال "
    "خصوصی عملیاتی ثبت شوند. اطلاعات ورود، رمز عبور، کد دو مرحله‌ای و داده‌های "
    "حساس در لاگر ثبت نمی‌شوند."
)

__all__ = ["LOGGER_PRIVACY_DISCLOSURE_FA"]
