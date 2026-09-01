# App-wide configuration. Read the environment ONCE, here, and nowhere else.
#
# This replaces the two lines that used to sit at the top of main.py:
#
#     load_dotenv()
#     DATABASE_URL = os.environ["DATABASE_URL"]
#
# Why bother? Because "read an environment variable" is the kind of thing that
# spreads. Do it inline and six months from now you have os.getenv() calls in
# nine files, half of them with a different default, and no single place to look
# up what this app actually needs to run.

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every environment variable this app needs, in one typed object.

    pydantic-settings reads .env and the real environment, then validates.
    Fields without a default are required: a missing one crashes the app at
    import time with a clear message. Failing at startup is the point — you
    want to find out now, not on the first request that touches it.
    """

    # --- Postgres -----------------------------------------------------------
    database_url: str

    # --- Supabase Auth ------------------------------------------------------
    # The project's base URL, e.g. https://abcdefgh.supabase.co
    #
    # This is NOT a secret. It is baked into every frontend bundle. It is here
    # because the API needs two things from it: the JWKS endpoint (to fetch the
    # public key that verifies access tokens) and the expected `iss` claim.
    supabase_url: str

    # The anon/publishable key. Also NOT a secret — it is the key browsers use.
    #
    # The API does not need it to verify tokens; verification uses the public
    # JWKS. It is used only by the /dev/signin helper below, which mints a real
    # token for curl testing before the frontend login screen exists.
    supabase_anon_key: str | None = None

    # --- Supabase Storage ---------------------------------------------------
    # The service role key. This one IS a secret, and it is the only one this
    # app holds.
    #
    # Everything above is public: the project URL and the anon key ship in
    # every frontend bundle, and token verification uses Supabase's *public*
    # JWKS. This key is different — it bypasses Row Level Security and can read
    # or write anything in the project.
    #
    # It is here because contributors have no account. Signing an upload URL
    # for someone with no token of their own cannot be done with a user's
    # credentials, because they have none. Never log it, never return it in a
    # response, never send it to the browser.
    supabase_service_role_key: str | None = None

    # The private bucket media objects go to. A setting rather than a constant
    # so staging and production cannot end up writing into the same bucket.
    supabase_storage_bucket: str = "memoir-media"

    # How long a signed download URL stays valid, in seconds. Long enough that
    # a photo does not expire while someone is reading the page, short enough
    # that a URL copied out of devtools stops working the same day.
    signed_url_ttl_seconds: int = 3600

    # --- AssemblyAI (transcription) -----------------------------------------
    # The API key. A secret, though a much smaller one than the service role
    # key above: it can spend money on this account and read the transcripts
    # this account has made, and nothing else. Confined to
    # src/integrations/assemblyai.py.
    assemblyai_api_key: str | None = None

    # A string WE invent, not one AssemblyAI issues.
    #
    # The webhook endpoint is public — it has to be, since AssemblyAI has no
    # account here and cannot hold a token. So each job is submitted with this
    # value as a custom header, and the endpoint checks that what comes back
    # matches. That is the whole of its authentication, which is why it should
    # be long and random.
    assemblyai_webhook_secret: str | None = None

    # Which model to ask for. A setting rather than a constant so the cheaper
    # tier can be tried without a deploy. Empty means "AssemblyAI's default".
    assemblyai_speech_model: str | None = None

    # Where AssemblyAI should call back, e.g. https://api.memoirproject.co
    #
    # Empty in local development, and that is expected: localhost is not
    # reachable from the internet. When this is unset no webhook is requested
    # and the reconcile-on-read path carries the feature by itself, which is
    # exactly why that path exists.
    public_base_url: str | None = None

    # The kill switch. Set false and uploads still work, transcripts are marked
    # 'skipped', and nothing is spent.
    transcription_enabled: bool = True

    # Registers POST /dev/signin. Off unless explicitly switched on, so the
    # route cannot exist in production by accident. See src/api/dev.py.
    enable_dev_routes: bool = False

    @property
    def supabase_jwks_url(self) -> str:
        """Where Supabase publishes the public keys that sign its JWTs.

        Public-key crypto is what makes this safe: Supabase holds the private
        key and signs tokens with it, and anyone can fetch the matching public
        key to check a signature. This API can verify a token is genuine
        without holding any secret capable of forging one.
        """
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def supabase_storage_url(self) -> str:
        """Base URL of the Storage REST API for this project."""
        return f"{self.supabase_url.rstrip('/')}/storage/v1"

    @property
    def assemblyai_webhook_url(self) -> str | None:
        """Where AssemblyAI should POST a finished transcript, or None.

        None whenever the public base URL or the shared secret is missing —
        the normal state on a laptop. Submitting a webhook URL that cannot be
        reached, or one with no secret to verify, is worse than submitting
        none: the job still succeeds and the result still arrives, just by the
        poll path instead.
        """
        if not self.public_base_url or not self.assemblyai_webhook_secret:
            return None
        return f"{self.public_base_url.rstrip('/')}/webhooks/assemblyai"

    @property
    def supabase_issuer(self) -> str:
        """The `iss` claim every token from this project carries.

        Checking it stops a validly-signed token from a *different* Supabase
        project being accepted here.
        """
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    model_config = SettingsConfigDict(
        env_file=".env",          # matches the .env already in the repo root
        env_file_encoding="utf-8",
        extra="ignore",           # don't crash on unrelated vars in .env
    )


# One shared instance, built when this module is first imported.
# Everything else does `from src.core.config import settings` and reads
# `settings.database_url` — no module ever touches os.environ again.
settings = Settings()
