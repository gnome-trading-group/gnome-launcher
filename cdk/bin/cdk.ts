#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { LauncherPipelineStack } from '../lib/launcher-pipeline-stack';
import { GnomeAccount } from '@gnome-trading-group/gnome-shared-cdk';

const app = new cdk.App();
new LauncherPipelineStack(app, 'LauncherPipelineStack', {
  env: GnomeAccount.InfraPipelines.environment,
});
app.synth();
