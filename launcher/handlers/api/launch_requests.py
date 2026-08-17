import logging

from launcher.dynamo import DynamoClient
from launcher.handlers.api.http import api_handler, error, ok

logger = logging.getLogger(__name__)


@api_handler
def handler(event, context):
    dynamo = DynamoClient()
    path_params = event.get("pathParameters") or {}
    request_id = path_params.get("id")

    if request_id:
        item = dynamo.get_request(request_id)
        if not item:
            return error(404, f"Request not found: {request_id}")
        return ok(item)

    query = event.get("queryStringParameters") or {}
    status = query.get("status")
    rule_type = query.get("rule_type")
    limit = int(query.get("limit", 50))

    items = dynamo.list_requests(status=status, rule_type=rule_type, limit=limit)
    return ok(items)
