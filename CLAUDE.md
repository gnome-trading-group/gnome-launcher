# CLAUDE.md

gnome-launcher decides when to launch and stop strategy sessions. Triggers come from the classifier (SNS -> SQS), the CS2 scanner (after each gnome-pipelines `cs2_upcoming` run), and the API. Each trigger is matched against launch rules, may wait for Slack or controller approval, and ends in a call to the registry (`create_strategy_session` -> `POST /strategy-sessions/launch`, or `stop_strategy_session_as_service`). The controller UI drives the API; gnome-bot reads it.

## Commands
- Install: `poetry install`
- Test all: `poetry run pytest`
- Test one: `poetry run pytest tests/test_expiry.py::test_a_trigger_after_its_deadline_makes_no_request`
- CDK deps: `cd cdk && npm ci` (needs a GitHub Packages token in `~/.npmrc` for `@gnome-trading-group/gnome-shared-cdk`)
- CDK type-check: `cd cdk && npx tsc --noEmit`
- CDK synth: `cd cdk && npx cdk synth` (what the pipeline runs)
- `cdk/**/*.js` and `*.d.ts` are stale, gitignored tsc output. Edit the `.ts` files.

## Layout
- `launcher/handlers/trigger_processor.py`: launch queue consumer; runs every active rule of the trigger's type.
- `launcher/handlers/shutdown_processor.py`: shutdown queue consumer; asks every rule of the type, including disabled and archived ones.
- `launcher/handlers/scheduled_launch.py`: EventBridge Scheduler target for SCHEDULED requests; rechecks rule, listings and deadline.
- `launcher/handlers/sweeper.py`: every 5 min; expiry, stuck-request recovery, session status tracking.
- `launcher/handlers/api/`: REST API (`api_trigger`, `launch_requests`, `launch_rules`, `rule_types`, shared `http`).
- `launcher/handlers/slack/`: `interaction` (Slack Interactivity endpoint), `approve_launch`, `approve_shutdown`.
- `launcher/handlers/custom/`: trigger sources: `classifier_adapter` (SNS), `cs2_scanner` (research tables).
- `launcher/rules/types.py`: `RuleType` base, `RULE_TYPE_REGISTRY`, `@register_rule_type`, shared strategy param schema.
- `launcher/rules/catalog.py`: imports every rule type; anything looking a type up by name goes through it.
- `launcher/rules/engine.py`: rate limits (max concurrent sessions, cooldown), then the type's `evaluate`.
- `launcher/rules/validation.py`: rule schema, defaults, trigger `data_schema` and deadline checks.
- `launcher/rules/{classifier_event,cs2,direct_launch}.py`: the three rule types.
- `launcher/lifecycle.py`: every request state change: dedup key, repeat check, launch, stop, fail, expire, schedules.
- `launcher/dynamo.py`: all table access; conditional `transition`; trigger log (`record_evaluation`).
- `launcher/models.py`: request statuses, trigger outcomes, open/dead-end status sets.
- `launcher/sessions.py`: registry session config building, active statuses, `launcher.rule.id` stamp.
- `launcher/slack_client.py`: approval/schedule messages; no-op when `SLACK_CHANNEL_ID` is empty.
- `launcher/alerts.py`: reports to the alerting system via gnomepy `send_alert`.
- `launcher/queues.py`: `send_trigger`, the only way triggers enter the FIFO queues.
- `cdk/lib/stacks/`: `launcher-stack.ts`, `slack-interaction-stack.ts`, `monitoring-stack.ts`; `cdk/lib/config.ts` per stage.
- `tests/conftest.py`: moto DynamoDB/SQS/Scheduler plus in-memory `FakeRegistry` and Slack fakes; registers a `probe` rule type.

