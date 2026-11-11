#!/bin/bash
# 经 SSM 在 la-ubuntu-02 下发：每分钟访问两个内网 ALB（含少量扫描路径），每 10 分钟对另两台做 SSH 密码爆破
set -euo pipefail
export AWS_REGION=${LA_REGION:-ap-northeast-1} AWS_DEFAULT_REGION=${LA_REGION:-ap-northeast-1}
O() { aws cloudformation describe-stacks --stack-name LogAuditTestEnv --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
A=$(O AlbDnsA); B=$(O AlbDnsB); IFS=, read WEB UBU LEG <<< "$(O Hosts)"
IPS=$(aws ec2 describe-instances --instance-ids $WEB $LEG --query 'Reservations[].Instances[].PrivateIpAddress' --output text)
SCRIPT=$(cat <<EOF
cat > /usr/local/bin/la-traffic.sh <<'X'
#!/bin/bash
ALBS=($A $B)
P=(/ /index.html /product/1001 /product/1002 /cart /api/v1/products /api/v1/orders /static/app.js /nope-404)
S=(/.env /wp-login.php /phpmyadmin/ /.git/config)
for i in \$(seq 1 30); do H=\${ALBS[\$((RANDOM % 2))]}; curl -s -o /dev/null -m 3 -A "Mozilla/5.0 la-sim" "http://\$H\${P[\$((RANDOM % 9))]}"; done
[ \$((RANDOM % 10)) -eq 0 ] && for s in "\${S[@]}"; do curl -s -o /dev/null -m 3 -A "zgrab/0.x" "http://\${ALBS[0]}\$s"; done
exit 0
X
cat > /usr/local/bin/la-attack.sh <<'X'
#!/bin/bash
for T in $IPS; do for u in root admin oracle test; do
  sshpass -p "wrong\$RANDOM" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=3 -o PreferredAuthentications=password -o PubkeyAuthentication=no \$u@\$T true 2>/dev/null
done; done; exit 0
X
chmod +x /usr/local/bin/la-traffic.sh /usr/local/bin/la-attack.sh
command -v sshpass >/dev/null || (apt-get update -y && apt-get install -y sshpass)
echo "* * * * * root /usr/local/bin/la-traffic.sh" > /etc/cron.d/la-traffic
echo "*/10 * * * * root /usr/local/bin/la-attack.sh" > /etc/cron.d/la-attack
/usr/local/bin/la-traffic.sh; /usr/local/bin/la-attack.sh; echo setup-done
EOF
)
CID=$(aws ssm send-command --instance-ids $UBU --document-name AWS-RunShellScript --parameters "$(jq -n --arg c "$SCRIPT" '{commands:[$c]}')" --query Command.CommandId --output text)
sleep 25; aws ssm get-command-invocation --command-id $CID --instance-id $UBU --query '[Status,StandardOutputContent,StandardErrorContent]' --output text | tail -5
