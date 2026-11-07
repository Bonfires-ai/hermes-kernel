"""Settings for the writer: the only place it reads the environment.

There is deliberately no ``env_file``: a stray ``.env`` in the working directory must not steer a test (or the writer)
at a live database.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Each field is read from the variable named in its alias, or passed by field name.
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True, env_ignore_empty=True)

    # The turn service's database: the writer reads closed sessions from it.
    mongo_uri: SecretStr = Field(default=SecretStr("mongodb://mongo:27017"), validation_alias="KT_MONGO_URI")
    mongo_db: str = Field(default="kernel_turns", validation_alias="KT_MONGO_DB")
    bonfire_id: str = Field(min_length=1, validation_alias="KT_BONFIRE_ID")

    # The kernel's own record (spend holds, the append lease).
    ops_uri: SecretStr = Field(default=SecretStr("mongodb://mongo:27017"), validation_alias="MK_OPS_URI")
    ops_db: str = Field(default="mk_ops", validation_alias="MK_OPS_DB")
    data_dir: Path = Field(default=Path("/opt/mk"), validation_alias="MK_DATA_DIR")
    neo4j_uri: str = Field(default="bolt://neo4j:7687", validation_alias="NEO4J_URI")
    neo4j_password: SecretStr | None = Field(default=None, validation_alias="NEO4J_PASSWORD")
    openrouter_api_key: SecretStr | None = Field(default=None, validation_alias="OPENROUTER_API_KEY")
    voyage_api_key: SecretStr | None = Field(default=None, validation_alias="VOYAGE_API_KEY")

    spend_ceiling_usd: Decimal = Field(default=Decimal("20"), validation_alias="KT_SPEND_CEILING_USD")
    founding_cap_usd: Decimal = Field(default=Decimal("1"), validation_alias="KT_FOUNDING_CAP_USD")
    # Stops of one session before it is marked failed.
    max_tries: int = Field(default=3, validation_alias="KT_WRITER_MAX_TRIES")
    poll_s: float = Field(default=15, validation_alias="KT_WRITER_POLL_S")
    # A session held at the spend ceiling is tried again after this many seconds.
    held_retry_s: float = Field(default=600, validation_alias="KT_WRITER_HELD_RETRY_S")
    scratch_dir: Path = Field(default=Path("/tmp/writer"), validation_alias="KT_SCRATCH_DIR")

    def secrets(self) -> tuple[str, ...]:
        """Every secret value, for redaction (a Mongo uri may carry a password too)."""
        values = (self.mongo_uri, self.ops_uri, self.neo4j_password, self.openrouter_api_key, self.voyage_api_key)
        return tuple(v.get_secret_value() for v in values if v is not None and v.get_secret_value())
