import { Duration, RemovalPolicy, Stack, StackProps, CfnOutput, Tags } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as targets from 'aws-cdk-lib/aws-elasticloadbalancingv2-targets';

/** POC 测试环境：3 台 Linux 主机（含 1 台未挂实例角色）+ 2 个内网 ALB（日志进 2 个不同桶）+ 流量/攻击模拟。
 *  全部在私有子网，无公网入口。 */
export class LogAuditTestEnvStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps & { vpcId: string }) {
    super(scope, id, props);
    const vpc = ec2.Vpc.fromLookup(this, 'Vpc', { vpcId: props.vpcId });
    const priv = { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS };

    const albSg = new ec2.SecurityGroup(this, 'AlbSg', { vpc, description: 'log-audit test ALB (internal)', allowAllOutbound: true });
    albSg.addIngressRule(ec2.Peer.ipv4(vpc.vpcCidrBlock), ec2.Port.tcp(80), 'VPC internal only');
    const hostSg = new ec2.SecurityGroup(this, 'HostSg', { vpc, description: 'log-audit test hosts', allowAllOutbound: true });
    hostSg.addIngressRule(albSg, ec2.Port.tcp(80), 'from ALB');
    hostSg.addIngressRule(hostSg, ec2.Port.tcp(22), 'ssh between test hosts (brute-force simulation)');

    const role = new iam.Role(this, 'HostRole', { assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore')] });

    const al2023 = ec2.MachineImage.latestAmazonLinux2023({ cpuType: ec2.AmazonLinuxCpuType.ARM_64 });
    const ubuntu = ec2.MachineImage.fromSsmParameter('/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id');

    // 公共：开启密码认证（仅 VPC 内可达）以产生 Failed password；制造典型审计事件
    const auditEvents = [
      'useradd -m demo-ops || true', 'echo "demo-ops:Demo-Passw0rd-$(date +%s)" | chpasswd',
      'sudo -u demo-ops true', '(crontab -l 2>/dev/null; echo "*/30 * * * * /usr/bin/true # demo") | crontab -',
    ];
    const mkHost = (name: string, image: ec2.IMachineImage, ud: string[], withRole: boolean, osTag: string) => {
      const userData = ec2.UserData.forLinux();
      userData.addCommands(...ud);
      const inst = new ec2.Instance(this, name, {
        vpc, vpcSubnets: priv, instanceType: new ec2.InstanceType('t4g.small'), machineImage: image, securityGroup: hostSg,
        role: withRole ? role : undefined, userData, requireImdsv2: true,
      });
      if (!withRole) {
        // 不挂实例角色：模拟"未纳管"主机（CDK 默认会建角色，这里移除）
        (inst.node.defaultChild as ec2.CfnInstance).addPropertyDeletionOverride('IamInstanceProfile');
      }
      Tags.of(inst).add('Name', name);
      Tags.of(inst).add('Project', 'log-audit-poc');
      Tags.of(inst).add('OS', osTag);
      return inst;
    };

    // 1) AL2023 + nginx + rsyslog（模拟"装了 rsyslog 的 AL"，有 /var/log/secure、/var/log/cron）
    const web = mkHost('la-web-01', al2023, [
      'dnf install -y nginx rsyslog cronie',
      'systemctl enable --now rsyslog; systemctl enable --now crond; systemctl enable --now nginx',
      "sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; rm -f /etc/ssh/sshd_config.d/*disable*password* ; systemctl restart sshd",
      ...auditEvents,
    ], true, 'al2023');
    // 2) Ubuntu 24.04 + apache2 + auditd，并作为"攻击者"对另外两台做 SSH 密码爆破
    const ubu = mkHost('la-ubuntu-02', ubuntu, [
      'export DEBIAN_FRONTEND=noninteractive; apt-get update -y; apt-get install -y apache2 auditd sshpass curl',
      'systemctl enable --now apache2 auditd',
      "sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; rm -f /etc/ssh/sshd_config.d/60-cloudimg-settings.conf; systemctl restart ssh",
      ...auditEvents,
    ], true, 'ubuntu');
    // 3) AL2023 不挂实例角色（SSM 未纳管），nginx
    const legacy = mkHost('la-legacy-03', al2023, ['dnf install -y nginx', 'systemctl enable --now nginx'], false, 'al2023');

    // ALB 日志桶 ×2（演示"多桶可配置"）
    const mkBucket = (id: string) => new s3.Bucket(this, id, { blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED, enforceSSL: true, removalPolicy: RemovalPolicy.DESTROY, autoDeleteObjects: true,
      lifecycleRules: [{ expiration: Duration.days(30) }] });
    const bucketA = mkBucket('AlbLogsA'), bucketB = mkBucket('AlbLogsB');

    const mkAlb = (name: string, target: ec2.Instance, bucket: s3.Bucket, prefix?: string) => {
      const lb = new elbv2.ApplicationLoadBalancer(this, name, { vpc, internetFacing: false, vpcSubnets: priv, securityGroup: albSg,
        loadBalancerName: name, dropInvalidHeaderFields: true });
      lb.logAccessLogs(bucket, prefix);
      lb.addListener('Http', { port: 80, open: false }).addTargets('T', { port: 80, targets: [new targets.InstanceTarget(target, 80)],
        healthCheck: { path: '/', healthyHttpCodes: '200-404' } });
      return lb;
    };
    const albA = mkAlb('la-shop-web', web, bucketA);
    const albB = mkAlb('la-shop-api', ubu, bucketB, 'prod');

    // 流量与 SSH 爆破模拟由 scripts/testenv_setup.sh 部署后经 SSM 下发到 la-ubuntu-02（避免 ALB↔实例 循环依赖）
    legacy.userData.addCommands("sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; systemctl restart sshd");

    new CfnOutput(this, 'AlbLogsBucketA', { value: bucketA.bucketName });
    new CfnOutput(this, 'AlbLogsBucketB', { value: bucketB.bucketName });
    new CfnOutput(this, 'AlbDnsA', { value: albA.loadBalancerDnsName });
    new CfnOutput(this, 'AlbDnsB', { value: albB.loadBalancerDnsName });
    new CfnOutput(this, 'Hosts', { value: [web, ubu, legacy].map(h => h.instanceId).join(',') });
  }
}
