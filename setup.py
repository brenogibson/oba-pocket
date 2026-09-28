#!/usr/bin/env python3
"""Sobe (ou atualiza) o Oba Pocket numa conta da AWS a partir do config.json.

  cp config.example.json config.json        # e ajuste
  python3 -m venv .venv && .venv/bin/pip install boto3 jsonschema
  .venv/bin/python setup.py [--config config.json] [--new-cert]

Usa o perfil padrão da AWS CLI (ou AWS_PROFILE). Pode rodar de novo quantas
vezes quiser: só muda o que mudou. Cada peça recebe o mínimo de permissão:

  placa    certificado do thing -> policy só em <p>/<thing>/..., e um role alias
           que troca o certificado por credenciais do Transcribe (só streaming)
  tela     Cognito sem login -> role que só lê <p>/<placa>/#
  harness  IoT Rule -> Lambda do roteador (sessões no DynamoDB, Obas no S3,
           publica só em cmd e ui/*) -> agente no AgentCore (só os modelos da lista)

Gera src/aws_config.h, display/config.js e src/secrets.h. O WiFi de um
secrets.h que já existe é mantido; a chave privada nasce nesta máquina e nunca
vai para a AWS (ela só assina um CSR). Para empacotar a Lambda e o agente
precisa do uv (ou do pip); para o certificado, do openssl.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
SECRETS = ROOT / "src" / "secrets.h"
AGENT_SRC = ROOT / "harness" / "agent"
ROUTER_SRC = ROOT / "harness" / "router"
sys.path.insert(0, str(ROOT / "tools"))

CRED_SECONDS = 3600        # credenciais do Transcribe na placa
ROUTER_TIMEOUT = 600       # até 3 rodadas do agente numa invocação (harness/router)
ROUTER_CONCURRENCY = 5
LOG_DAYS = 30
MODEL_GEOS = {"us", "eu", "apac", "jp", "au", "ca", "global"}  # prefixos de perfis de inferência
VOCAB_WORD = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ]+(?:-[A-Za-zÀ-ÖØ-öø-ÿ]+)*$")  # letras e hífen, sem dígitos

REPORT = []


# ---------- utilidades ----------

def note(what, name, status):
    REPORT.append((what, name, status))
    print(f"  {status:<11} {what}: {hide(name)}")


def hide(s) -> str:
    return str(s).replace(ACCOUNT, "<conta>")


def fail(msg):
    sys.exit(f"erro: {msg}")


def doc(*statements) -> dict:
    return {"Version": "2012-10-17", "Statement": list(statements)}


def allow(sid, action, resource, **extra) -> dict:
    return {"Sid": sid, "Effect": "Allow", "Action": action, "Resource": resource, **extra}


def tree_sha(files, base: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(str(f.relative_to(base)).encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:32]


def zip_tree(pkg: Path) -> bytes:
    """Zip reprodutível: ordem, data e permissões fixas."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pkg.rglob("*")):
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            info = zipfile.ZipInfo(str(f.relative_to(pkg)), date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = (0o755 if f.stat().st_mode & 0o111 else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
    return buf.getvalue()


def pip_target(target: Path, *reqs):
    """Pacotes para Linux arm64 / Python 3.13 (Lambda e AgentCore)."""
    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "-q", "--python-platform", "aarch64-manylinux2014", "--python-version", "3.13"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-q", "--platform", "manylinux2014_aarch64",
               "--implementation", "cp", "--python-version", "3.13"]
    subprocess.run(cmd + ["--target", str(target), "--only-binary=:all:", *reqs], check=True)


def staging(name: str) -> Path:
    pkg = BUILD / name
    shutil.rmtree(pkg, ignore_errors=True)
    pkg.mkdir(parents=True)
    return pkg


def ensure_role(name, trust, policy, desc) -> str:
    try:
        role = iam.get_role(RoleName=name)["Role"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description=desc)["Role"]["Arn"]
        iam.put_role_policy(RoleName=name, PolicyName=name, PolicyDocument=json.dumps(policy))
        note("role", name, "criado")
        return arn
    changed = False
    if role["AssumeRolePolicyDocument"] != trust:
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        changed = True
    try:
        cur = iam.get_role_policy(RoleName=name, PolicyName=name)["PolicyDocument"]
    except iam.exceptions.NoSuchEntityException:
        cur = None
    if cur != policy:
        iam.put_role_policy(RoleName=name, PolicyName=name, PolicyDocument=json.dumps(policy))
        changed = True
    note("role", name, "atualizado" if changed else "mantido")
    return role["Arn"]


def ensure_iot_policy(name, document):
    try:
        cur = json.loads(iot.get_policy(policyName=name)["policyDocument"])
    except iot.exceptions.ResourceNotFoundException:
        iot.create_policy(policyName=name, policyDocument=json.dumps(document))
        return note("policy IoT", name, "criado")
    if cur == document:
        return note("policy IoT", name, "mantido")
    versions = iot.list_policy_versions(policyName=name)["policyVersions"]
    if len(versions) >= 5:  # limite da IoT: sai a versão não padrão mais antiga
        old = min((v for v in versions if not v["isDefaultVersion"]), key=lambda v: int(v["versionId"]))
        iot.delete_policy_version(policyName=name, policyVersionId=old["versionId"])
    iot.create_policy_version(policyName=name, policyDocument=json.dumps(document), setAsDefault=True)
    note("policy IoT", name, "atualizado")


def retain_logs(group):
    try:
        logs.create_log_group(logGroupName=group)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=group, retentionInDays=LOG_DAYS)


