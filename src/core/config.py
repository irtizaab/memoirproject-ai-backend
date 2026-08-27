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
