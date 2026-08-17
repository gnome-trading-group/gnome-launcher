import os

_STAGE = os.getenv("STAGE", "prod").lower()


class Config:
    CONTROLLER_BASE_URL: str


class DevConfig(Config):
    CONTROLLER_BASE_URL: str = "https://controller-dev.gnometrading.group"


class ProdConfig(Config):
    CONTROLLER_BASE_URL: str = "https://controller.gnometrading.group"


_CONFIG_MAP = {
    "dev": DevConfig,
    "prod": ProdConfig,
}

config = _CONFIG_MAP.get(_STAGE, ProdConfig)
