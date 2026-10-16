"""Every rule type, registered by importing it: anything that looks a type up by name goes through here."""
from launcher.rules.classifier_event import ClassifierEventRule
from launcher.rules.cs2 import CS2Rule
from launcher.rules.direct_launch import DirectLaunchRule
from launcher.rules.types import RULE_TYPE_REGISTRY

RULE_TYPES = (ClassifierEventRule, CS2Rule, DirectLaunchRule)

__all__ = ["RULE_TYPES", "RULE_TYPE_REGISTRY"]
