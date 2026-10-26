"""gnome-bot's buttons share this Slack app, so their clicks arrive at the launcher's interaction endpoint."""
import json

from launcher.handlers.slack import interaction


def test_a_bot_button_is_forwarded_to_gnome_bot_untouched(slack_click):
    value = json.dumps({"channel": "C1", "thread_ts": "500.1"})
    response = slack_click(None, None, action={"action_id": "bot_fix", "value": value})
    assert response["statusCode"] == 200
    function, payload = slack_click.lambda_client.invoked[0]
    assert function == "gnome-bot-interact"
    assert payload["actions"][0] == {"action_id": "bot_fix", "value": value} and payload["user"]["id"] == "U1"


def test_the_launchers_own_buttons_are_not_forwarded(ctx, slack_click):
    slack_click("launch_approve", "no-such-request")
    assert all(function != "gnome-bot-interact" for function, _ in slack_click.lambda_client.invoked)


def test_a_failed_forward_still_answers_slack(slack_click, monkeypatch):
    class Down:
        def invoke(self, **kwargs):
            raise RuntimeError("Lambda unavailable")
    monkeypatch.setattr(interaction, "_lambda_client", Down())
    assert slack_click(None, None, action={"action_id": "bot_fix", "value": "{}"})["statusCode"] == 200