## Conventions and invariants
- Launch and shutdown queues are FIFO with `MessageGroupId = rule_type`. That way one trigger per type is handled at a time, so two triggers can't both pass a dedup or concurrency check. The account's Lambda concurrency is too small to reserve concurrency instead. Always enqueue through `queues.send_trigger`.
- SQS content dedup is off (`MessageDeduplicationId` = trigger id). The launcher dedups itself.
- Dedup key = sha256 of `{rule_type, action, data}`. A trigger is a repeat for a rule if that rule has an open request with the key (any age), or a non-dead-end one inside `dedup_window_minutes` (default 60). Window 0 disables the window only, not the open check. Dead-end requests (FAILED, REJECTED, CANCELLED, EXPIRED, ...) don't count, so the next identical trigger retries.
- `deadline` travels beside `data`, not in it. A moved deadline is the same trigger, and it moves the open request's `expires_at`. Keep trigger `data` stable for the life of the thing it describes (CS2 sends ids only).
- Every status change goes through `DynamoClient.transition(expected -> new)`. That way a double click, or an approval racing a cancel, acts only once. Don't write `status` directly.
- `lifecycle.launch` records `session_id` before calling the registry, so a timeout after the registry made the session still resolves to LAUNCHED.
- Every rule outcome per trigger is written to `gnome-launch-triggers` (REQUESTED, DUPLICATE, RATE_LIMITED, NO_MATCH, BLOCKED, ERROR, INVALID, NO_RULES, EXPIRED). This is what answers "why didn't this launch?". Rows that made no request expire after 30 days.
- Invalid triggers are logged and dropped, not retried, because retrying can't fix them.
- BLOCKED: the registry refuses overlapping listings for the same strategy and mode (STOPPING sessions included), so the launcher checks first.
- Sessions are stamped `launcher.rule.id`. A rule only ever stops sessions it launched, never manual ones.
- Rules are archived, never deleted. Every save writes a revision. PATCH takes `revision` to refuse lost updates. Rules are validated whole on every write.
- Adding a rule type: subclass `RuleType` with `@register_rule_type`, set `type`, `display_name`, `parameter_schema`, `data_schema`, then add it to `RULE_TYPES` in `rules/catalog.py`. A type not imported by catalog is invisible to the processors and API.
- Approval paths: `launch_path` / `shutdown_path` is `auto` or `approval`. Approval posts a Slack message, and the request can be approved from Slack or from the controller (`POST /launch-requests/{id}/approve`). Both use the same conditional transition, then async-invoke `ApproveLaunch` / `ApproveShutdown`.
- Expiry: a request past its trigger deadline becomes EXPIRED and loses its Slack buttons. A scheduled launch whose time is at or after the deadline is refused. Approvals with no action for 24h are cancelled (shutdown approvals become SHUTDOWN_REJECTED). LAUNCHING/APPROVED/STOPPING requests idle for 15 min are resolved by the sweeper.
- `scheduled_launch` rechecks when the schedule fires. If the rule is no longer active the launch is CANCELLED, if the listings are held it is FAILED, if the deadline has passed it is EXPIRED.
- Schedules are named `launcher-<request_id>` and must be in UTC. IAM is scoped to `schedule/default/launcher-*`.
- Slack: the launcher's Slack app has a single Interactivity URL (`/slack/interact` on `gnome-launcher-slack-api`), so Slack sends this handler every click on that app's messages. It verifies the signing secret, handles only `launch_*`, `shutdown_*`, `schedule_*` actions and the `schedule_modify` modal, and acknowledges every other click with 200 and does nothing. gnome-bot's buttons (`bot_*` action ids, e.g. "Attempt fix" on a triage) post through the same Slack app, so they arrive here too: they're forwarded untouched, as an async invoke, to the `gnome-bot-interact` Lambda (fixed name, `BOT_INTERACT_FUNCTION_NAME`), and a failed forward is only logged. Slack still gets its 200 at once.
- API auth: shared `GnomeApiAuthorizer` (Cognito user pool from SSM `/gnome/cognito/user-pool-arn`). Writes allow `CallerKind.PERSON` only. GETs allow PERSON and BOT. No API key. The actor is recorded as `ui:<email>`.
- Free-form JSON fields (`parameters`, `data`, `resolved_config`, `links`) are stored as strings, because DynamoDB returns Decimals that break strategies and the registry.
- A failure to send an alert is only logged, never raised. Alerting must not block launches.
- Dev has no Slack channel (`slackChannelId: ''`), so all Slack calls are no-ops there. In dev, approve through the controller.
- `classifier_event` calls Claude Haiku (secret `anthropic-api-key`) to work out a schedule time from event names.

