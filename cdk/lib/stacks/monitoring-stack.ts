import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { DefaultDashboardFactory, MonitoringFacade, noopAction } from 'cdk-monitoring-constructs';

interface Props extends cdk.StackProps {
  queues: { name: string; queue: sqs.Queue; dlq: sqs.Queue }[];
  functions: { name: string; fn: lambda.IFunction }[];
}

// The launcher's alarms. They carry no action: the registry's alerting stack hears every alarm change through
// EventBridge and reads the severity from the tier each is declared under. A message dead-lettered is a launch, or a
// shutdown, that never happened, so that's an Error; a queue backing up is a Warning.
export class LauncherMonitoringStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: Props) {
    super(scope, id, props);

    const monitoring = new MonitoringFacade(this, 'LauncherDashboard', {
      // Only the alarms are used; each dashboard is billed monthly and nobody reads them.
      dashboardFactory: new DefaultDashboardFactory(this, 'LauncherDashboardFactory', {
        dashboardNamePrefix: 'LauncherDashboard',
        createDashboard: false,
      }),
      alarmFactoryDefaults: {
        actionsEnabled: true,
        alarmNamePrefix: 'Launcher-',
        action: noopAction(),
        datapointsToAlarm: 1,
      },
    });

    for (const { name, queue, dlq } of props.queues) {
      monitoring.monitorSqsQueueWithDlq({
        queue,
        deadLetterQueue: dlq,
        humanReadableName: `${name} Queue`,
        alarmFriendlyName: `${name}Queue`,
        addDeadLetterQueueMaxSizeAlarm: {
          Error: { maxMessageCount: 0 },
        },
        addQueueMaxMessageAgeAlarm: {
          Warning: { maxAgeInSeconds: 15 * 60 },
        },
      });
    }
    for (const { name, fn } of props.functions) {
      monitoring.monitorLambdaFunction({
        lambdaFunction: fn,
        humanReadableName: name,
        alarmFriendlyName: name,
        addFaultCountAlarm: {
          Error: { maxErrorCount: 0 },
        },
      });
    }
  }
}
