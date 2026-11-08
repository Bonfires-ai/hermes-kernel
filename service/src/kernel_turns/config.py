"""Settings for the turn service: the only place this service reads the environment.

Every field is ``KT_<NAME>``. There is deliberately no ``env_file``: a stray ``.env`` in the working directory must not
steer a test (or the service) at a live database.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Keys join with \x1f, so a bonfire id holds no control character. The seam's answers are held to the same rule.
BONFIRE_PATTERN = r"^[^\x00-\x1f\x7f-\x9f]+$"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KT_",
        extra="ignore",
        # compose passes `KT_X: ${VAR:-}`: an empty variable means unset, not "".
        env_ignore_empty=True,
        # A refused start prints its error: never with the values it was given (a token, the Mongo URI).
        hide_input_in_errors=True,
    )

    # `mk serve`: unauthenticated, reachable on the stack's own network only.
    mk_url: str = "http://mk-serve:8000"
    mongo_uri: SecretStr = SecretStr("mongodb://mongo:27017")
    mongo_db: str = "kernel_turns"
    port: int = 8000
    # Local: the one bonfire every conversation speaks to (required). Hosted: unset, since each key names its bonfire.
    bonfire_id: str | None = Field(default=None, min_length=1, pattern=BONFIRE_PATTERN)
    # Hosted seams. `auth_url` checks a key (POST {"key"} -> {bonfire_id, permission}); `handoff_url` is a base URL that
    # takes each bonfire's oldest closed session, every `handoff_every_s`. Each is called with
    # `Authorization: Bearer <token>`.
    auth_url: str | None = None
    auth_token: SecretStr = SecretStr("")
    handoff_url: str | None = None
    handoff_token: SecretStr = SecretStr("")
    handoff_every_s: float = 15
    # The bearer every route but /healthz requires. Empty refuses every request (never "no auth").
    token: SecretStr = SecretStr("")
    # Per-route budgets for `mk serve`: a resume may wait up to 30 s behind a teach on the kernel's side, a teach
    # takes 2-12 s. A plan stays under the plugin's 10 s read.
    plan_timeout_s: float = 8
    resume_timeout_s: float = 45
    teach_timeout_s: float = 30
    # A chat's session closes after this many seconds of quiet, or at this many messages.
    session_idle_s: float = 1800
    session_max_messages: int = 100
    # Turn and ledger rows expire after this many days. Unset keeps them. 0 would remove them all on the next pass.
    retention_days: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _local_or_hosted(self) -> Self:
        if (self.auth_url is None) != (self.handoff_url is None):
            raise ValueError("KT_AUTH_URL and KT_HANDOFF_URL are set together (hosted) or not at all (local)")
        if self.auth_url is None and self.bonfire_id is None:
            raise ValueError("KT_BONFIRE_ID is required unless KT_AUTH_URL and KT_HANDOFF_URL are set")
        if self.auth_url is not None and self.bonfire_id is not None:
            raise ValueError("KT_BONFIRE_ID is for a local stack: a hosted one takes each bonfire from the key")
        if self.auth_url is not None:  # blank would start, and then every key check or every hand-off would be a 401
            for variable, token in (("KT_AUTH_TOKEN", self.auth_token), ("KT_HANDOFF_TOKEN", self.handoff_token)):
                if not token.get_secret_value().strip():
                    raise ValueError(f"{variable} is blank: a hosted stack sends it as the bearer to its seam")
        return self
