import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';

// gnome-pipelines' DataStack owns the bucket and these parameters; gnomepy.data reads the parameters to find it.
const TABLE_BUCKET = 'gnome-research-tables';
const TABLE_REGION = 'us-east-1';
const PARAMETER_PREFIX = '/gnome/research/';

/** Lets a role read the research datasets through gnomepy.data (gnome-orchestrator grants sessions the same). */
export function grantResearchTablesRead(stack: cdk.Stack, grantee: iam.IGrantable): void {
  const bucketArn = `arn:aws:s3tables:${TABLE_REGION}:${stack.account}:bucket/${TABLE_BUCKET}`;
  grantee.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
    actions: [
      's3tables:GetTableBucket',
      's3tables:GetNamespace',
      's3tables:ListNamespaces',
      's3tables:ListTables',
      's3tables:GetTable',
      's3tables:GetTableMetadataLocation',
      's3tables:GetTableData',
    ],
    resources: [bucketArn, `${bucketArn}/table/*`],
  }));
  grantee.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
    actions: ['ssm:GetParameters'],
    resources: [`arn:aws:ssm:${TABLE_REGION}:${stack.account}:parameter${PARAMETER_PREFIX}*`],
  }));
}
