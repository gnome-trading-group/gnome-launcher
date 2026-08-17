import logging

from launcher.handlers.api.http import api_handler, ok
from launcher.rules.classifier_event import ClassifierEventRule  # noqa: F401 — registers rule type
from launcher.rules.direct_launch import DirectLaunchRule  # noqa: F401 — registers rule type
from launcher.rules.types import RULE_TYPE_REGISTRY

logger = logging.getLogger(__name__)


@api_handler
def handler(event, context):
    result = [
        {
            "type": cls.type,
            "display_name": cls.display_name,
            "parameter_schema": cls.parameter_schema,
            "data_schema": cls.data_schema,
        }
        for cls in RULE_TYPE_REGISTRY.values()
    ]
    return ok(result)
