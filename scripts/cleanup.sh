#!/bin/bash
# 删除 POC 全部资源（两套栈）。CloudTrail 原有 trail/桶不受影响（平台只读）。
set -euo pipefail
cd "$(dirname "$0")/../infra"
export AWS_REGION=${LA_REGION:-ap-northeast-1} AWS_DEFAULT_REGION=${LA_REGION:-ap-northeast-1}
# SourceAccessFn 在 api/worker 角色上写的内联策略不由 CloudFormation 管理，先删掉，否则角色删除会失败
for R in $(aws cloudformation describe-stacks --stack-name LogAuditPlatform --query "Stacks[0].Outputs[?OutputKey=='ApiRoleName' || OutputKey=='WorkerRoleName'].OutputValue" --output text 2>/dev/null); do
  aws iam delete-role-policy --role-name "$R" --policy-name LogAuditSourceRead 2>/dev/null || true
done
# 测试环境栈只在传入 vpcId 时才实例化：从已部署栈的主机反查 VPC（未部署则只删平台栈）
STACKS=(LogAuditPlatform); CTX=(-c "region=$AWS_REGION")
HOSTS=$(aws cloudformation describe-stacks --stack-name LogAuditTestEnv --query "Stacks[0].Outputs[?OutputKey=='Hosts'].OutputValue" --output text 2>/dev/null || true)
if [ -n "$HOSTS" ] && [ "$HOSTS" != "None" ]; then
  VPC=${LA_VPC_ID:-$(aws ec2 describe-instances --instance-ids ${HOSTS//,/ } --query 'Reservations[0].Instances[0].VpcId' --output text)}
  STACKS=(LogAuditTestEnv LogAuditPlatform); CTX+=(-c "vpcId=$VPC")
fi
npx cdk destroy "${STACKS[@]}" "${CTX[@]}" --force
aws glue delete-database --name logaudit 2>/dev/null || true
aws glue delete-database --name logaudit_eval 2>/dev/null || true
