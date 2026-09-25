#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/autotriage-user-data.log) 2>&1

CONTAINER_NAME="${container_name}"
AWS_REGION="${aws_region}"
LOG_GROUP_NAME="${log_group_name}"

dnf update -y
dnf install -y docker unzip jq cronie
systemctl enable --now docker
systemctl enable --now crond

if ! command -v aws >/dev/null 2>&1; then
  curl -sSL "https://awscliv2.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  unzip -q /tmp/awscliv2.zip -d /tmp
  /tmp/aws/install
fi

curl -sSL "https://amazoncloudwatch-agent-$AWS_REGION.s3.$AWS_REGION.amazonaws.com/amazon_linux/amd64/latest/amazon-cloudwatch-agent.rpm" \
  -o /tmp/amazon-cloudwatch-agent.rpm
rpm -U /tmp/amazon-cloudwatch-agent.rpm

mkdir -p /opt/autotriage/app /var/log/autotriage
chmod 777 /var/log/autotriage

base64 -d <<< "${app_main_py_b64}" > /opt/autotriage/app/main.py
base64 -d <<< "${app_requirements_txt_b64}" > /opt/autotriage/app/requirements.txt
base64 -d <<< "${app_dockerfile_b64}" > /opt/autotriage/app/Dockerfile

cat > /opt/aws/amazon-cloudwatch-agent/etc/config.json <<CWCONFIG
{
  "agent": { "metrics_collection_interval": 60, "run_as_user": "root" },
  "metrics": {
    "namespace": "CWAgent",
    "append_dimensions": { "InstanceId": "$${aws:InstanceId}" },
    "metrics_collected": {
      "disk": {
        "measurement": ["used_percent"],
        "resources": ["/"],
        "drop_device": true,
        "metrics_collection_interval": 60
      },
      "mem": {
        "measurement": ["mem_used_percent"],
        "metrics_collection_interval": 60
      }
    }
  },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/autotriage/app.log",
            "log_group_name": "$LOG_GROUP_NAME",
            "log_stream_name": "{instance_id}/app",
            "timestamp_format": "%Y-%m-%dT%H:%M:%SZ"
          }
        ]
      }
    }
  }
}
CWCONFIG

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json

cd /opt/autotriage/app
docker build -t "$CONTAINER_NAME:latest" .
docker run -d --name "$CONTAINER_NAME" --restart=no -p 8000:8000 \
  -v /var/log/autotriage:/var/log/autotriage \
  "$CONTAINER_NAME:latest"

# --- heartbeat: publishes AutoTriage/ContainerRunning every minute so the
# "container down" alarm has something to key off of (the app itself can't
# report its own death once os._exit(1) has been called).
cat > /etc/default/autotriage <<ENVFILE
CONTAINER_NAME=$CONTAINER_NAME
AWS_REGION=$AWS_REGION
ENVFILE

cat > /opt/autotriage/heartbeat.sh <<'HEARTBEAT'
#!/bin/bash
set -euo pipefail
. /etc/default/autotriage
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
RUNNING=$(docker ps -q -f "name=^$CONTAINER_NAME$" -f status=running)
if [ -n "$RUNNING" ]; then
  VALUE=1
else
  VALUE=0
fi
aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace AutoTriage \
  --metric-name ContainerRunning --dimensions InstanceId=$INSTANCE_ID --value $VALUE
HEARTBEAT
chmod +x /opt/autotriage/heartbeat.sh

echo "* * * * * root /opt/autotriage/heartbeat.sh >> /var/log/autotriage-heartbeat.log 2>&1" > /etc/cron.d/autotriage-heartbeat
chmod 644 /etc/cron.d/autotriage-heartbeat
