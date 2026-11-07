"""Settings for the turn service: the only place this service reads the environment.

Every field is ``KT_<NAME>``. There is deliberately no ``env_file``: a stray ``.env`` in the working directory must not
steer a test (or the service) at a live database.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KT_",
        extra="ignore",
        # compose passes `KT_X: ${VAR:-}`: an empty variable means unset, not "".
        env_ignore_empty=True,
    )

    # `mk serve`: unauthenticated, reachable on the stack's own network only.
    mk_url: str = "http://mk-serve:8000"
    mongo_uri: SecretStr = SecretStr("mongodb://mongo:27017")
    mongo_db: str = "kernel_turns"
    # The one bonfire every conversation speaks to. Keys join it with \x1f, so it holds no control character.
    bonfire_id: str = Field(min_length=1, pattern=r"^[^\x00-\x1f\x7f-\x9f]+$")
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
