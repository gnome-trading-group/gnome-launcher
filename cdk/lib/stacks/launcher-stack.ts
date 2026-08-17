import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as secrets from 'aws-cdk-lib/aws-secretsmanager';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { join } from 'path';
import { Stage } from '@gnome-trading-group/gnome-shared-cdk';

interface Props extends cdk.StackProps {
  stage: Stage;
  slackChannelId: string;
}

export class LauncherStack extends cdk.Stack {
  public readonly api: apigateway.RestApi;
  public readonly approveLaunchFn: lambda.DockerImageFunction;

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

    // ── SQS Queues ────────────────────────────────────────────────────────

    const launcherDlq = new sqs.Queue(this, 'LauncherDlq', {
      retentionPeriod: cdk.Duration.days(14),
    });
    const launcherQueue = new sqs.Queue(this, 'LauncherQueue', {
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

    // ── Shared Lambda factory ─────────────────────────────────────────────

    const sharedEnv = {
      STAGE: props.stage,
      LAUNCH_REQUESTS_TABLE: requestsTable.tableName,
      LAUNCH_RULES_TABLE: rulesTable.tableName,
      REGISTRY_API_URL: cdk.Fn.importValue('RegistryApiUrl'),
      REGISTRY_API_KEY_ID: registryApiKeyId,
      SLACK_BOT_TOKEN_SECRET: 'slack-bot-token',
      SLACK_CHANNEL_ID: props.slackChannelId,
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
      { LAUNCHER_QUEUE_URL: launcherQueue.queueUrl },
      (fn) => {
        launcherQueue.grantSendMessages(fn);
      },
    );
    classifierAdapterFn.addEventSource(
      new lambdaEventSources.SqsEventSource(classifierAdapterQueue, { batchSize: 1 })
    );

    // ── Trigger Processor Lambda ──────────────────────────────────────────

    const triggerProcessorFn = createLambda(
      'TriggerProcessor',
      'launcher.handlers.trigger_processor.handler',
      cdk.Duration.minutes(2),
      512,
      {},
      (fn) => {
        requestsTable.grantReadWriteData(fn);
        rulesTable.grantReadData(fn);
        slackBotTokenSecret.grantRead(fn);
        fn.addToRolePolicy(new iam.PolicyStatement({
          actions: ['apigateway:GET'],
          resources: [registryApiKeyArn],
        }));
      },
    );
    triggerProcessorFn.addEventSource(
      new lambdaEventSources.SqsEventSource(launcherQueue, { batchSize: 1 })
    );

    // ── API Trigger Lambda ────────────────────────────────────────────────

    const apiTriggerFn = createLambda(
      'ApiTrigger',
      'launcher.handlers.api.api_trigger.handler',
      cdk.Duration.seconds(10),
      256,
      { LAUNCHER_QUEUE_URL: launcherQueue.queueUrl },
      (fn) => {
        launcherQueue.grantSendMessages(fn);
      },
    );

    // ── Launch Requests Lambda ────────────────────────────────────────────

    const launchRequestsFn = createLambda(
      'LaunchRequests',
      'launcher.handlers.api.launch_requests.handler',
      cdk.Duration.seconds(10),
      256,
      {},
      (fn) => {
        requestsTable.grantReadData(fn);
      },
    );

    // ── Launch Rules Lambda ───────────────────────────────────────────────

    const launchRulesFn = createLambda(
      'LaunchRules',
      'launcher.handlers.api.launch_rules.handler',
      cdk.Duration.seconds(10),
      256,
      {},
      (fn) => {
        rulesTable.grantReadWriteData(fn);
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

    new cdk.CfnOutput(this, 'ApproveLaunchFunctionArn', {
      value: this.approveLaunchFn.functionArn,
      exportName: 'LauncherApproveLaunchFunctionArn',
    });

    // ── API Gateway ───────────────────────────────────────────────────────

    const apiKey = new apigateway.ApiKey(this, 'LauncherApiKey');
    const usagePlan = new apigateway.UsagePlan(this, 'LauncherUsagePlan');

    this.api = new apigateway.RestApi(this, 'LauncherApi', {
      restApiName: 'gnome-launcher-api',
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: [...apigateway.Cors.DEFAULT_HEADERS, 'x-api-key'],
      },
    });

    usagePlan.addApiStage({ api: this.api, stage: this.api.deploymentStage });
    usagePlan.addApiKey(apiKey);

    const apiKeyRequired = true;
    const lambdaIntegration = (fn: lambda.DockerImageFunction) =>
      new apigateway.LambdaIntegration(fn);

    const triggers = this.api.root.addResource('triggers');
    triggers.addMethod('POST', lambdaIntegration(apiTriggerFn), { apiKeyRequired });

    const launchRequests = this.api.root.addResource('launch-requests');
    launchRequests.addMethod('GET', lambdaIntegration(launchRequestsFn), { apiKeyRequired });
    const launchRequestById = launchRequests.addResource('{id}');
    launchRequestById.addMethod('GET', lambdaIntegration(launchRequestsFn), { apiKeyRequired });

    const launchRules = this.api.root.addResource('launch-rules');
    launchRules.addMethod('GET', lambdaIntegration(launchRulesFn), { apiKeyRequired });
    launchRules.addMethod('POST', lambdaIntegration(launchRulesFn), { apiKeyRequired });
    const launchRuleById = launchRules.addResource('{id}');
    launchRuleById.addMethod('GET', lambdaIntegration(launchRulesFn), { apiKeyRequired });
    launchRuleById.addMethod('PATCH', lambdaIntegration(launchRulesFn), { apiKeyRequired });
    launchRuleById.addMethod('DELETE', lambdaIntegration(launchRulesFn), { apiKeyRequired });

    const ruleTypes = this.api.root.addResource('rule-types');
    ruleTypes.addMethod('GET', lambdaIntegration(ruleTypesFn), { apiKeyRequired });

    new cdk.CfnOutput(this, 'LauncherApiUrl', {
      value: this.api.url,
      exportName: 'LauncherApiUrl',
    });
    new cdk.CfnOutput(this, 'LauncherApiKeyId', {
      value: apiKey.keyId,
      exportName: 'LauncherApiKeyId',
    });
  }
}
