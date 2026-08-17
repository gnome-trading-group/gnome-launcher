import { GnomeAccount, Stage } from '@gnome-trading-group/gnome-shared-cdk';

export const GITHUB_REPO = 'gnome-trading-group/gnome-launcher';
export const GITHUB_BRANCH = 'main';

export interface LauncherConfig {
  account: GnomeAccount;
  slackChannelId: string;
}

export const CONFIGS: { [stage in Stage]?: LauncherConfig } = {
  [Stage.DEV]: { account: GnomeAccount.InfraDev, slackChannelId: '' },
  [Stage.PROD]: { account: GnomeAccount.InfraProd, slackChannelId: 'C0BQNL97J3W' },
};
