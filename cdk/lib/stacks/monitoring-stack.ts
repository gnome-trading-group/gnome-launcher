import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';

interface Props extends cdk.StackProps {
  queues: { name: string; queue: sqs.Queue; dlq: sqs.Queue }[];
  functions: { name: string; fn: lambda.IFunction }[];
}

// The launcher's alarms. They carry no action: the registry's alerting stack hears every alarm change through
// EventBridge and reads the severity from the end of the name. A message dead-lettered is a launch, or a shutdown,
// that never happened, so that's an Error; a queue backing up is a Warning.
export class LauncherMonitoringStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: Props) {
    super(scope, id, props);

    const alarm = (name: string, tier: 'Error' | 'Warning', metric: cloudwatch.Metric, threshold: number) =>
      metric.createAlarm(this, name, {
        alarmName: `Launcher-${name}-${tier}`,
        threshold,
        evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    const fiveMinutes = cdk.Duration.minutes(5);

    for (const { name, queue, dlq } of props.queues) {
      alarm(`${name}Dlq`, 'Error', dlq.metricApproximateNumberOfMessagesVisible({ period: fiveMinutes }), 1);
      alarm(`${name}QueueAge`, 'Warning',
        queue.metricApproximateAgeOfOldestMessage({ period: fiveMinutes, statistic: 'Maximum' }), 15 * 60);
    }
    for (const { name, fn } of props.functions) {
      alarm(`${name}Errors`, 'Error', fn.metricErrors({ period: fiveMinutes, statistic: 'Sum' }), 1);
    }
  }
}
