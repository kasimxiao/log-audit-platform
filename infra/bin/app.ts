#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { LogAuditPlatformStack } from '../lib/platform-stack';
import { LogAuditTestEnvStack } from '../lib/testenv-stack';

const app = new App();
// 部署参数：-c region=<区域>（默认 ap-northeast-1）；-c vpcId=<vpc-xxx>（测试环境所在 VPC，需带 NAT 的私有子网）
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: app.node.tryGetContext('region') || 'ap-northeast-1' };
new LogAuditPlatformStack(app, 'LogAuditPlatform', { env });
const vpcId: string | undefined = app.node.tryGetContext('vpcId');
if (vpcId) {
  new LogAuditTestEnvStack(app, 'LogAuditTestEnv', { env, vpcId });
} else {
  console.error('[提示] 未传入 -c vpcId=<vpc-xxx>，跳过测试环境栈 LogAuditTestEnv（仅平台栈 LogAuditPlatform 可部署）');
}
