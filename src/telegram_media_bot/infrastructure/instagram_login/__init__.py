"""Instagram transient login infrastructure (T018).

Contains a deterministic fake acquirer for tests/operator use. A real upstream adapter is
operator-supplied behind `application.ports.instagram_login.InstagramSessionAcquirer` and must
fail closed; no real provider client is bundled or called here.
"""
