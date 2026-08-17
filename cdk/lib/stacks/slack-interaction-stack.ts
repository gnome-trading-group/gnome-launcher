import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as secrets from 'aws-cdk-lib/aws-secretsmanager';
import { join } from 'path';
import { Stage } from '@gnome-trading-group/gnome-shared-cdk';

interface Props extends cdk.StackProps {
  stage: Stage;
  slackChannelId: string;
  approveLaunchFn: lambda.DockerImageFunction;
  requestsTableName: string;
  rulesTableName: string;
}

export class LauncherSlackInteractionStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: Props) {
    super(scope, id, props);

    const imageAsset = join(__dirname, '..', '..', '..');

    const slackBotTokenSecret = secrets.Secret.fromSecretNameV2(
      this, 'SlackBotToken', 'slack-bot-token'
    );
    const slackSigningSecret = secrets.Secret.fromSecretNameV2(
      this, 'SlackSigningSecret', 'slack-signing-secret'
    );

    // ── Slack Interaction Lambda ──────────────────────────────────────────

    const slackInteractionFn = new lambda.DockerImageFunction(this, 'SlackInteraction', {
      code: lambda.DockerImageCode.fromImageAsset(imageAsset, {
        cmd: ['launcher.handlers.slack.interaction.handler'],
      }),
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      environment: {
        STAGE: props.stage,
        LAUNCH_REQUESTS_TABLE: props.requestsTableName,
        LAUNCH_RULES_TABLE: props.rulesTableName,
        SLACK_BOT_TOKEN_SECRET: 'slack-bot-token',
        SLACK_SIGNING_SECRET: 'slack-signing-secret',
        SLACK_CHANNEL_ID: props.slackChannelId,
        APPROVE_LAUNCH_FUNCTION_NAME: props.approveLaunchFn.functionName,
      },
    });

    slackBotTokenSecret.grantRead(slackInteractionFn);
    slackSigningSecret.grantRead(slackInteractionFn);
    props.approveLaunchFn.grantInvoke(slackInteractionFn);

    slackInteractionFn.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'dynamodb:GetItem',
        'dynamodb:UpdateItem',
      ],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${props.requestsTableName}`,
      ],
    }));

    // ── Public API Gateway (no API key — Slack verifies via signing secret) ──

    const api = new apigateway.RestApi(this, 'SlackInteractionApi', {
      restApiName: 'gnome-launcher-slack-api',
    });

    const slackResource = api.root.addResource('slack');
    const interactResource = slackResource.addResource('interact');
    interactResource.addMethod(
      'POST',
      new apigateway.LambdaIntegration(slackInteractionFn),
    );

    new cdk.CfnOutput(this, 'SlackInteractionUrl', {
      value: `${api.url}slack/interact`,
      description: 'Set this as the Interactivity Request URL in your Slack App settings',
    });
  }
}
