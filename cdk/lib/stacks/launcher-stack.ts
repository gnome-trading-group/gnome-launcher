import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Platform } from 'aws-cdk-lib/aws-ecr-assets';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as secrets from 'aws-cdk-lib/aws-secretsmanager';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { join } from 'path';
import { Stage } from '@gnome-trading-group/gnome-shared-cdk';
import { grantResearchTablesRead } from '../research-tables';

interface Props extends cdk.StackProps {
  stage: Stage;
  slackChannelId: string;
}

export class LauncherStack extends cdk.Stack {
  public static readonly USER_POOL_ARN_PARAMETER = '/gnome/cognito/user-pool-arn';
  public readonly api: apigateway.RestApi;
  public readonly approveLaunchFn: lambda.DockerImageFunction;
  public readonly approveShutdownFn: lambda.DockerImageFunction;
  public readonly scheduledLaunchFunctionArn: string;
  public readonly schedulerRoleArn: string;
  // For the monitoring stack's alarms.
  public readonly queues: { name: string; queue: sqs.Queue; dlq: sqs.Queue }[];
  public readonly processors: { name: string; fn: lambda.IFunction }[];

  constructor(scope: Construct, id: string, props: Props) {
    super(scope, id, props);

    const imageAsset = join(__dirname, '..', '..', '..');

    const slackBotTokenSecret = secrets.Secret.fromSecretNameV2(
      this, 'SlackBotToken', 'slack-bot-token'
    );

    // ── DynamoDB Tables ───────────────────────────────────────────────────

    const requestsTable = new dynamodb.Table(this, 'LaunchRequestsTable', {
      tableName: 'gnome-launch-requests',
      partitionKey: { name: 'request_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
    });
    requestsTable.addGlobalSecondaryIndex({
      indexName: 'dedup_key-date_created-index',
      partitionKey: { name: 'dedup_key', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'date_created', type: dynamodb.AttributeType.STRING },
    });
    requestsTable.addGlobalSecondaryIndex({
      indexName: 'status-date_created-index',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'date_created', type: dynamodb.AttributeType.STRING },
    });
    requestsTable.addGlobalSecondaryIndex({
      indexName: 'rule_type-date_created-index',
      partitionKey: { name: 'rule_type', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'date_created', type: dynamodb.AttributeType.STRING },
    });
    requestsTable.addGlobalSecondaryIndex({
      indexName: 'matched_rule_id-date_created-index',
      partitionKey: { name: 'matched_rule_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'date_created', type: dynamodb.AttributeType.STRING },
    });

    const rulesTable = new dynamodb.Table(this, 'LaunchRulesTable', {
      tableName: 'gnome-launch-rules',
      partitionKey: { name: 'rule_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
    });
    rulesTable.addGlobalSecondaryIndex({
      indexName: 'rule_type-status-index',
      partitionKey: { name: 'rule_type', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'status', type: dynamodb.AttributeType.STRING },
    });

    // Sparse: only a launched request whose session hasn't ended carries `tracking`, so the sweeper reads just those.
    requestsTable.addGlobalSecondaryIndex({
      indexName: 'tracking-date_created-index',
      partitionKey: { name: 'tracking', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'date_created', type: dynamodb.AttributeType.STRING },
    });

    // Every rule's outcome for every trigger (launcher/dynamo.py record_evaluation): what answers "why didn't this
    // launch?". Rows that made no request expire after 30 days.
    const triggersTable = new dynamodb.Table(this, 'LaunchTriggersTable', {
      tableName: 'gnome-launch-triggers',
      partitionKey: { name: 'trigger_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'rule_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
    });
    triggersTable.addGlobalSecondaryIndex({
      indexName: 'rule_id-received_at-index',
      partitionKey: { name: 'rule_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'received_at', type: dynamodb.AttributeType.STRING },
    });
    triggersTable.addGlobalSecondaryIndex({
      indexName: 'rule_type-received_at-index',
      partitionKey: { name: 'rule_type', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'received_at', type: dynamodb.AttributeType.STRING },
    });

    // Every saved version of every rule, with who saved it.
    const revisionsTable = new dynamodb.Table(this, 'LaunchRuleRevisionsTable', {
      tableName: 'gnome-launch-rule-revisions',
      partitionKey: { name: 'rule_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'revision', type: dynamodb.AttributeType.NUMBER },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
    });

    // ── SQS Queues ────────────────────────────────────────────────────────

    // The launch and shutdown queues are FIFO, and every trigger's message group is its rule type
    // (launcher/queues.py), so the processors take one trigger of a type at a time and two can't both pass a dedup
    // or concurrency check. Reserved concurrency would do the same, but the account's Lambda concurrency is too small
    // to reserve any. SQS's own content dedup stays off: the launcher dedups per rule and window itself.
    const launcherDlq = new sqs.Queue(this, 'LauncherFifoDlq', {
      fifo: true,
      retentionPeriod: cdk.Duration.days(14),
    });
    const launcherQueue = new sqs.Queue(this, 'LauncherFifoQueue', {
      fifo: true,
      visibilityTimeout: cdk.Duration.minutes(5),
      deadLetterQueue: { queue: launcherDlq, maxReceiveCount: 3 },
    });

    const classifierAdapterDlq = new sqs.Queue(this, 'ClassifierAdapterDlq', {
      retentionPeriod: cdk.Duration.days(14),
    });
    const classifierAdapterQueue = new sqs.Queue(this, 'ClassifierAdapterQueue', {
      visibilityTimeout: cdk.Duration.minutes(2),
      deadLetterQueue: { queue: classifierAdapterDlq, maxReceiveCount: 3 },
    });

    const shutdownDlq = new sqs.Queue(this, 'ShutdownFifoDlq', {
      fifo: true,
      retentionPeriod: cdk.Duration.days(14),
    });
    const shutdownQueue = new sqs.Queue(this, 'ShutdownFifoQueue', {
      fifo: true,
      visibilityTimeout: cdk.Duration.minutes(5),
      deadLetterQueue: { queue: shutdownDlq, maxReceiveCount: 3 },
    });

    this.queues = [
      { name: 'Launcher', queue: launcherQueue, dlq: launcherDlq },
      { name: 'ClassifierAdapter', queue: classifierAdapterQueue, dlq: classifierAdapterDlq },
      { name: 'Shutdown', queue: shutdownQueue, dlq: shutdownDlq },
    ];

    // Subscribe to the classifier's SNS topic
    const classifierNotificationsTopic = sns.Topic.fromTopicArn(
      this,
      'ClassifierNotificationsTopic',
      cdk.Fn.importValue('ClassifierNotificationsTopicArn'),
    );
    classifierNotificationsTopic.addSubscription(
      new snsSubscriptions.SqsSubscription(classifierAdapterQueue)
    );

    // ── Registry API key ──────────────────────────────────────────────────

    const registryApiKeyId = cdk.Fn.importValue('RegistryApiKeyId');
    const registryApiKeyArn = `arn:aws:apigateway:${this.region}::/apikeys/${registryApiKeyId}`;

    // ── EventBridge Scheduler IAM Role ────────────────────────────────────

    const schedulerRole = new iam.Role(this, 'SchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
    });

    // ── Shared Lambda factory ─────────────────────────────────────────────

    const sharedEnv = {
      STAGE: props.stage,
      LAUNCH_REQUESTS_TABLE: requestsTable.tableName,
      LAUNCH_RULES_TABLE: rulesTable.tableName,
      LAUNCH_TRIGGERS_TABLE: triggersTable.tableName,
      LAUNCH_RULE_REVISIONS_TABLE: revisionsTable.tableName,
      REGISTRY_API_URL: cdk.Fn.importValue('RegistryApiUrl'),
      REGISTRY_API_KEY_ID: registryApiKeyId,
      SLACK_BOT_TOKEN_SECRET: 'slack-bot-token',
      SLACK_CHANNEL_ID: props.slackChannelId,
      // Where launches, stops and their failures are reported (the registry's alerting stack).
      ALERTS_API_URL: ssm.StringParameter.valueForStringParameter(this, '/gnome/alerting/api-url'),
    };

    const createLambda = (
      id: string,
      handler: string,
      timeout: cdk.Duration,
      memorySize: number,
      extraEnv: Record<string, string> = {},
      grants: (fn: lambda.DockerImageFunction) => void = () => {},
    ): lambda.DockerImageFunction => {
      const fn = new lambda.DockerImageFunction(this, id, {
        code: lambda.DockerImageCode.fromImageAsset(imageAsset, {
          cmd: [handler],
          // The functions run on x86_64; pinned so an image built on an arm64 machine still runs.
          platform: Platform.LINUX_AMD64,
        }),
        timeout,
        memorySize,
        environment: { ...sharedEnv, ...extraEnv },
      });
      grants(fn);
      return fn;
    };

    // ── Classifier Adapter Lambda ─────────────────────────────────────────

    const classifierAdapterFn = createLambda(
      'ClassifierAdapter',
      'launcher.handlers.custom.classifier_adapter.handler',
      cdk.Duration.seconds(30),
      256,
      {
        LAUNCHER_QUEUE_URL: launcherQueue.queueUrl,
        SHUTDOWN_QUEUE_URL: shutdownQueue.queueUrl,
      },
      (fn) => {
        launcherQueue.grantSendMessages(fn);
        shutdownQueue.grantSendMessages(fn);
      },
    );
    classifierAdapterFn.addEventSource(
      new lambdaEventSources.SqsEventSource(classifierAdapterQueue, { batchSize: 1 })
    );

    // ── Trigger Processor Lambda ──────────────────────────────────────────

    const scheduledLaunchFn = createLambda(
      'ScheduledLaunch',
      'launcher.handlers.scheduled_launch.handler',
      cdk.Duration.minutes(2),
      512,
      {},
      (fn) => {
        requestsTable.grantReadWriteData(fn);
        // It checks the rule is still active before launching.
        rulesTable.grantReadData(fn);
        slackBotTokenSecret.grantRead(fn);
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['apigateway:GET'],
          resources: [registryApiKeyArn],
        }));
      },
    );

    scheduledLaunchFn.grantInvoke(schedulerRole);

    this.scheduledLaunchFunctionArn = scheduledLaunchFn.functionArn;
    this.schedulerRoleArn = schedulerRole.roleArn;

    const triggerProcessorFn = createLambda(
      'TriggerProcessor',
      'launcher.handlers.trigger_processor.handler',
      cdk.Duration.minutes(2),
      512,
      {
        SCHEDULED_LAUNCH_FUNCTION_ARN: scheduledLaunchFn.functionArn,
        SCHEDULER_ROLE_ARN: schedulerRole.roleArn,
        ANTHROPIC_API_KEY_SECRET: 'anthropic-api-key',
      },
      (fn) => {
        requestsTable.grantReadWriteData(fn);
        rulesTable.grantReadData(fn);
        triggersTable.grantWriteData(fn);
        slackBotTokenSecret.grantRead(fn);
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['apigateway:GET'],
          resources: [registryApiKeyArn],
        }));
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['scheduler:CreateSchedule', 'scheduler:DeleteSchedule', 'scheduler:UpdateSchedule'],
          resources: [`arn:aws:scheduler:${this.region}:${this.account}:schedule/default/launcher-*`],
        }));
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['iam:PassRole'],
          resources: [schedulerRole.roleArn],
        }));
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['secretsmanager:GetSecretValue'],
          resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:anthropic-api-key*`],
        }));
      },
    );
    triggerProcessorFn.addEventSource(
      new lambdaEventSources.SqsEventSource(launcherQueue, { batchSize: 1 })
    );

    // ── Shutdown Processor Lambda ─────────────────────────────────────────

    const shutdownProcessorFn = createLambda(
      'ShutdownProcessor',
      'launcher.handlers.shutdown_processor.handler',
      cdk.Duration.minutes(2),
      512,
      {},
      (fn) => {
        requestsTable.grantReadWriteData(fn);
        rulesTable.grantReadData(fn);
        triggersTable.grantWriteData(fn);
        slackBotTokenSecret.grantRead(fn);
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['apigateway:GET'],
          resources: [registryApiKeyArn],
        }));
      },
    );
    shutdownProcessorFn.addEventSource(
      new lambdaEventSources.SqsEventSource(shutdownQueue, { batchSize: 1 })
    );
    // ── CS2 Scanner Lambda ────────────────────────────────────────────────

    // Reads gnome-pipelines' cs2_upcoming_matches each time a cs2_upcoming run succeeds (gnome-pipelines' runner
    // announces it on the default bus) and queues cs2 launch and shutdown triggers.
    const cs2ScannerFn = createLambda(
      'Cs2Scanner',
      'launcher.handlers.custom.cs2_scanner.handler',
      cdk.Duration.minutes(2),
      1024,
      {
        LAUNCHER_QUEUE_URL: launcherQueue.queueUrl,
        SHUTDOWN_QUEUE_URL: shutdownQueue.queueUrl,
      },
      (fn) => {
        launcherQueue.grantSendMessages(fn);
        shutdownQueue.grantSendMessages(fn);
        grantResearchTablesRead(this, fn);
      },
    );
    new events.Rule(this, 'Cs2ScannerOnUpcomingRun', {
      eventPattern: {
        source: ['gnome.pipelines'],
        detailType: ['Pipeline Run Succeeded'],
        detail: { pipeline: ['cs2_upcoming'] },
      },
      targets: [new targets.LambdaFunction(cs2ScannerFn, { retryAttempts: 0 })],
    });

    this.processors = [
      { name: 'TriggerProcessor', fn: triggerProcessorFn },
      { name: 'ShutdownProcessor', fn: shutdownProcessorFn },
      { name: 'ScheduledLaunch', fn: scheduledLaunchFn },
      { name: 'ClassifierAdapter', fn: classifierAdapterFn },
      { name: 'Cs2Scanner', fn: cs2ScannerFn },
    ];

    // ── API Trigger Lambda ────────────────────────────────────────────────

    const apiTriggerFn = createLambda(
      'ApiTrigger',
      'launcher.handlers.api.api_trigger.handler',
      cdk.Duration.seconds(10),
      256,
      { LAUNCHER_QUEUE_URL: launcherQueue.queueUrl, SHUTDOWN_QUEUE_URL: shutdownQueue.queueUrl },
      (fn) => {
        launcherQueue.grantSendMessages(fn);
        shutdownQueue.grantSendMessages(fn);
        triggersTable.grantReadData(fn);
      },
    );

    // ── Launch Requests Lambda ────────────────────────────────────────────

    const launchRequestsFn = createLambda(
      'LaunchRequests',
      'launcher.handlers.api.launch_requests.handler',
      cdk.Duration.seconds(30),
      512,
      {},
      (fn) => {
        // Approve, reject, cancel, retry and resend write requests; retry and resend read the rule; cancel deletes
        // the schedule; each settles the request's Slack message, and resend looks up an older request's links in
        // the registry. Invoking the approve Lambdas is granted below, once they exist.
        requestsTable.grantReadWriteData(fn);
        rulesTable.grantReadData(fn);
        triggersTable.grantReadData(fn);
        slackBotTokenSecret.grantRead(fn);
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['apigateway:GET'],
          resources: [registryApiKeyArn],
        }));
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['scheduler:DeleteSchedule'],
          resources: [`arn:aws:scheduler:${this.region}:${this.account}:schedule/default/launcher-*`],
        }));
      },
    );

    // ── Launch Rules Lambda ───────────────────────────────────────────────

    const launchRulesFn = createLambda(
      'LaunchRules',
      'launcher.handlers.api.launch_rules.handler',
      cdk.Duration.seconds(30),
      512,
      {},
      (fn) => {
        rulesTable.grantReadWriteData(fn);
        revisionsTable.grantReadWriteData(fn);
        // Stats and dry runs read the rule's requests, its trigger log, and the registry's sessions and listings.
        requestsTable.grantReadData(fn);
        triggersTable.grantReadData(fn);
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['apigateway:GET'],
          resources: [registryApiKeyArn],
        }));
      },
    );

    // ── Rule Types Lambda ─────────────────────────────────────────────────

    const ruleTypesFn = createLambda(
      'RuleTypes',
      'launcher.handlers.api.rule_types.handler',
      cdk.Duration.seconds(10),
      128,
    );

    // ── Approve Launch Lambda ─────────────────────────────────────────────

    this.approveLaunchFn = createLambda(
      'ApproveLaunch',
      'launcher.handlers.slack.approve_launch.handler',
      cdk.Duration.minutes(2),
      512,
      {
        SCHEDULED_LAUNCH_FUNCTION_ARN: scheduledLaunchFn.functionArn,
        SCHEDULER_ROLE_ARN: schedulerRole.roleArn,
      },
      (fn) => {
        requestsTable.grantReadWriteData(fn);
        slackBotTokenSecret.grantRead(fn);
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['apigateway:GET'],
          resources: [registryApiKeyArn],
        }));
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['scheduler:CreateSchedule'],
          resources: [`arn:aws:scheduler:${this.region}:${this.account}:schedule/default/launcher-*`],
        }));
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['iam:PassRole'],
          resources: [schedulerRole.roleArn],
        }));
      },
    );

    new cdk.CfnOutput(this, 'ApproveLaunchFunctionArn', {
      value: this.approveLaunchFn.functionArn,
      exportName: 'LauncherApproveLaunchFunctionArn',
    });

    // ── Approve Shutdown Lambda ───────────────────────────────────────────

    this.approveShutdownFn = createLambda(
      'ApproveShutdown',
      'launcher.handlers.slack.approve_shutdown.handler',
      cdk.Duration.minutes(2),
      512,
      {},
      (fn) => {
        requestsTable.grantReadWriteData(fn);
        slackBotTokenSecret.grantRead(fn);
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['apigateway:GET'],
          resources: [registryApiKeyArn],
        }));
      },
    );

    launchRequestsFn.addEnvironment('APPROVE_LAUNCH_FUNCTION_NAME', this.approveLaunchFn.functionName);
    launchRequestsFn.addEnvironment('APPROVE_SHUTDOWN_FUNCTION_NAME', this.approveShutdownFn.functionName);
    this.approveLaunchFn.grantInvoke(launchRequestsFn);
    this.approveShutdownFn.grantInvoke(launchRequestsFn);

    // ── Sweeper Lambda ────────────────────────────────────────────────────

    // Keeps launched requests in step with their sessions, and resolves requests stuck between steps
    // (launcher/handlers/sweeper.py).
    const sweeperFn = createLambda(
      'Sweeper',
      'launcher.handlers.sweeper.handler',
      cdk.Duration.minutes(2),
      512,
      {},
      (fn) => {
        requestsTable.grantReadWriteData(fn);
        slackBotTokenSecret.grantRead(fn);
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['apigateway:GET'],
          resources: [registryApiKeyArn],
        }));
      },
    );
    new events.Rule(this, 'SweeperSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
      targets: [new targets.LambdaFunction(sweeperFn, { retryAttempts: 0 })],
    });
    this.processors.push({ name: 'Sweeper', fn: sweeperFn });

    // ── API Gateway ───────────────────────────────────────────────────────

    this.api = new apigateway.RestApi(this, 'LauncherApi', {
      restApiName: 'gnome-launcher-api',
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: apigateway.Cors.DEFAULT_HEADERS,
      },
    });

    // Without these, a request the Cognito authorizer rejects comes back with no CORS headers and the browser
    // reports an opaque CORS failure instead of the auth error.
    this.api.addGatewayResponse('Default4xxCors', {
      type: apigateway.ResponseType.DEFAULT_4XX,
      responseHeaders: { 'Access-Control-Allow-Origin': "'*'" },
    });
    this.api.addGatewayResponse('Default5xxCors', {
      type: apigateway.ResponseType.DEFAULT_5XX,
      responseHeaders: { 'Access-Control-Allow-Origin': "'*'" },
    });

    // Only people drive this API (the controller UI), so it takes a Cognito ID token rather than an API key that
    // would have to ship in the browser bundle. Slack approvals arrive through LauncherSlackInteractionStack instead.
    const userPool = cognito.UserPool.fromUserPoolArn(this, 'OperatorUserPool',
      ssm.StringParameter.valueForStringParameter(this, LauncherStack.USER_POOL_ARN_PARAMETER));
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'OperatorAuthorizer', {
      cognitoUserPools: [userPool],
    });
    const methodOptions: apigateway.MethodOptions = {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    };
    const lambdaIntegration = (fn: lambda.DockerImageFunction) =>
      new apigateway.LambdaIntegration(fn);

    const triggers = this.api.root.addResource('triggers');
    triggers.addMethod('POST', lambdaIntegration(apiTriggerFn), methodOptions);
    triggers.addMethod('GET', lambdaIntegration(apiTriggerFn), methodOptions);

    const launchRequests = this.api.root.addResource('launch-requests');
    launchRequests.addMethod('GET', lambdaIntegration(launchRequestsFn), methodOptions);
    const launchRequestById = launchRequests.addResource('{id}');
    launchRequestById.addMethod('GET', lambdaIntegration(launchRequestsFn), methodOptions);
    // approve | reject | cancel | retry
    launchRequestById.addResource('{action}').addMethod('POST', lambdaIntegration(launchRequestsFn), methodOptions);

    const launchRules = this.api.root.addResource('launch-rules');
    launchRules.addMethod('GET', lambdaIntegration(launchRulesFn), methodOptions);
    launchRules.addMethod('POST', lambdaIntegration(launchRulesFn), methodOptions);
    const launchRuleById = launchRules.addResource('{id}');
    launchRuleById.addMethod('GET', lambdaIntegration(launchRulesFn), methodOptions);
    launchRuleById.addMethod('PATCH', lambdaIntegration(launchRulesFn), methodOptions);
    launchRuleById.addMethod('DELETE', lambdaIntegration(launchRulesFn), methodOptions);
    launchRules.addResource('validate').addMethod('POST', lambdaIntegration(launchRulesFn), methodOptions);
    // revisions | activity | stats (GET) and dry-run (POST)
    const launchRuleView = launchRuleById.addResource('{view}');
    launchRuleView.addMethod('GET', lambdaIntegration(launchRulesFn), methodOptions);
    launchRuleView.addMethod('POST', lambdaIntegration(launchRulesFn), methodOptions);

    const ruleTypes = this.api.root.addResource('rule-types');
    ruleTypes.addMethod('GET', lambdaIntegration(ruleTypesFn), methodOptions);

    new cdk.CfnOutput(this, 'LauncherApiUrl', {
      value: this.api.url,
      exportName: 'LauncherApiUrl',
    });
  }
}
