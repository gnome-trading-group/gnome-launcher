import os

_STAGE = os.getenv("STAGE", "prod").lower()


class Config:
    CONTROLLER_BASE_URL: str
    SLACK_INTERACTION_ENABLED: bool


class DevConfig(Config):
    CONTROLLER_BASE_URL: str = "https://controller-dev.gnometrading.group"
    SLACK_INTERACTION_ENABLED: bool = False


class ProdConfig(Config):
    CONTROLLER_BASE_URL: str = "https://controller.gnometrading.group"
    SLACK_INTERACTION_ENABLED: bool = True


_CONFIG_MAP = {
    "dev": DevConfig,
    "prod": ProdConfig,
}

config = _CONFIG_MAP.get(_STAGE, ProdConfig)