## Operations
- Deploys: `LauncherPipeline` (in the InfraPipelines account) builds from `main` and deploys stage `Dev`, then the manual `ApproveProd` step, then `Prod`. Each stage has `LauncherStack`, `LauncherSlackInteractionStack` and `LauncherMonitoringStack`. All Lambdas share one Docker image (repo root `Dockerfile`, linux/amd64) and differ only by handler cmd.
- Imports from other stacks: `ClassifierNotificationsTopicArn`, `RegistryApiUrl`, `RegistryApiKeyId`, SSM `/gnome/alerting/api-url`.
- What runs:
  - ClassifierAdapter: ClassifierAdapterQueue (subscribed to the classifier SNS topic). It sends `new_events` to the launch queue and `resolved` to the shutdown queue.
  - Cs2Scanner: EventBridge rule on `gnome.pipelines` / `Pipeline Run Succeeded` / `cs2_upcoming`. It reads `cs2_upcoming_matches` and sends cs2 triggers. It resends every run on purpose, because dedup makes the repeats no-ops.
  - TriggerProcessor: LauncherFifoQueue (batch 1).
  - ShutdownProcessor: ShutdownFifoQueue (batch 1).
  - ScheduledLaunch: EventBridge Scheduler one-shot schedules.
  - Sweeper: EventBridge rate(5 minutes).
  - ApproveLaunch / ApproveShutdown: async invoke from SlackInteraction or the LaunchRequests API.
  - SlackInteraction: `POST /slack/interact`.
  - ApiTrigger, LaunchRequests, LaunchRules, RuleTypes: `gnome-launcher-api` routes `/triggers`, `/launch-requests`, `/launch-rules`, `/rule-types`.
- Queues: the three queues dead-letter after 3 receives and keep DLQ messages 14 days. Visibility is 5 min on the FIFO queues and 2 min on ClassifierAdapterQueue.
- Logs: default Lambda log groups `/aws/lambda/<function name>`. Function names are CloudFormation-generated from the stack and construct id, e.g. TriggerProcessor in the `Prod-LauncherStack` stack. Find exact names with `aws logs describe-log-groups --log-group-name-prefix /aws/lambda/Prod-LauncherStack --profile prod`.
- Alarms (`monitoring-stack.ts`, cdk-monitoring-constructs like the registry and classifier; no actions: the registry's alerting stack picks up every alarm change via EventBridge, and the name suffix is the severity):
  - `Launcher--{Launcher,ClassifierAdapter,Shutdown}Queue-DLQ-Queue-Message-Count-Max-Error`: a message was dead-lettered, meaning a launch or shutdown that never happened. Check that function's logs for the exception, then the trigger log and request. Redrive only after the cause is fixed.
  - `Launcher--{Launcher,ClassifierAdapter,Shutdown}Queue-Queue-Message-Age-Max-Warning`: the oldest message is over 15 min old. A FIFO group is stuck behind a failing message, or the consumer is erroring or throttled.
  - `Launcher--{TriggerProcessor,ShutdownProcessor,ScheduledLaunch,ClassifierAdapter,Cs2Scanner,Sweeper,SlackInteraction}-Fault-Count-Error`: one or more invocation errors in 5 min. The API Lambdas and ApproveLaunch/ApproveShutdown have no alarms.
- Alerts sent (`alerts.py`, actor `launcher:<rule name>`):
  - `LAUNCH_SUCCEEDED`: a rule launched a session (key per session).
  - `LAUNCH_FAILED`: the registry refused or errored, the listings were held when a schedule fired, a schedule couldn't be created, or the sweeper found a stuck LAUNCHING/APPROVED request. The body carries the registry's reason. The key `LAUNCH_FAILED:<rule_id>:<dedup_key>` makes retries of the same trigger refire one alert.
  - `SESSION_STOPPED_AUTO`: a rule stopped a session it launched.
  - `SHUTDOWN_FAILED`: the registry stop call failed for a session (one alert per session; the request becomes STOP_FAILED). Stop the session by hand if needed.
  - A STOPPING request the sweeper finds stuck becomes STOP_FAILED with no alert.
- Triage starting points: `GET /triggers?rule_type=` (trigger log), `GET /launch-requests/{id}` (request plus its trigger's log), `GET /launch-rules/{id}/activity`, `POST /launch-rules/{id}/dry-run`.
- Runbooks: none in this repo.
