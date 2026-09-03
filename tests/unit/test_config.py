"""Settings, and the URLs derived from them.

No database, no network. These are the values every other layer trusts, and two
of them decide whether a security control is active at all.
"""

import pytest

from src.core.config import Settings, settings


def test_the_jwks_url_points_at_this_project():
    """Where the public keys that verify every token come from.

    Public-key crypto is what makes the split safe: Supabase holds the private
    key, this API fetches the matching public one. It can confirm a token is
    genuine while holding nothing capable of forging one.
    """
    assert settings.supabase_jwks_url == (
        "https://project.supabase.test/auth/v1/.well-known/jwks.json"
    )


def test_the_issuer_is_derived_not_configured_separately():
    """One source, so the issuer check cannot drift from the key source.

    If `iss` were its own setting, a deployment could end up verifying
    signatures against one project while accepting tokens issued by another.
    Deriving both from `supabase_url` makes that impossible to misconfigure.
    """
    assert settings.supabase_issuer == "https://project.supabase.test/auth/v1"


def test_trailing_slashes_do_not_produce_double_slashed_urls():
    """A trailing slash in an environment variable is a very ordinary typo."""
    tidy = Settings(
        database_url="postgresql://x/memoir_test",
        supabase_url="https://project.supabase.test/",
    )

    assert "//auth" not in tidy.supabase_issuer.removeprefix("https://")
    assert tidy.supabase_issuer.endswith("/auth/v1")
    assert tidy.supabase_storage_url.endswith("/storage/v1")


class TestTheWebhookUrl:
    """When a callback is requested, and when it deliberately is not.

    `assemblyai_webhook_url` returning None means no webhook is asked for, and
    the reconcile-on-read path delivers the transcript instead. That is the
    normal state on a laptop, and it is why transcription can be developed with
    no tunnel.
    """

    def _settings(self, **overrides):
        base = {
            "database_url": "postgresql://x/memoir_test",
            "supabase_url": "https://project.supabase.test",
        }
        return Settings(**{**base, **overrides})

    def test_none_without_a_public_base_url(self):
        s = self._settings(public_base_url="", assemblyai_webhook_secret="shh")
        assert s.assemblyai_webhook_url is None

    def test_none_without_a_secret(self):
        """Fail closed, and this is the important half.

        Submitting a webhook URL with no secret to verify would leave a public
        endpoint that accepts anything — worse than having no webhook, because
        the job still succeeds either way and nothing would look broken.
        """
        s = self._settings(
            public_base_url="https://api.memoir.test", assemblyai_webhook_secret=None
        )
        assert s.assemblyai_webhook_url is None

    def test_a_url_when_both_are_present(self):
        s = self._settings(
            public_base_url="https://api.memoir.test/",
            assemblyai_webhook_secret="shh",
        )
        assert s.assemblyai_webhook_url == (
            "https://api.memoir.test/webhooks/assemblyai"
        )


def test_a_missing_required_setting_is_a_startup_crash():
    """Loud at boot rather than mysterious on the first request that needs it.

    `database_url` has no default on purpose. A deployment missing it should
    refuse to start, not start cleanly and then fail hours later on whichever
    request first touched the database.
    """
    import os

    saved = os.environ.pop("DATABASE_URL", None)
    try:
        with pytest.raises(Exception):
            Settings(_env_file=None, supabase_url="https://x.test")
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved


def test_transcription_can_be_switched_off_entirely():
    """The blunt kill switch.

    Uploads keep working; transcripts are recorded as `skipped` rather than
    `failed`, so no retry pass ever spends money on them later.
    """
    s = Settings(
        database_url="postgresql://x/memoir_test",
        supabase_url="https://x.test",
        transcription_enabled=False,
    )
    assert s.transcription_enabled is False


def test_dev_routes_are_off_unless_asked_for():
    """The default matters more than the flag.

    A deployment that forgets to set anything gets the safe posture — the dev
    router is never imported and `/dev/signin`, which mints a real token for
    anybody, does not exist.
    """
    s = Settings(database_url="postgresql://x/memoir_test", supabase_url="https://x.test")
    assert s.enable_dev_routes is False