# ---------- configuração ----------

def load_config(path: Path) -> dict:
    if not path.is_file():
        fail(f"{path.name} não existe: cp config.example.json config.json e ajuste")
    cfg = json.loads(path.read_text())
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", cfg.get("prefix", "")):
        fail("prefix: letras minúsculas, números e hífen (2 a 31), começando por letra")
    if not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", cfg.get("device", "")):
        fail("device: letras, números, _, : e - (é o nome do thing no IoT)")
    if not cfg.get("models"):
        fail("models: pelo menos um modelo")
    for name, a in (cfg.get("agents") or {}).items():
        if a.get("type") not in ("agentcore", "http") or (a["type"] == "http" and not a.get("url")):
            fail(f"agents.{name}: use {{\"type\": \"agentcore\"[, \"arn\"]}} ou {{\"type\": \"http\", \"url\"}}")
    return cfg


def init(cfg):
    global CFG, P, P_, DEV, REGION, TR_REGION, LANG, ACCOUNT, IOT_ARN, BUCKET
    global iam, iot, s3, ddb, lam, ctl, cog, tr, logs
    CFG, P, DEV, REGION = cfg, cfg["prefix"], cfg["device"], cfg.get("region", "us-east-1")
    P_ = P.replace("-", "_")  # nomes que não aceitam hífen (IoT Rule, AgentCore, Cognito)
    cfg.setdefault("transcribe", {})
    TR_REGION = cfg["transcribe"].get("region", REGION)
    LANG = cfg["transcribe"].get("language", "pt-BR")
    ACCOUNT = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    IOT_ARN = f"arn:aws:iot:{REGION}:{ACCOUNT}"
    BUCKET = f"{P}-{ACCOUNT}"
    iam = boto3.client("iam")
    iot = boto3.client("iot", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    ddb = boto3.client("dynamodb", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)
    ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    cog = boto3.client("cognito-identity", region_name=REGION)
    tr = boto3.client("transcribe", region_name=TR_REGION)
    logs = boto3.client("logs", region_name=REGION)


# ---------- registro de Obas (S3) e sessões (DynamoDB) ----------

def collect_obas() -> dict:
    import oba as obatool
    found = {}
    for d in CFG.get("obas") or ["obas"]:
        for f in sorted((ROOT / d).glob("*/oba.json")):
            errs = obatool.validate(f.parent)
            if errs:
                print(f"  pulando {f.parent.relative_to(ROOT)}: {errs[0]}" + (f" (+{len(errs) - 1})" if len(errs) > 1 else ""))
                continue
            spec = obatool.load(f.parent)[2]
            found[spec["id"]] = (f, spec)
    if not found:
        fail("nenhum Oba válido nas pastas de \"obas\" do config.json")
    return found


def ensure_bucket():
    try:
        s3.head_bucket(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT)
        status = "mantido"
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            fail(f"bucket {hide(BUCKET)}: {e}")
        extra = {} if REGION == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": REGION}}
        s3.create_bucket(Bucket=BUCKET, **extra)
        status = "criado"
    s3.put_public_access_block(Bucket=BUCKET, PublicAccessBlockConfiguration={k: True for k in (
        "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")})
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(doc({
        "Sid": "OnlyTls", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}}})))
    s3.put_bucket_lifecycle_configuration(Bucket=BUCKET, LifecycleConfiguration={"Rules": [{
        "ID": "old-versions", "Status": "Enabled", "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}]})
    note("bucket", BUCKET, status)


def upload_obas(obas: dict):
    """obas/<id>/oba.json e os arquivos de "files" (sprites, sons), pulando o que não mudou."""
    import oba_remote
    for oid, (path, spec) in obas.items():
        r = oba_remote.registry_sync(s3, BUCKET, path.parent, spec, owner=ACCOUNT, prune=False)
        n = len(r["sent"]) + len(r["kept"])
        name = oid if n == 1 else f"{oid} ({n} arquivos)"
        note("Oba", name, "criado" if r["new"] else "atualizado" if r["sent"] else "mantido")


def ensure_table() -> str:
    name = f"{P}-sessions"
    try:
        arn = ddb.describe_table(TableName=name)["Table"]["TableArn"]
        status = "mantido"
    except ddb.exceptions.ResourceNotFoundException:
        arn = ddb.create_table(
            TableName=name, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in ("pk", "sk")],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        )["TableDescription"]["TableArn"]
        ddb.get_waiter("table_exists").wait(TableName=name)
        status = "criado"
    ttl = ddb.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
    if ttl["TimeToLiveStatus"] not in ("ENABLED", "ENABLING"):
        ddb.update_time_to_live(TableName=name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires"})
    note("tabela", name, status)
    return arn


# ---------- placa: Transcribe, policy, thing e certificado ----------

def ensure_transcribe() -> str:
    alias = f"{P}-transcribe"
    alias_arn = f"{IOT_ARN}:rolealias/{alias}"
    trust = doc({"Effect": "Allow", "Principal": {"Service": "credentials.iot.amazonaws.com"}, "Action": "sts:AssumeRole",
                 "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}, "ArnLike": {"aws:SourceArn": alias_arn}}})
    policy = doc(allow("StreamingOnly", "transcribe:StartStreamTranscriptionWebSocket", "*",
                       Condition={"StringEquals": {"aws:RequestedRegion": TR_REGION}}))
    role = ensure_role(alias, trust, policy, "Oba Pocket: a placa só abre streams do Transcribe")
    try:
        cur = iot.describe_role_alias(roleAlias=alias)["roleAliasDescription"]
        if cur["roleArn"] == role and cur["credentialDurationSeconds"] == CRED_SECONDS:
            note("role alias", alias, "mantido")
        else:
            iot.update_role_alias(roleAlias=alias, roleArn=role, credentialDurationSeconds=CRED_SECONDS)
            note("role alias", alias, "atualizado")
    except iot.exceptions.ResourceNotFoundException:
        iot.create_role_alias(roleAlias=alias, roleArn=role, credentialDurationSeconds=CRED_SECONDS)
        note("role alias", alias, "criado")
    return alias


def vocab_phrases(obas: dict) -> list:
    """Termos do config.json + palavras de ativação de todos os Obas."""
    words = list(CFG["transcribe"].get("vocabulary") or [])
    for _, spec in obas.values():
        words += spec.get("wake_words") or []
    out, seen = [], set()
    for w in words:
        w = "-".join(str(w).split())
        if not VOCAB_WORD.match(w):
            print(f"  vocabulário: {w!r} fora do formato (letras e hífen, sem dígitos), pulado")
        elif w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


def start_vocabulary(phrases: list) -> str:
    """Cria ou atualiza o vocabulário; ele fica pronto em alguns minutos (wait_vocabulary)."""
    if not phrases:
        return ""
    name = f"{P}-{LANG.replace('-', '').lower()}"
    arn = f"arn:aws:transcribe:{TR_REGION}:{ACCOUNT}:vocabulary/{name}"
    sha = hashlib.sha256(json.dumps([LANG, phrases]).encode()).hexdigest()[:32]
    tags = [{"Key": "oba-sha", "Value": sha}]
    existing = [v for v in tr.list_vocabularies(NameContains=name, MaxResults=100).get("Vocabularies", [])
                if v["VocabularyName"] == name]
    if not existing:
        tr.create_vocabulary(VocabularyName=name, LanguageCode=LANG, Phrases=phrases, Tags=tags)
        note("vocabulário", name, "criado")
        return name
    cur = {t["Key"]: t["Value"] for t in tr.list_tags_for_resource(ResourceArn=arn).get("Tags", [])}
    if cur.get("oba-sha") == sha and existing[0]["VocabularyState"] != "FAILED":
        note("vocabulário", name, "mantido")
        return name
    wait_vocabulary(name, quiet=True)  # não dá para atualizar enquanto está pendente
    tr.update_vocabulary(VocabularyName=name, LanguageCode=LANG, Phrases=phrases)
    tr.tag_resource(ResourceArn=arn, Tags=tags)
    note("vocabulário", name, "atualizado")
    return name


def wait_vocabulary(name: str, quiet=False) -> str:
    if not name:
        return ""
    for i in range(90):
        v = tr.get_vocabulary(VocabularyName=name)
        if v["VocabularyState"] == "READY":
            return name
        if v["VocabularyState"] == "FAILED":
            if quiet:
                return name
            print(f"  aviso: o vocabulário falhou ({v.get('FailureReason')}); a placa segue sem ele")
            return ""
        if i == 0 and not quiet:
            print("  aguardando o vocabulário do Transcribe (alguns minutos)...")
        time.sleep(10)
    fail(f"vocabulário {name} não ficou pronto em 15 minutos")


def device_policy(alias: str) -> str:
    name = f"{P}-device"
    thing = "${iot:Connection.Thing.ThingName}"  # o client ID do MQTT é o nome do thing
    topic = lambda ch: f"{IOT_ARN}:topic/{P}/{thing}/{ch}"
    ensure_iot_policy(name, doc(
        allow("Connect", "iot:Connect", f"{IOT_ARN}:client/{thing}"),
        allow("Publish", "iot:Publish", [topic(c) for c in ("state", "evt", "transcript", "reply")]),
        allow("RetainState", "iot:RetainPublish", topic("state")),
        allow("Commands", "iot:Subscribe", f"{IOT_ARN}:topicfilter/{P}/{thing}/cmd"),
        allow("CommandsReceive", "iot:Receive", topic("cmd")),
        allow("Transcribe", "iot:AssumeRoleWithCertificate", f"{IOT_ARN}:rolealias/{alias}"),
    ))
    return name


def read_secrets() -> dict:
    out = {"wifi": ['#define WIFI_SSID "minha-rede"', '#define WIFI_PASS "minha-senha"'], "cert": None, "key": None}
    if not SECRETS.is_file():
        return out
    text = SECRETS.read_text()
    wifi = [l for l in text.splitlines() if l.startswith(("#define WIFI_SSID", "#define WIFI_PASS"))]
    if len(wifi) == 2:
        out["wifi"] = wifi
    cert = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text, re.S)
    key = re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.+?-----END [A-Z ]*PRIVATE KEY-----", text, re.S)
    if cert and key:
        try:
            ssl.PEM_cert_to_DER_cert(cert.group(0))
            out["cert"], out["key"] = cert.group(0), key.group(0)
        except ValueError:
            pass  # o "..." do secrets.h.example
    return out


def write_secrets(wifi: list, cert: str, key: str):
    text = "\n".join([
        "// Gerado pelo setup.py. Fica fora do git: tem a senha do WiFi e a chave da placa.",
        "#pragma once", "", *wifi, "",
        f"// Certificado do thing {DEV}, emitido pelo AWS IoT a partir de um CSR",
        'static const char DEVICE_CERT[] = R"PEM(', cert.strip(), ')PEM";', "",
        "// Chave privada gerada nesta máquina pelo setup.py (não sai daqui)",
        'static const char DEVICE_KEY[] = R"PEM(', key.strip(), ')PEM";', ""])
    fd = os.open(SECRETS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(SECRETS, 0o600)


def new_key_and_csr() -> tuple:
    if not shutil.which("openssl"):
        fail("preciso do openssl para gerar a chave da placa")
    with tempfile.TemporaryDirectory() as d:
        key = Path(d) / "key.pem"
        subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)],
                       check=True, capture_output=True)
        csr = subprocess.run(["openssl", "req", "-new", "-key", str(key), "-subj", f"/CN={DEV}"],
                             check=True, capture_output=True, text=True).stdout
        return key.read_text(), csr


def ensure_thing(policy: str, new_cert: bool):
    try:
        iot.describe_thing(thingName=DEV)
        note("thing", DEV, "mantido")
    except iot.exceptions.ResourceNotFoundException:
        iot.create_thing(thingName=DEV)
        note("thing", DEV, "criado")
    attached = iot.list_thing_principals(thingName=DEV).get("principals", [])
    cur = read_secrets()
    old_arn = None
    if cur["cert"]:
        cid = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cur["cert"])).hexdigest()  # o ID do certificado no IoT
        try:
            d = iot.describe_certificate(certificateId=cid)["certificateDescription"]
            if d["certificateArn"] in attached:
                old_arn = d["certificateArn"]
                if d["status"] == "ACTIVE" and not new_cert:
                    iot.attach_policy(policyName=policy, target=old_arn)
                    return note("certificado", f"{cid[:10]}… (src/secrets.h)", "mantido")
        except iot.exceptions.ResourceNotFoundException:
            pass
    key, csr = new_key_and_csr()
    r = iot.create_certificate_from_csr(certificateSigningRequest=csr, setAsActive=True)
    iot.attach_policy(policyName=policy, target=r["certificateArn"])
    iot.attach_thing_principal(thingName=DEV, principal=r["certificateArn"])
    if cur["cert"]:
        backup = SECRETS.with_name("secrets.h.bak")
        shutil.copy2(SECRETS, backup)
        os.chmod(backup, 0o600)
        print(f"  o secrets.h anterior ficou em {backup.relative_to(ROOT)}")
    write_secrets(cur["wifi"], r["certificatePem"], key)
    note("certificado", f"{r['certificateId'][:10]}… (src/secrets.h)", "criado")
    if old_arn:  # rotação: o certificado anterior deste thing sai e fica inativo (dá para reativar)
        iot.detach_thing_principal(thingName=DEV, principal=old_arn)
        iot.update_certificate(certificateId=old_arn.split("/")[-1], newStatus="INACTIVE")
        note("certificado", f"{old_arn.split('/')[-1][:10]}… (anterior)", "desativado")


# ---------- agente (AgentCore) ----------

def model_arns() -> list:
    arns = []
    for m in CFG["models"]:
        geo, _, fm = m.partition(".")
        if geo in MODEL_GEOS:
            arns.append(f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{m}")
            arns += [f"arn:aws:bedrock:{r}::foundation-model/{fm}" for r in CFG.get("model_regions") or [REGION]]
            if geo == "global":
                arns.append(f"arn:aws:bedrock:::foundation-model/{fm}")
        else:
            arns.append(f"arn:aws:bedrock:{REGION}::foundation-model/{m}")
    return arns


def agent_role(name: str) -> str:
    trust = doc({"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
                 "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT},
                               "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{name}-*"}}})
    group = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/{name}-*"
    policy = doc(
        allow("OnlyTheListedModels", ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], model_arns()),
        allow("Logs", ["logs:CreateLogGroup", "logs:DescribeLogStreams"], [group]),
        allow("LogStreams", ["logs:CreateLogStream", "logs:PutLogEvents"], [f"{group}:log-stream:*"]),
        allow("DescribeLogGroups", "logs:DescribeLogGroups", [f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:*"]),
    )
    return ensure_role(f"{P}-agent", trust, policy, "Oba Pocket: agente no AgentCore (só os modelos da lista e logs)")


def agent_env() -> dict:
    env = {"MODELS": ",".join(CFG["models"])}
    mcp = CFG.get("mcp") or {}
    if mcp.get("aws-knowledge"):
        env["AWS_KNOWLEDGE_MCP_URL"] = mcp["aws-knowledge"]
    if mcp.get("demos"):
        env["DEMOS_MCP_URL"] = mcp["demos"]
        if os.environ.get("DEMOS_MCP_TOKEN"):
            env["DEMOS_MCP_TOKEN"] = os.environ["DEMOS_MCP_TOKEN"]
    return env


def find_runtime(name: str):
    kw = {}
    while True:
        r = ctl.list_agent_runtimes(**kw)
        for rt in r.get("agentRuntimes", []):
            if rt["agentRuntimeName"] == name:
                return rt
        if not r.get("nextToken"):
            return None
        kw["nextToken"] = r["nextToken"]


def ensure_agent() -> str:
    name, key = f"{P_}_agent", "agent/agent.zip"
    role = agent_role(name)
    files = [f for f in AGENT_SRC.rglob("*.py") if "__pycache__" not in f.parts] + [AGENT_SRC / "requirements.txt"]
    sha = tree_sha(files, AGENT_SRC)
    try:
        head = s3.head_object(Bucket=BUCKET, Key=key, ExpectedBucketOwner=ACCOUNT)
        version = head["VersionId"] if head.get("Metadata", {}).get("code-sha") == sha else None
    except ClientError:
        version = None
    if not version:
        print("  empacotando o agente...")
        pkg = staging("agent")
        pip_target(pkg, "-r", str(AGENT_SRC / "requirements.txt"))
        for f in files:
            (pkg / f.relative_to(AGENT_SRC)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(f, pkg / f.relative_to(AGENT_SRC))
        body = zip_tree(pkg)
        print(f"  pacote do agente: {len(body) // 1024} KB")
        version = s3.put_object(Bucket=BUCKET, Key=key, Body=body, Metadata={"code-sha": sha},
                                ExpectedBucketOwner=ACCOUNT)["VersionId"]
    args = dict(
        agentRuntimeArtifact={"codeConfiguration": {
            "code": {"s3": {"bucket": BUCKET, "prefix": key, "versionId": version}},
            "runtime": "PYTHON_3_13", "entryPoint": ["main.py"]}},
        roleArn=role,
        networkConfiguration={"networkMode": "PUBLIC"},
        environmentVariables=agent_env(),
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
    )
    rt = find_runtime(name)
    if rt:
        rid = rt["agentRuntimeId"]
        cur = ctl.get_agent_runtime(agentRuntimeId=rid)
        same = (cur.get("agentRuntimeArtifact") == args["agentRuntimeArtifact"] and cur.get("roleArn") == role
                and (cur.get("environmentVariables") or {}) == args["environmentVariables"])
        if same:
            note("agente", name, "mantido")
        else:
            ctl.update_agent_runtime(agentRuntimeId=rid, **args)
            note("agente", name, "atualizado")
    else:
        for _ in range(12):  # a role recém-criada demora a valer
            try:
                rid = ctl.create_agent_runtime(agentRuntimeName=name, **args)["agentRuntimeId"]
                break
            except ctl.exceptions.ValidationException as e:
                print("  aguardando a role...", str(e)[:120])
                time.sleep(10)
        else:
            fail("não consegui criar o runtime do agente")
        note("agente", name, "criado")
    while True:
        r = ctl.get_agent_runtime(agentRuntimeId=rid)
        if r["status"] == "READY":
            retain_logs(f"/aws/bedrock-agentcore/runtimes/{rid}-DEFAULT")
            return r["agentRuntimeArn"]
        if r["status"].endswith("FAILED"):
            fail(f"runtime do agente: {r['status']} {r.get('failureReason', '')}")
        time.sleep(5)


def resolve_agents(own_arn: str) -> dict:
    """Catálogo de endpoints do roteador: "default" é o agente deste setup."""
    out = {}
    for name, a in {"default": {"type": "agentcore"}, **(CFG.get("agents") or {})}.items():
        if a["type"] == "http":
            out[name] = {"type": "http", "url": a["url"]}
        else:
            out[name] = {"type": "agentcore", "arn": a.get("arn") or own_arn}
    return out


# ---------- roteador (Lambda + IoT Rule) ----------

def router_role(fn: str, table_arn: str, agents: dict) -> str:
    trust = doc({"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole",
                 "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}}})
    group = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{fn}"
    runtimes = sorted({a["arn"] for a in agents.values() if a["type"] == "agentcore"})
    statements = [
        allow("Logs", "logs:CreateLogGroup", [group]),
        allow("LogStreams", ["logs:CreateLogStream", "logs:PutLogEvents"], [f"{group}:log-stream:*"]),
        # DeleteItem: o on_reply tira da tabela o read que o agente pediu
        allow("Sessions", ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                           "dynamodb:Query"], [table_arn]),
        allow("PublishToDevices", "iot:Publish", [f"{IOT_ARN}:topic/{P}/*/cmd", f"{IOT_ARN}:topic/{P}/*/ui/*"]),
        allow("RetainUi", "iot:RetainPublish", [f"{IOT_ARN}:topic/{P}/*/ui/*"]),
        allow("ObaRegistry", "s3:GetObject", [f"arn:aws:s3:::{BUCKET}/obas/*"]),
    ]
    if runtimes:
        statements.append(allow("InvokeAgents", "bedrock-agentcore:InvokeAgentRuntime",
                                [x for a in runtimes for x in (a, f"{a}/runtime-endpoint/DEFAULT")]))
    return ensure_role(fn, trust, doc(*statements), "Oba Pocket: roteador entre o IoT Core e os agentes")


def router_zip(files: list) -> bytes:
    print("  empacotando o roteador...")
    pkg = staging("router")
    pip_target(pkg, "boto3")  # junto: garante o cliente bedrock-agentcore, seja qual for o runtime
    for f in files:
        (pkg / f.relative_to(ROUTER_SRC)).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(f, pkg / f.relative_to(ROUTER_SRC))
    body = zip_tree(pkg)
    print(f"  pacote do roteador: {len(body) // 1024} KB")
    return body


def ensure_router(table_arn: str, agents: dict, data_endpoint: str) -> str:
    fn = f"{P}-router"
    role = router_role(fn, table_arn, agents)
    icons = sorted((ROUTER_SRC / "icons").glob("*.png"))
    if not icons:
        import fetch_icons
        fetch_icons.fetch()
        icons = sorted((ROUTER_SRC / "icons").glob("*.png"))
    files = [ROUTER_SRC / "router.py", ROUTER_SRC / "icons.py", *icons]
    sha = tree_sha(files, ROUTER_SRC)
    env = {"PREFIX": P, "TABLE": table_arn.split("/")[-1], "BUCKET": BUCKET, "IOT_ENDPOINT": data_endpoint,
           "AGENTS": json.dumps(agents, separators=(",", ":")), "URL_HOSTS": ",".join(CFG.get("url_hosts") or [])}
    conf = dict(Role=role, Handler="router.handler", Runtime="python3.13", Timeout=ROUTER_TIMEOUT, MemorySize=256,
                Environment={"Variables": env}, Description="Oba Pocket: eventos da placa -> gatilhos -> agente -> comandos")
    retain_logs(f"/aws/lambda/{fn}")
    try:
        cur = lam.get_function(FunctionName=fn)
    except lam.exceptions.ResourceNotFoundException:
        cur = None
    if cur is None:
        code = router_zip(files)
        for _ in range(12):  # a role recém-criada demora a valer
            try:
                lam.create_function(FunctionName=fn, Code={"ZipFile": code}, Architectures=["arm64"],
                                    Tags={"oba-code": sha}, **conf)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                print("  aguardando a role...", str(e)[:120])
                time.sleep(10)
        else:
            fail("não consegui criar a Lambda do roteador")
        lam.get_waiter("function_active_v2").wait(FunctionName=fn)
        status = "criado"
    else:
        c, status = cur["Configuration"], "mantido"
        if cur.get("Tags", {}).get("oba-code") != sha:
            lam.update_function_code(FunctionName=fn, ZipFile=router_zip(files), Architectures=["arm64"])
            lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
            lam.tag_resource(Resource=c["FunctionArn"], Tags={"oba-code": sha})
            status = "atualizado"
        now = {"Role": c["Role"], "Handler": c["Handler"], "Runtime": c["Runtime"], "Timeout": c["Timeout"],
               "MemorySize": c["MemorySize"], "Environment": {"Variables": c.get("Environment", {}).get("Variables", {})},
               "Description": c.get("Description", "")}
        if now != conf:
            lam.update_function_configuration(FunctionName=fn, **conf)
            status = "atualizado"
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
    # Frases chegando juntas não precisam de muitas cópias; evento velho ou com erro não volta
    try:
        lam.put_function_concurrency(FunctionName=fn, ReservedConcurrentExecutions=ROUTER_CONCURRENCY)
    except lam.exceptions.InvalidParameterValueException:  # conta nova: a cota começa em 10
        print("  aviso: a cota de Lambda da conta é baixa, o roteador segue sem concorrência reservada")
    lam.put_function_event_invoke_config(FunctionName=fn, MaximumRetryAttempts=0, MaximumEventAgeInSeconds=900)
    note("Lambda", fn, status)
    return lam.get_function(FunctionName=fn)["Configuration"]["FunctionArn"]


def ensure_rule(function_arn: str):
    name = f"{P_}_router"
    rule = {
        "sql": f"SELECT *, topic(2) AS dev, topic(3) AS ch FROM '{P}/+/+' WHERE topic(3) = 'state' "
               "OR topic(3) = 'evt' OR (topic(3) = 'reply' AND (isUndefined(re) OR re <> 'oba.chunk')) "
               "OR (topic(3) = 'transcript' AND partial = false)",
        "awsIotSqlVersion": "2016-03-23",
        "ruleDisabled": False,
        "description": "Oba Pocket: state, evt, reply (menos acks de pedaço) e frases finais -> roteador",
        "actions": [{"lambda": {"functionArn": function_arn}}],
    }
    try:
        cur = iot.get_topic_rule(ruleName=name)["rule"]
        same = (cur["sql"] == rule["sql"] and cur["actions"] == rule["actions"] and not cur.get("ruleDisabled")
                and cur.get("description") == rule["description"])
        if not same:
            iot.replace_topic_rule(ruleName=name, topicRulePayload=rule)
        note("IoT Rule", name, "mantido" if same else "atualizado")
    except (iot.exceptions.UnauthorizedException, iot.exceptions.ResourceNotFoundException):
        iot.create_topic_rule(ruleName=name, topicRulePayload=rule)
        note("IoT Rule", name, "criado")
    try:
        lam.add_permission(FunctionName=function_arn, StatementId="iot-rule", Action="lambda:InvokeFunction",
                           Principal="iot.amazonaws.com", SourceArn=f"{IOT_ARN}:rule/{name}", SourceAccount=ACCOUNT)
    except lam.exceptions.ResourceConflictException:
        pass


# ---------- tela (Cognito sem login, só leitura) ----------

def ensure_display() -> str:
    name, kw, pid = f"{P_}_display", {"MaxResults": 60}, None
    while not pid:
        r = cog.list_identity_pools(**kw)
        pid = next((p["IdentityPoolId"] for p in r["IdentityPools"] if p["IdentityPoolName"] == name), None)
        if not r.get("NextToken"):
            break
        kw["NextToken"] = r["NextToken"]
    if pid:
        cur = cog.describe_identity_pool(IdentityPoolId=pid)
        if not cur["AllowUnauthenticatedIdentities"] or cur.get("AllowClassicFlow"):
            cog.update_identity_pool(IdentityPoolId=pid, IdentityPoolName=name,
                                     AllowUnauthenticatedIdentities=True, AllowClassicFlow=False)
        note("Cognito", name, "mantido")
    else:
        pid = cog.create_identity_pool(IdentityPoolName=name, AllowUnauthenticatedIdentities=True,
                                       AllowClassicFlow=False)["IdentityPoolId"]
        note("Cognito", name, "criado")
    trust = doc({"Effect": "Allow", "Principal": {"Federated": "cognito-identity.amazonaws.com"},
                 "Action": "sts:AssumeRoleWithWebIdentity",
                 "Condition": {"StringEquals": {"cognito-identity.amazonaws.com:aud": pid},
                               "ForAnyValue:StringLike": {"cognito-identity.amazonaws.com:amr": "unauthenticated"}}})
    policy = doc(
        allow("Connect", "iot:Connect", f"{IOT_ARN}:client/{P}-display-*"),
        allow("Subscribe", "iot:Subscribe", f"{IOT_ARN}:topicfilter/{P}/{DEV}/*"),
        allow("Receive", "iot:Receive", f"{IOT_ARN}:topic/{P}/{DEV}/*"),
    )
    role = ensure_role(f"{P}-display", trust, policy, "Oba Pocket: a tela só lê os tópicos da placa")
    cog.set_identity_pool_roles(IdentityPoolId=pid, Roles={"unauthenticated": role})
    return pid


# ---------- arquivos gerados ----------

def write_aws_config(data_ep: str, cred_ep: str, alias: str, vocab: str):
    header = "// Gerado pelo setup.py a partir do config.json. Não edite: rode o setup de novo."
    (ROOT / "src" / "aws_config.h").write_text(f"""{header}
// Nada aqui é segredo: o acesso é controlado pelo certificado em secrets.h.
#pragma once

// Tópicos <prefixo>/<placa>/... (docs/protocol.md)
#define OBA_PREFIX "{P}"

// Thing do IoT, que também é o client ID do MQTT
#define IOT_THING_NAME "{DEV}"
#define IOT_DATA_ENDPOINT "{data_ep}"

// Credentials provider: troca o certificado por credenciais temporárias que só
// abrem streams do Transcribe
#define IOT_CRED_ENDPOINT "{cred_ep}"
#define IOT_ROLE_ALIAS "{alias}"

// Transcribe streaming. Uma região perto da placa: com o buffer TCP pequeno do
// ESP32, a latência limita a vazão do áudio.
#define TRANSCRIBE_REGION "{TR_REGION}"
#define TRANSCRIBE_HOST "transcribestreaming.{TR_REGION}.amazonaws.com"
#define TRANSCRIBE_PORT 8443
#define TRANSCRIBE_LANG "{LANG}"
#define TRANSCRIBE_VOCAB "{vocab}"  // vazio: sem vocabulário
""")
    js = {"region": REGION, "identityPool": POOL, "iotEndpoint": data_ep, "prefix": P, "device": DEV}
    (ROOT / "display" / "config.js").write_text(
        f"{header}\nwindow.OBA_CONFIG = {json.dumps(js, indent=2)};\n")


def main():
    global POOL
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--new-cert", action="store_true", help="emite um certificado novo e desativa o anterior")
    args = ap.parse_args()
    init(load_config(Path(args.config)))
    print(f"Oba Pocket: prefixo {P}, placa {DEV}, região {REGION} (Transcribe em {TR_REGION})\n")

    obas = collect_obas()
    ensure_bucket()
    upload_obas(obas)
    table_arn = ensure_table()
    data_ep = iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
    cred_ep = iot.describe_endpoint(endpointType="iot:CredentialProvider")["endpointAddress"]

    alias = ensure_transcribe()
    vocab = start_vocabulary(vocab_phrases(obas))
    ensure_thing(device_policy(alias), args.new_cert)

    agents = resolve_agents(ensure_agent())
    ensure_rule(ensure_router(table_arn, agents, data_ep))
    POOL = ensure_display()

    vocab = wait_vocabulary(vocab)
    write_aws_config(data_ep, cred_ep, alias, vocab)
    print("  gerados    src/aws_config.h, display/config.js")

    changed = [r for r in REPORT if r[2] != "mantido"]
    print(f"\nPronto: {len(REPORT)} recursos, {len(changed)} criados ou alterados.")
    wifi = read_secrets()["wifi"]
    if any("minha-rede" in l or "minha-senha" in l for l in wifi):
        print("Falta o WiFi: preencha WIFI_SSID e WIFI_PASS em src/secrets.h.")
    print("Próximos passos: pio run -e core2foraws -t upload, e abra display/index.html no navegador.")


if __name__ == "__main__":
    main()
