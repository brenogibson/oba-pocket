"""Ícones oficiais dos serviços da AWS para os balões.

O agente manda o nome do serviço ("Amazon SQS", "sqs", "AWS Lambda") e o
roteador troca pelo PNG de 64x64 do pacote de ícones de arquitetura da AWS, que
o tools/fetch_icons.py baixa para icons/ (fora do repo).
"""

import base64
import re
import unicodedata
from pathlib import Path

ICONS_DIR = Path(__file__).parent / "icons"

# Siglas que o agente (ou a transcrição) usa no lugar do nome oficial
ALIASES = {
    "sqs": "Amazon-Simple-Queue-Service", "sns": "Amazon-Simple-Notification-Service",
    "ses": "Amazon-Simple-Email-Service", "s3": "Amazon-Simple-Storage-Service",
    "glacier": "Amazon-Simple-Storage-Service-Glacier", "ec2": "Amazon-EC2",
    "ecs": "Amazon-Elastic-Container-Service", "eks": "Amazon-Elastic-Kubernetes-Service",
    "ecr": "Amazon-Elastic-Container-Registry", "rds": "Amazon-RDS", "aurora": "Amazon-Aurora",
    "dynamodb": "Amazon-DynamoDB", "dynamo": "Amazon-DynamoDB", "iam": "AWS-Identity-and-Access-Management",
    "vpc": "Amazon-Virtual-Private-Cloud", "kms": "AWS-Key-Management-Service",
    "msk": "Amazon-Managed-Streaming-for-Apache-Kafka", "kafka": "Amazon-Managed-Streaming-for-Apache-Kafka",
    "efs": "Amazon-EFS", "ebs": "Amazon-Elastic-Block-Store", "elb": "Elastic-Load-Balancing",
    "alb": "Elastic-Load-Balancing", "nlb": "Elastic-Load-Balancing", "quicksight": "Amazon-Quick",
    "kinesis": "Amazon-Kinesis-Data-Streams", "firehose": "Amazon-Data-Firehose",
    "sagemaker": "Amazon-SageMaker-AI", "agentcore": "Amazon-Bedrock-AgentCore", "bedrock": "Amazon-Bedrock",
    "lambda": "AWS-Lambda", "fargate": "AWS-Fargate", "cdk": "AWS-Cloud-Development-Kit",
    "waf": "AWS-WAF", "acm": "AWS-Certificate-Manager", "sso": "AWS-IAM-Identity-Center",
    "cloudwatch": "Amazon-CloudWatch", "cloudfront": "Amazon-CloudFront", "route53": "Amazon-Route-53",
    "opensearch": "Amazon-OpenSearch-Service", "elasticsearch": "Amazon-OpenSearch-Service",
    "elasticache": "Amazon-ElastiCache", "redis": "Amazon-ElastiCache", "iot": "AWS-IoT-Core",
    "greengrass": "AWS-IoT-Greengrass", "step": "AWS-Step-Functions", "q": "Amazon-Q",
}


def norm(s: str) -> str:
    """Minúsculas, sem acento nem hífen ("e-mail" = "email"); igual ao foldText do firmware."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("-", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _strip_vendor(s: str) -> str:
    return " ".join(w for w in s.split() if w not in ("amazon", "aws"))


ICONS = {_strip_vendor(norm(p.stem.replace("-", " "))): p.stem for p in ICONS_DIR.glob("*.png")}


def resolve_icon(*names: str | None) -> str | None:
    """Nome do serviço -> arquivo do ícone oficial (ou None)."""
    for name in filter(None, names):
        if not isinstance(name, str):
            continue
        n = _strip_vendor(norm(name))
        if n in ICONS:
            return ICONS[n]
        for w in n.split():  # "Amazon SQS", "SNS + SES": a primeira sigla conhecida
            if w in ALIASES and ALIASES[w] in ICONS.values():
                return ALIASES[w]
        # Nome parcial: o ícone cujo nome inteiro aparece no texto (o mais longo)
        best = max((k for k in ICONS if f" {k} " in f" {n} "), key=len, default=None)
        if best:
            return ICONS[best]
    return None


def icon_png(slug: str) -> str:
    """O PNG do ícone em base64, como vai no balão."""
    return base64.b64encode((ICONS_DIR / f"{slug}.png").read_bytes()).decode()
