"""Thin boto3 wrappers. Clients are injectable so handlers can be unit-tested without AWS."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

SERIAL_TAG = "paloalto:serial"


def _client(name: str):
    import boto3  # provided by the Lambda runtime

    return boto3.client(name)


class AwsOps:
    def __init__(self, *, ec2=None, autoscaling=None, secretsmanager=None, sns=None):
        self._ec2 = ec2
        self._asg = autoscaling
        self._sm = secretsmanager
        self._sns = sns

    @property
    def ec2(self):
        self._ec2 = self._ec2 or _client("ec2")
        return self._ec2

    @property
    def autoscaling(self):
        self._asg = self._asg or _client("autoscaling")
        return self._asg

    @property
    def secretsmanager(self):
        self._sm = self._sm or _client("secretsmanager")
        return self._sm

    @property
    def sns(self):
        self._sns = self._sns or _client("sns")
        return self._sns

    # -- secrets ----------------------------------------------------------
    def get_secret_json(self, secret_id: str) -> Dict[str, Any]:
        raw = self.secretsmanager.get_secret_value(SecretId=secret_id)["SecretString"]
        return json.loads(raw) if raw.strip().startswith("{") else _parse_kv(raw)

    def merge_secret_json(self, secret_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        try:
            current = self.get_secret_json(secret_id)
        except self.secretsmanager.exceptions.ResourceNotFoundException:
            current = None
        merged = dict(current or {})
        merged.update(updates)
        body = json.dumps(merged)
        if current is None:
            self.secretsmanager.create_secret(Name=secret_id, SecretString=body)
        else:
            self.secretsmanager.put_secret_value(SecretId=secret_id, SecretString=body)
        return merged

    # -- EC2 / ASG --------------------------------------------------------
    def describe_instances(self, instance_ids: List[str]) -> List[Dict[str, Any]]:
        if not instance_ids:
            return []
        out: List[Dict[str, Any]] = []
        paginator = self.ec2.get_paginator("describe_instances")
        for page in paginator.paginate(InstanceIds=instance_ids):
            for res in page.get("Reservations", []):
                out.extend(res.get("Instances", []))
        return out

    def asg_instance_ids(self, asg_name: str) -> List[str]:
        resp = self.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        groups = resp.get("AutoScalingGroups", [])
        if not groups:
            raise RuntimeError(f"ASG {asg_name!r} not found")
        return [
            i["InstanceId"] for i in groups[0].get("Instances", [])
            if not i.get("LifecycleState", "").startswith("Terminat")
        ]

    def tag_instance(self, instance_id: str, key: str, value: str) -> None:
        self.ec2.create_tags(Resources=[instance_id], Tags=[{"Key": key, "Value": value}])

    def complete_lifecycle(self, *, hook: str, asg: str, instance_id: str, token: Optional[str],
                           result: str = "CONTINUE") -> None:
        kwargs = dict(LifecycleHookName=hook, AutoScalingGroupName=asg,
                      InstanceId=instance_id, LifecycleActionResult=result)
        if token:
            kwargs["LifecycleActionToken"] = token
        self.autoscaling.complete_lifecycle_action(**kwargs)

    def notify(self, topic_arn: Optional[str], subject: str, message: Any) -> None:
        if not topic_arn:
            log.warning("ALERT (no SNS topic configured): %s %s", subject, message)
            return
        text = message if isinstance(message, str) else json.dumps(message, indent=2, default=str)
        self.sns.publish(TopicArn=topic_arn, Subject=subject[:100], Message=text)


def instance_tag(instance: Dict[str, Any], key: str) -> Optional[str]:
    for t in instance.get("Tags", []) or []:
        if t.get("Key") == key:
            return t.get("Value")
    return None


def mgmt_ip(instance: Dict[str, Any], device_index: int) -> Optional[str]:
    """Private IP of the management ENI. With mgmt-interface-swap=enable, mgmt is DeviceIndex 1."""
    for eni in instance.get("NetworkInterfaces", []) or []:
        if eni.get("Attachment", {}).get("DeviceIndex") == device_index:
            return eni.get("PrivateIpAddress")
    return None


def _parse_kv(raw: str) -> Dict[str, str]:
    """Parse PAN-OS bootstrap style 'k=v;k=v' or one-per-line text."""
    out: Dict[str, str] = {}
    for part in raw.replace("\n", ";").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def extract_d_authcode(value: str) -> Optional[str]:
    """Bootstrap 'authcodes' may hold several comma-separated codes; return the Software NGFW D- code."""
    for code in (value or "").split(","):
        code = code.strip()
        if code.upper().startswith("D"):
            return code
    return None
