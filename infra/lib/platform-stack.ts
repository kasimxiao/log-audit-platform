import * as path from 'path';
import { Duration, RemovalPolicy, Stack, StackProps, CfnOutput, Aws, Size } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as ddb from 'aws-cdk-lib/aws-dynamodb';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as athena from 'aws-cdk-lib/aws-athena';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as apigw from 'aws-cdk-lib/aws-apigatewayv2';
import * as integ from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as authz from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import * as cf from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as sns from 'aws-cdk-lib/aws-sns';

export class LogAuditPlatformStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);
    const glueDb = 'logaudit', evalDb = 'logaudit_eval', wgName = 'logaudit';

    // ---------------- 存储
    const logBucket = new s3.Bucket(this, 'LogBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, encryption: s3.BucketEncryption.S3_MANAGED, enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY, autoDeleteObjects: true,
      lifecycleRules: [
        { prefix: 'staging/', expiration: Duration.days(7) },
        { prefix: 'athena-results/', expiration: Duration.days(30) },
      ],
      // 浏览器无需直连；主机侧 curl PUT 预签名 URL 不涉及 CORS
    });
    const webBucket = new s3.Bucket(this, 'WebBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, encryption: s3.BucketEncryption.S3_MANAGED, enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY, autoDeleteObjects: true,
    });
    const table = new ddb.Table(this, 'Table', {
      partitionKey: { name: 'pk', type: ddb.AttributeType.STRING }, sortKey: { name: 'sk', type: ddb.AttributeType.STRING },
      billingMode: ddb.BillingMode.PAY_PER_REQUEST, removalPolicy: RemovalPolicy.DESTROY,
    });

    // ---------------- Glue / Athena
    for (const [cid, name] of [['GlueDb', glueDb], ['GlueEvalDb', evalDb]]) {
      new glue.CfnDatabase(this, cid, { catalogId: Aws.ACCOUNT_ID, databaseInput: { name } });
    }
    new athena.CfnWorkGroup(this, 'WorkGroup', {
      name: wgName, recursiveDeleteOption: true, state: 'ENABLED',
      workGroupConfiguration: {
        enforceWorkGroupConfiguration: true, publishCloudWatchMetricsEnabled: true,
        bytesScannedCutoffPerQuery: 10 * 1024 ** 3,
        resultConfiguration: { outputLocation: `s3://${logBucket.bucketName}/athena-results/`, encryptionConfiguration: { encryptionOption: 'SSE_S3' } },
        engineVersion: { selectedEngineVersion: 'Athena engine version 3' },
      },
    });

    // ---------------- Cognito
    const pool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: 'log-audit-users', selfSignUpEnabled: false, signInAliases: { username: true },
      passwordPolicy: { minLength: 10, requireDigits: true, requireLowercase: true, requireUppercase: false, requireSymbols: false,
        tempPasswordValidity: Duration.days(7) },
      accountRecovery: cognito.AccountRecovery.NONE, removalPolicy: RemovalPolicy.DESTROY,
      standardAttributes: { fullname: { required: false, mutable: true }, email: { required: false, mutable: true } },
    });
    const client = pool.addClient('WebClient', {
      authFlows: { userPassword: true, userSrp: true }, generateSecret: false, preventUserExistenceErrors: true,
      idTokenValidity: Duration.hours(8), accessTokenValidity: Duration.hours(8), refreshTokenValidity: Duration.days(7),
    });
    new cognito.CfnUserPoolGroup(this, 'AdminGroup', { userPoolId: pool.userPoolId, groupName: 'admin', description: '平台管理员' });
    new cognito.CfnUserPoolGroup(this, 'UserGroup', { userPoolId: pool.userPoolId, groupName: 'user', description: '普通用户（查询）' });

    // ---------------- 主机实例角色（只给 SSM 托管能力；上传走预签名 URL）
    const instRole = new iam.Role(this, 'InstanceRole', {
      roleName: `LogAuditInstanceRole-${Aws.REGION}`,
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore')],
    });
    const instProfile = new iam.CfnInstanceProfile(this, 'InstanceProfile', {
      instanceProfileName: `LogAuditInstanceProfile-${Aws.REGION}`, roles: [instRole.roleName] });

    // ---------------- Lambda
    const code = lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'backend', 'build'));
    const env: Record<string, string> = {
      TABLE_NAME: table.tableName, LOG_BUCKET: logBucket.bucketName, GLUE_DB: glueDb, EVAL_DB: evalDb, ATHENA_WORKGROUP: wgName,
      USER_POOL_ID: pool.userPoolId, INSTANCE_PROFILE_ARN: instProfile.attrArn, ACCOUNT_ID: Aws.ACCOUNT_ID,
      NLQ_MODEL_ID: 'global.anthropic.claude-sonnet-5-5', JUDGE_MODEL_ID: 'global.anthropic.claude-haiku-5-5',
    };
    const mk = (id: string, handler: string, role: iam.Role, timeout: number, mem: number, extra: Partial<lambda.FunctionProps> = {}) =>
      new lambda.Function(this, id, {
        runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64, code, handler, role,
        timeout: Duration.seconds(timeout), memorySize: mem, environment: env,
        logGroup: new logs.LogGroup(this, `${id}Logs`, { retention: logs.RetentionDays.ONE_MONTH, removalPolicy: RemovalPolicy.DESTROY }),
        ...extra,
      });
    const basic = iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole');
    const apiRole = new iam.Role(this, 'ApiRole', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'), managedPolicies: [basic] });
    const workerRole = new iam.Role(this, 'WorkerRole', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'), managedPolicies: [basic] });
    const extractorRole = new iam.Role(this, 'ExtractorRole', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'), managedPolicies: [basic] });

    table.grantReadWriteData(apiRole); table.grantReadWriteData(workerRole); table.grantReadWriteData(extractorRole);
    logBucket.grantReadWrite(extractorRole);
    // 预签名上传（staging）+ Athena 结果
    logBucket.grantPut(apiRole, 'staging/*');
    for (const r of [apiRole, workerRole]) {
      logBucket.grantReadWrite(r, 'athena-results/*');
      r.addToPolicy(new iam.PolicyStatement({ actions: ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults',
        'athena:StopQueryExecution', 'athena:GetWorkGroup'], resources: [`arn:aws:athena:${Aws.REGION}:${Aws.ACCOUNT_ID}:workgroup/${wgName}`] }));
      // 平台日志桶只读（hostlogs/、synthetic/、eval/ 等）；ALB/CloudTrail 源桶的读权限由 SourceAccessFn 按已登记数据源动态写入
      logBucket.grantRead(r);
      r.addToPolicy(new iam.PolicyStatement({ actions: ['glue:GetDatabase', 'glue:GetDatabases', 'glue:GetTable', 'glue:GetTables',
        'glue:GetPartition', 'glue:GetPartitions'], resources: ['*'] }));
    }
    // 仅 api 角色可做 DDL（建/删 ALB 源表、CloudTrail 表）；worker（智能问答）无此能力
    apiRole.addToPolicy(new iam.PolicyStatement({ actions: ['glue:CreateTable', 'glue:DeleteTable', 'glue:UpdateTable', 'glue:CreateDatabase'],
      resources: [`arn:aws:glue:${Aws.REGION}:${Aws.ACCOUNT_ID}:catalog`, `arn:aws:glue:${Aws.REGION}:${Aws.ACCOUNT_ID}:database/${glueDb}`,
        `arn:aws:glue:${Aws.REGION}:${Aws.ACCOUNT_ID}:database/${evalDb}`, `arn:aws:glue:${Aws.REGION}:${Aws.ACCOUNT_ID}:database/default`,
        `arn:aws:glue:${Aws.REGION}:${Aws.ACCOUNT_ID}:table/${glueDb}/*`, `arn:aws:glue:${Aws.REGION}:${Aws.ACCOUNT_ID}:table/${evalDb}/*`] }));
    apiRole.addToPolicy(new iam.PolicyStatement({ actions: ['ec2:DescribeInstances', 'ec2:DescribeRegions', 'ec2:DescribeIamInstanceProfileAssociations',
      'elasticloadbalancing:DescribeLoadBalancers', 'elasticloadbalancing:DescribeLoadBalancerAttributes', 'cloudtrail:DescribeTrails',
      'ssm:DescribeInstanceInformation', 'ssm:GetCommandInvocation', 'ssm:ListCommandInvocations'], resources: ['*'] }));
    apiRole.addToPolicy(new iam.PolicyStatement({ actions: ['ec2:AssociateIamInstanceProfile', 'ec2:CreateTags'], resources: ['arn:aws:ec2:*:*:instance/*'] }));
    apiRole.addToPolicy(new iam.PolicyStatement({ actions: ['iam:PassRole'], resources: [instRole.roleArn] }));
    apiRole.addToPolicy(new iam.PolicyStatement({ actions: ['ssm:SendCommand'], resources: ['arn:aws:ec2:*:*:instance/*',
      'arn:aws:ssm:*::document/AWS-RunShellScript'] }));
    apiRole.addToPolicy(new iam.PolicyStatement({ actions: ['cognito-idp:ListUsers', 'cognito-idp:ListUsersInGroup', 'cognito-idp:AdminCreateUser',
      'cognito-idp:AdminAddUserToGroup', 'cognito-idp:AdminRemoveUserFromGroup', 'cognito-idp:AdminEnableUser', 'cognito-idp:AdminDisableUser',
      'cognito-idp:AdminSetUserPassword', 'cognito-idp:AdminDeleteUser'], resources: [pool.userPoolArn] }));
    workerRole.addToPolicy(new iam.PolicyStatement({ actions: ['bedrock:InvokeModel', 'bedrock:Converse'], resources: [
      'arn:aws:bedrock:*::foundation-model/anthropic.claude-*', `arn:aws:bedrock:*:${Aws.ACCOUNT_ID}:inference-profile/global.anthropic.claude-*`] }));

    // 告警通知（邮件订阅由管理员在平台内添加，收件人需点击确认）
    const alertTopic = new sns.Topic(this, 'AlertTopic', { topicName: 'log-audit-alerts', displayName: '日志审计告警' });
    env.ALERT_TOPIC_ARN = alertTopic.topicArn;
    alertTopic.grantPublish(workerRole);
    apiRole.addToPolicy(new iam.PolicyStatement({ actions: ['sns:Subscribe', 'sns:Unsubscribe', 'sns:ListSubscriptionsByTopic'],
      resources: [alertTopic.topicArn] }));
    apiRole.addToPolicy(new iam.PolicyStatement({ actions: ['sns:Unsubscribe'], resources: ['*'] }));  // Unsubscribe 以订阅 ARN 为资源
    const workerName = 'log-audit-worker';
    env.WORKER_FN = workerName;
    // 数据源读权限收敛：只允许改写 api/worker 角色上的内联策略 LogAuditSourceRead（策略由代码按桶名模板生成）
    const saRole = new iam.Role(this, 'SourceAccessRole', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'), managedPolicies: [basic] });
    saRole.addToPolicy(new iam.PolicyStatement({ actions: ['iam:PutRolePolicy', 'iam:GetRolePolicy'], resources: [apiRole.roleArn, workerRole.roleArn] }));
    const saFn = new lambda.Function(this, 'SourceAccessFn', { runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64, code,
      handler: 'source_access.handler', role: saRole, timeout: Duration.seconds(30), memorySize: 256,
      environment: { TARGET_ROLES: `${apiRole.roleName},${workerRole.roleName}` },
      logGroup: new logs.LogGroup(this, 'SourceAccessFnLogs', { retention: logs.RetentionDays.ONE_MONTH, removalPolicy: RemovalPolicy.DESTROY }) });
    saFn.grantInvoke(apiRole);
    env.SOURCE_ACCESS_FN = saFn.functionName;
    const worker = mk('WorkerFn', 'worker.handler', workerRole, 900, 1024, { functionName: workerName });
    new lambda.EventInvokeConfig(this, 'WorkerAsync', { function: worker, retryAttempts: 0, maxEventAge: Duration.hours(1) });
    workerRole.addToPolicy(new iam.PolicyStatement({ actions: ['lambda:InvokeFunction'],
      resources: [`arn:aws:lambda:${Aws.REGION}:${Aws.ACCOUNT_ID}:function:${workerName}`] }));
    const api = mk('ApiFn', 'api.handler', apiRole, 29, 1024);
    worker.grantInvoke(apiRole);
    const extractor = mk('ExtractorFn', 'extractor.handler', extractorRole, 900, 3008, { ephemeralStorageSize: Size.mebibytes(4096),
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'backend', 'build_extractor')) });
    // 入库时登记 hostlogs 分区（dt/instance_id/logtype）
    extractorRole.addToPolicy(new iam.PolicyStatement({ actions: ['glue:GetTable', 'glue:GetPartition', 'glue:BatchCreatePartition'],
      resources: [`arn:aws:glue:${Aws.REGION}:${Aws.ACCOUNT_ID}:catalog`, `arn:aws:glue:${Aws.REGION}:${Aws.ACCOUNT_ID}:database/${glueDb}`,
        `arn:aws:glue:${Aws.REGION}:${Aws.ACCOUNT_ID}:table/${glueDb}/hostlogs`] }));
    const syncFn = mk('SyncFn', 'syncsvc.scheduled', apiRole, 300, 512);
    logBucket.addEventNotification(s3.EventType.OBJECT_CREATED, new s3n.LambdaDestination(extractor), { prefix: 'staging/', suffix: '.tgz' });

    // 每日 02:00（Asia/Shanghai）同步
    const schedRole = new iam.Role(this, 'SchedulerRole', { assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com') });
    syncFn.grantInvoke(schedRole);
    worker.grantInvoke(schedRole);
    new scheduler.CfnSchedule(this, 'DailyAlerts', {
      name: 'log-audit-daily-alerts', scheduleExpression: 'cron(0 3 * * ? *)', scheduleExpressionTimezone: 'Asia/Shanghai',
      flexibleTimeWindow: { mode: 'OFF' }, target: { arn: worker.functionArn, roleArn: schedRole.roleArn, input: '{"task":"alerts","source":"scheduler"}' },
    });
    new scheduler.CfnSchedule(this, 'DailySync', {
      name: 'log-audit-daily-sync', scheduleExpression: 'cron(0 2 * * ? *)', scheduleExpressionTimezone: 'Asia/Shanghai',
      flexibleTimeWindow: { mode: 'OFF' }, target: { arn: syncFn.functionArn, roleArn: schedRole.roleArn, input: '{"source":"scheduler"}' },
    });

    // ---------------- HTTP API（JWT）
    const httpApi = new apigw.HttpApi(this, 'HttpApi', { apiName: 'log-audit-api', createDefaultStage: true });
    const jwt = new authz.HttpJwtAuthorizer('Jwt', `https://cognito-idp.${Aws.REGION}.amazonaws.com/${pool.userPoolId}`,
      { jwtAudience: [client.userPoolClientId] });
    httpApi.addRoutes({ path: '/api/{proxy+}', methods: [apigw.HttpMethod.ANY], authorizer: jwt,
      integration: new integ.HttpLambdaIntegration('ApiInteg', api) });

    // ---------------- CloudFront（唯一入口）
    const apiDomain = `${httpApi.apiId}.execute-api.${Aws.REGION}.amazonaws.com`;
    const dist = new cf.Distribution(this, 'Cdn', {
      comment: 'log-audit-platform', defaultRootObject: 'index.html',
      defaultBehavior: { origin: origins.S3BucketOrigin.withOriginAccessControl(webBucket), viewerProtocolPolicy: cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cf.CachePolicy.CACHING_DISABLED, responseHeadersPolicy: cf.ResponseHeadersPolicy.SECURITY_HEADERS },
      additionalBehaviors: { '/api/*': { origin: new origins.HttpOrigin(apiDomain), viewerProtocolPolicy: cf.ViewerProtocolPolicy.HTTPS_ONLY,
        allowedMethods: cf.AllowedMethods.ALLOW_ALL, cachePolicy: cf.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cf.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER } },
    });

    new CfnOutput(this, 'Url', { value: `https://${dist.distributionDomainName}` });
    new CfnOutput(this, 'DistributionId', { value: dist.distributionId });
    new CfnOutput(this, 'WebBucketName', { value: webBucket.bucketName });
    new CfnOutput(this, 'LogBucketName', { value: logBucket.bucketName });
    new CfnOutput(this, 'UserPoolId', { value: pool.userPoolId });
    new CfnOutput(this, 'UserPoolClientId', { value: client.userPoolClientId });
    new CfnOutput(this, 'InstanceProfileName', { value: instProfile.instanceProfileName! });
    new CfnOutput(this, 'ApiFnName', { value: api.functionName });
    new CfnOutput(this, 'WorkerFnName', { value: worker.functionName });
    new CfnOutput(this, 'TableName', { value: table.tableName });
    new CfnOutput(this, 'ApiRoleName', { value: apiRole.roleName });
    new CfnOutput(this, 'AlertTopicArn', { value: alertTopic.topicArn });
    new CfnOutput(this, 'SourceAccessFnName', { value: saFn.functionName });
    new CfnOutput(this, 'WorkerRoleName', { value: workerRole.roleName });
  }
}
