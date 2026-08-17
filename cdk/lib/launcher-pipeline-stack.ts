import * as cdk from 'aws-cdk-lib';
import * as pipelines from 'aws-cdk-lib/pipelines';
import * as secrets from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import { Stage } from '@gnome-trading-group/gnome-shared-cdk';
import { CONFIGS, GITHUB_BRANCH, GITHUB_REPO, LauncherConfig } from './config';
import { LauncherStack } from './stacks/launcher-stack';
import { LauncherSlackInteractionStack } from './stacks/slack-interaction-stack';


class AppStage extends cdk.Stage {
  constructor(scope: Construct, id: string, config: LauncherConfig) {
    super(scope, id, { env: config.account.environment });

    const launcherStack = new LauncherStack(this, 'LauncherStack', {
      stage: config.account.stage,
      slackChannelId: config.slackChannelId,
    });

    new LauncherSlackInteractionStack(this, 'LauncherSlackInteractionStack', {
      stage: config.account.stage,
      slackChannelId: config.slackChannelId,
      approveLaunchFn: launcherStack.approveLaunchFn,
      requestsTableName: 'gnome-launch-requests',
      rulesTableName: 'gnome-launch-rules',
    });
  }
}

export class LauncherPipelineStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const npmSecret = secrets.Secret.fromSecretNameV2(this, 'NPMToken', 'npm-token');
    const dockerHubCredentials = secrets.Secret.fromSecretNameV2(this, 'DockerHub', 'docker-hub-credentials');

    const pipeline = new pipelines.CodePipeline(this, 'LauncherPipeline', {
      crossAccountKeys: true,
      pipelineName: 'LauncherPipeline',
      synth: new pipelines.ShellStep('deploy', {
        input: pipelines.CodePipelineSource.gitHub(GITHUB_REPO, GITHUB_BRANCH),
        commands: [
          'echo "//npm.pkg.github.com/:_authToken=${NPM_TOKEN}" > ~/.npmrc',
          'cd cdk/',
          'npm ci',
          'npx cdk synth',
        ],
        env: {
          NPM_TOKEN: npmSecret.secretValue.unsafeUnwrap(),
        },
        primaryOutputDirectory: 'cdk/cdk.out',
      }),
      dockerCredentials: [
        pipelines.DockerCredential.dockerHub(dockerHubCredentials),
      ],
    });

    const dev = new AppStage(this, 'Dev', CONFIGS[Stage.DEV]!);
    const prod = new AppStage(this, 'Prod', CONFIGS[Stage.PROD]!);

    pipeline.addStage(dev);
    pipeline.addStage(prod, {
      pre: [new pipelines.ManualApprovalStep('ApproveProd')],
    });

    pipeline.buildPipeline();
    npmSecret.grantRead(pipeline.synthProject.role!!);
    npmSecret.grantRead(pipeline.pipeline.role);
  }
}
