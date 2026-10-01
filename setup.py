#!/usr/bin/env python3
"""Sobe (ou atualiza) o Oba Pocket numa conta da AWS a partir do config.json.

  cp config.example.json config.json        # e ajuste
  python3 -m venv .venv && .venv/bin/pip install boto3 jsonschema
  .venv/bin/python setup.py [--config config.json] [--new-cert]

Usa o perfil padrão da AWS CLI (ou AWS_PROFILE). Pode rodar de novo quantas
vezes quiser: só muda o que mudou. Cada peça recebe o mínimo de permissão:

  placa    certificado do thing -> policy só em <p>/<thing>/..., e um role alias
           que troca o certificado por credenciais do Transcribe (só streaming)
  portal   CloudFront -> S3 privado (o site) e Function URL da API (só pelo OAC);
           login no Cognito (usuários do config.json), e o navegador só lê
           <p>/<placa>/* pelo identity pool; comandos da placa e os Obas do
           registro passam pela API, e um instalador manda o Oba para a placa
  harness  IoT Rule -> Lambda do roteador (sessões no DynamoDB, Obas no S3,
           publica só em cmd e ui/*) -> agente no AgentCore (só os modelos da lista)

  ponte    thing <p>-ponte-<nome> por máquina com Claude Code (docs/ponte.md) ->
           policy só em ext/<thing> da placa, na resposta dele e no state

  python3 setup.py --portal             # só o portal
  python3 setup.py --resend USUÁRIO     # convite de novo (senha temporária vencida)
  python3 setup.py --ponte NOME         # credenciais de uma ponte em build/ponte-NOME/

Gera src/aws_config.h, src/secrets.h e build/portal/config.js (vai direto para o
bucket do portal). O WiFi de um secrets.h que já existe é mantido; a chave privada
nasce nesta máquina e nunca vai para a AWS (ela só assina um CSR). Para empacotar
as Lambdas e o agente precisa do uv (ou do pip); para o certificado, do openssl.
"""

import argparse
import functools
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
    global iam, iot, s3, ddb, lam, ctl, cog, tr, logs, idp, cf
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
    idp = boto3.client("cognito-idp", region_name=REGION)
    cf = boto3.client("cloudfront", region_name="us-east-1")


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
        # Fontes externas (docs/protocol.md): ouve ext/<fonte> e responde em ext/<fonte>/re
        allow("Ext", "iot:Subscribe", f"{IOT_ARN}:topicfilter/{P}/{thing}/ext/+"),
        allow("ExtReceive", "iot:Receive", topic("ext/*")),
        allow("ExtReply", "iot:Publish", topic("ext/*/re")),
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


def new_key_and_csr(cn: str | None = None) -> tuple:
    if not shutil.which("openssl"):
        fail("preciso do openssl para gerar a chave")
    with tempfile.TemporaryDirectory() as d:
        key = Path(d) / "key.pem"
        subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)],
                       check=True, capture_output=True)
        csr = subprocess.run(["openssl", "req", "-new", "-key", str(key), "-subj", f"/CN={cn or DEV}"],
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


# ---------- ponte (fontes externas) ----------

PONTE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,19}")


def ponte_policy() -> str:
    """Uma policy para todas as pontes: cada uma só fala no próprio ext/<thing> desta placa."""
    name = f"{P}-ponte"
    thing = "${iot:Connection.Thing.ThingName}"
    ext = f"{P}/{DEV}/ext/{thing}"
    ensure_iot_policy(name, doc(
        allow("Connect", "iot:Connect", f"{IOT_ARN}:client/{thing}",
              Condition={"Bool": {"iot:Connection.Thing.IsAttached": "true"}}),
        allow("Publish", "iot:Publish", f"{IOT_ARN}:topic/{ext}"),  # também a LWT
        allow("Subscribe", "iot:Subscribe", [f"{IOT_ARN}:topicfilter/{ext}/re", f"{IOT_ARN}:topicfilter/{P}/{DEV}/state"]),
        allow("Receive", "iot:Receive", [f"{IOT_ARN}:topic/{ext}/re", f"{IOT_ARN}:topic/{P}/{DEV}/state"]),
    ))
    return name


def root_ca() -> str:
    text = (ROOT / "src" / "amazon_root_ca.h").read_text()
    return re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text, re.S).group(0) + "\n"


def write_private(path: Path, text: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def ensure_ponte(name: str, new_cert: bool) -> Path:
    """Thing, certificado e a pasta build/ponte-<nome>/ que vai para ~/.oba-ponte na máquina."""
    if not PONTE_NAME.fullmatch(name):
        fail("--ponte: letras minúsculas, números e hífen (até 20), como mac ou notebook-casa")
    thing = f"{P}-ponte-{name}"
    policy = ponte_policy()
    out = BUILD / f"ponte-{name}"
    try:
        iot.describe_thing(thingName=thing)
        note("thing", thing, "mantido")
    except iot.exceptions.ResourceNotFoundException:
        iot.create_thing(thingName=thing, attributePayload={"attributes": {"oba": "ponte", "device": DEV}})
        note("thing", thing, "criado")
    attached = iot.list_thing_principals(thingName=thing).get("principals", [])
    cert_file = out / "cert.pem"
    keep = None
    if cert_file.is_file() and (out / "key.pem").is_file() and not new_cert:
        cid = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert_file.read_text())).hexdigest()
        try:
            d = iot.describe_certificate(certificateId=cid)["certificateDescription"]
            if d["certificateArn"] in attached and d["status"] == "ACTIVE":
                keep = d["certificateArn"]
        except iot.exceptions.ResourceNotFoundException:
            pass
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    if keep:
        iot.attach_policy(policyName=policy, target=keep)
        note("certificado", f"{keep.split('/')[-1][:10]}… ({out.relative_to(ROOT)})", "mantido")
    else:
        key, csr = new_key_and_csr(thing)
        r = iot.create_certificate_from_csr(certificateSigningRequest=csr, setAsActive=True)
        iot.attach_policy(policyName=policy, target=r["certificateArn"])
        iot.attach_thing_principal(thingName=thing, principal=r["certificateArn"])
        write_private(out / "key.pem", key)
        write_private(cert_file, r["certificatePem"])
        note("certificado", f"{r['certificateId'][:10]}… ({out.relative_to(ROOT)})", "criado")
        for old in attached:  # rotação: os anteriores desta ponte saem e ficam inativos
            iot.detach_thing_principal(thingName=thing, principal=old)
            iot.update_certificate(certificateId=old.split("/")[-1], newStatus="INACTIVE")
            note("certificado", f"{old.split('/')[-1][:10]}… (anterior)", "desativado")
    write_private(out / "AmazonRootCA1.pem", root_ca())
    cfg = {"endpoint": iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"],
           "prefix": P, "device": DEV, "thing": thing, "name": name,
           "cert": "cert.pem", "key": "key.pem", "ca": "AmazonRootCA1.pem"}
    write_private(out / "config.json", json.dumps(cfg, indent=2) + "\n")
    return out


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


def ensure_function(fn: str, sha: str, build, conf: dict, concurrency: int | None = None,
                    max_age: int = 900) -> str:
    """Cria ou atualiza uma Lambda arm64. Só empacota (build()) quando o tag oba-code não bate
    com o sha. Evento assíncrono com erro não volta (0 retries); mais velho que max_age, some."""
    retain_logs(f"/aws/lambda/{fn}")
    try:
        cur = lam.get_function(FunctionName=fn)
    except lam.exceptions.ResourceNotFoundException:
        cur = None
    if cur is None:
        code = build()
        for _ in range(12):  # a role recém-criada demora a valer
            try:
                lam.create_function(FunctionName=fn, Code={"ZipFile": code}, Architectures=["arm64"],
                                    Tags={"oba-code": sha}, **conf)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                print("  aguardando a role...", str(e)[:120])
                time.sleep(10)
        else:
            fail(f"não consegui criar a Lambda {fn}")
        lam.get_waiter("function_active_v2").wait(FunctionName=fn)
        status = "criado"
    else:
        c, status = cur["Configuration"], "mantido"
        if cur.get("Tags", {}).get("oba-code") != sha:
            lam.update_function_code(FunctionName=fn, ZipFile=build(), Architectures=["arm64"])
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
    if concurrency:
        try:
            lam.put_function_concurrency(FunctionName=fn, ReservedConcurrentExecutions=concurrency)
        except lam.exceptions.InvalidParameterValueException:  # conta nova: a cota começa em 10
            print(f"  aviso: a cota de Lambda da conta é baixa, {fn} segue sem concorrência reservada")
    lam.put_function_event_invoke_config(FunctionName=fn, MaximumRetryAttempts=0, MaximumEventAgeInSeconds=max_age)
    note("Lambda", fn, status)
    return lam.get_function(FunctionName=fn)["Configuration"]["FunctionArn"]


def ensure_router(table_arn: str, agents: dict, data_endpoint: str) -> str:
    fn = f"{P}-router"
    role = router_role(fn, table_arn, agents)
    icons = sorted((ROUTER_SRC / "icons").glob("*.png"))
    if not icons:
        import fetch_icons
        fetch_icons.fetch()
        icons = sorted((ROUTER_SRC / "icons").glob("*.png"))
    files = [ROUTER_SRC / "router.py", ROUTER_SRC / "icons.py", *icons]
    env = {"PREFIX": P, "TABLE": table_arn.split("/")[-1], "BUCKET": BUCKET, "IOT_ENDPOINT": data_endpoint,
           "AGENTS": json.dumps(agents, separators=(",", ":")), "URL_HOSTS": ",".join(CFG.get("url_hosts") or [])}
    conf = dict(Role=role, Handler="router.handler", Runtime="python3.13", Timeout=ROUTER_TIMEOUT, MemorySize=256,
                Environment={"Variables": env}, Description="Oba Pocket: eventos da placa -> gatilhos -> agente -> comandos")
    # Frases chegando juntas não precisam de muitas cópias
    return ensure_function(fn, tree_sha(files, ROUTER_SRC), lambda: router_zip(files), conf, ROUTER_CONCURRENCY)


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


# ---------- portal (CloudFront -> S3 privado e Lambda URL; login no Cognito) ----------

PORTAL_API = ROOT / "portal" / "api"
PORTAL_WEB = ROOT / "portal" / "web"
PORTAL_REQS = ("PyJWT[crypto]", "jsonschema", "paho-mqtt")
PORTAL_TIMEOUT = 30             # o mesmo do CloudFront para a origem
PORTAL_CONCURRENCY = 10
INSTALLER_TIMEOUT = 300         # o envio, a conferência e o toque em "Instalar" (até 60 s)
TOKEN_MINUTES = 60
REFRESH_DAYS = 30
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"   # políticas gerenciadas do CloudFront
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".webmanifest": "application/manifest+json", ".json": "application/json", ".txt": "text/plain; charset=utf-8"}


def conf_sha(conf) -> str:
    return hashlib.sha256(json.dumps(conf, sort_keys=True, default=str).encode()).hexdigest()[:32]


def portal_cfg() -> dict:
    pc = CFG.get("portal") or {}
    for u in pc.get("users") or []:
        if not re.fullmatch(r"[a-z0-9._-]{2,64}", str(u.get("username", ""))) or "@" not in str(u.get("email", "")):
            fail("portal.users: cada um com \"username\" (minúsculas, números, . _ -) e \"email\"")
        if str(u["email"]).endswith("@exemplo.com"):
            fail("portal.users: troque o usuário de exemplo pelo seu (o convite vai para o email)")
    if bool(pc.get("domain")) != bool(pc.get("cert_arn")):
        fail("portal: \"domain\" e \"cert_arn\" (ACM em us-east-1) vão juntos")
    return pc


def ensure_portal_bucket() -> str:
    name = f"{P}-portal-{ACCOUNT}"
    try:
        s3.head_bucket(Bucket=name, ExpectedBucketOwner=ACCOUNT)
        status = "mantido"
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            fail(f"bucket {hide(name)}: {e}")
        extra = {} if REGION == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": REGION}}
        s3.create_bucket(Bucket=name, ObjectOwnership="BucketOwnerEnforced", **extra)
        status = "criado"
    s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={k: True for k in (
        "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")})
    s3.put_bucket_ownership_controls(Bucket=name, OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]})
    s3.put_bucket_encryption(Bucket=name, ServerSideEncryptionConfiguration={"Rules": [
        {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    note("bucket", name, status)
    return name


def find_distribution() -> dict | None:
    for page in cf.get_paginator("list_distributions").paginate():
        for d in page["DistributionList"].get("Items", []):
            if d["Comment"] == f"{P}-portal":
                return d
    return None


def portal_url(dist: dict | None) -> str | None:
    pc = CFG.get("portal") or {}
    if pc.get("domain"):
        return f"https://{pc['domain']}/"
    return f"https://{dist['DomainName']}/" if dist else None


def pool_conf(url: str | None) -> dict:
    """Configuração inteira: o UpdateUserPool volta ao padrão o que não vier."""
    where = f"<br><br>Entre em {url}" if url else ""
    return dict(
        Policies={"PasswordPolicy": {"MinimumLength": 12, "RequireUppercase": True, "RequireLowercase": True,
                                     "RequireNumbers": True, "RequireSymbols": True,
                                     "PasswordHistorySize": 5, "TemporaryPasswordValidityDays": 3}},
        DeletionProtection="ACTIVE",
        MfaConfiguration="OFF",
        AutoVerifiedAttributes=["email"],
        UserAttributeUpdateSettings={"AttributesRequireVerificationBeforeUpdate": ["email"]},
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True, "InviteMessageTemplate": {
            "EmailSubject": "Seu acesso ao Oba Pocket",
            "EmailMessage": "Você ganhou acesso ao portal do Oba Pocket.<br><br>Usuário: {username}<br>"
                            "Senha temporária: {####}<br><br>Ela vale 3 dias; no primeiro login o portal pede "
                            f"uma senha nova.{where}"}},
        AccountRecoverySetting={"RecoveryMechanisms": [{"Priority": 1, "Name": "verified_email"}]},
        EmailConfiguration={"EmailSendingAccount": "COGNITO_DEFAULT"},
        UserPoolTier="ESSENTIALS",
    )


def find_user_pool() -> str | None:
    for page in idp.get_paginator("list_user_pools").paginate(MaxResults=60):
        for p in page["UserPools"]:
            if p["Name"] == f"{P}-portal":
                return p["Id"]
    return None


def ensure_user_pool(url: str | None) -> str:
    name, conf = f"{P}-portal", pool_conf(url)
    sha, pool = conf_sha(conf), find_user_pool()
    if not pool:
        # Usuário simples, sem alias de email: com alias, o login revela quem existe
        pool = idp.create_user_pool(PoolName=name, UsernameConfiguration={"CaseSensitive": False},
                                    UserPoolTags={"oba-conf": sha}, **conf)["UserPool"]["Id"]
        return note("user pool", name, "criado") or pool
    tags = idp.describe_user_pool(UserPoolId=pool)["UserPool"].get("UserPoolTags", {})
    if tags.get("oba-conf") == sha:
        return note("user pool", name, "mantido") or pool
    idp.update_user_pool(UserPoolId=pool, PoolName=name, UserPoolTags={**tags, "oba-conf": sha}, **conf)
    return note("user pool", name, "atualizado") or pool


def ensure_login_domain(pool: str) -> str:
    """Prefixo com um hash da conta, que não aparece no endereço."""
    prefix = f"{P}-{hashlib.sha256(ACCOUNT.encode()).hexdigest()[:8]}"
    cur = idp.describe_user_pool_domain(Domain=prefix)["DomainDescription"]
    if not cur:
        idp.create_user_pool_domain(Domain=prefix, UserPoolId=pool, ManagedLoginVersion=2)
        note("login", prefix, "criado")
    elif cur["UserPoolId"] != pool:
        fail(f"o domínio de login {prefix} é de outro user pool")
    elif cur.get("ManagedLoginVersion") != 2:
        idp.update_user_pool_domain(Domain=prefix, UserPoolId=pool, ManagedLoginVersion=2)
        note("login", prefix, "atualizado")
    else:
        note("login", prefix, "mantido")
    return f"{prefix}.auth.{REGION}.amazoncognito.com"


def client_conf(url: str | None) -> dict:
    conf = dict(
        ClientName=f"{P}-portal",
        AccessTokenValidity=TOKEN_MINUTES, IdTokenValidity=TOKEN_MINUTES, RefreshTokenValidity=REFRESH_DAYS,
        TokenValidityUnits={"AccessToken": "minutes", "IdToken": "minutes", "RefreshToken": "days"},
        ReadAttributes=["email", "email_verified"], WriteAttributes=[],
        # Refresh só pelo /oauth2/token, com rotação: cada uso devolve um refresh novo
        ExplicitAuthFlows=["ALLOW_USER_SRP_AUTH"],
        RefreshTokenRotation={"Feature": "ENABLED", "RetryGracePeriodSeconds": 10},
        SupportedIdentityProviders=["COGNITO"],
        PreventUserExistenceErrors="ENABLED", EnableTokenRevocation=True, AuthSessionValidity=3,
    )
    if url:  # sem a URL do portal ainda (1ª rodada), o client fica sem o login hospedado
        conf.update(CallbackURLs=[url], LogoutURLs=[url], AllowedOAuthFlows=["code"],
                    AllowedOAuthScopes=["openid", "email"], AllowedOAuthFlowsUserPoolClient=True)
    return conf


def ensure_client(pool: str, url: str | None) -> str:
    conf = client_conf(url)
    cid = None
    for page in idp.get_paginator("list_user_pool_clients").paginate(UserPoolId=pool, MaxResults=60):
        cid = next((c["ClientId"] for c in page["UserPoolClients"] if c["ClientName"] == conf["ClientName"]), cid)
    if not cid:
        cid = idp.create_user_pool_client(UserPoolId=pool, GenerateSecret=False, **conf)["UserPoolClient"]["ClientId"]
        note("app client", conf["ClientName"], "criado")
    else:
        cur = idp.describe_user_pool_client(UserPoolId=pool, ClientId=cid)["UserPoolClient"]
        norm = lambda v: (sorted(v) if isinstance(v, list) else v) or None  # lista vazia volta como ausente
        if any(norm(cur.get(k)) != norm(v) for k, v in conf.items()):
            idp.update_user_pool_client(UserPoolId=pool, ClientId=cid, **conf)
            note("app client", conf["ClientName"], "atualizado")
        else:
            note("app client", conf["ClientName"], "mantido")
    try:
        idp.describe_managed_login_branding_by_client(UserPoolId=pool, ClientId=cid)
    except idp.exceptions.ResourceNotFoundException:  # sem branding, o login v2 não abre
        idp.create_managed_login_branding(UserPoolId=pool, ClientId=cid, UseCognitoProvidedValues=True)
    return cid


def ensure_users(pool: str):
    for u in (CFG.get("portal") or {}).get("users") or []:
        try:
            idp.admin_get_user(UserPoolId=pool, Username=u["username"])
            note("usuário", u["username"], "mantido")
        except idp.exceptions.UserNotFoundException:
            idp.admin_create_user(UserPoolId=pool, Username=u["username"], DesiredDeliveryMediums=["EMAIL"],
                                  UserAttributes=[{"Name": "email", "Value": u["email"]},
                                                  {"Name": "email_verified", "Value": "true"}])
            note("usuário", u["username"], "criado")


def resend_invite(username: str):
    pool = find_user_pool() or fail("o portal ainda não existe: rode o setup.py")
    try:
        idp.admin_create_user(UserPoolId=pool, Username=username, MessageAction="RESEND",
                              DesiredDeliveryMediums=["EMAIL"])
    except idp.exceptions.UserNotFoundException:
        fail(f"usuário {username} não existe no portal")
    except idp.exceptions.UnsupportedUserStateException:
        fail(f"{username} já trocou a senha temporária; para outra senha, use \"Esqueci a senha\" no login")
    print(f"Convite reenviado para {username}.")


def viewer_doc() -> dict:
    """Mesma policy no IAM (role autenticada) e no IoT (anexada ao identity ID): vale o que as duas liberam."""
    return doc(
        # O client ID começa pelo identity ID; o sufixo deixa abrir no celular e no notebook juntos
        allow("Connect", "iot:Connect", f"{IOT_ARN}:client/${{cognito-identity.amazonaws.com:sub}}-*"),
        allow("Subscribe", "iot:Subscribe", f"{IOT_ARN}:topicfilter/{P}/{DEV}/*"),
        allow("Receive", "iot:Receive", f"{IOT_ARN}:topic/{P}/{DEV}/*"),
        # Os pedidos das fontes externas (comandos, caminhos, trechos de código) ficam só na placa
        {"Sid": "NotExt", "Effect": "Deny", "Action": "iot:Receive", "Resource": f"{IOT_ARN}:topic/{P}/{DEV}/ext/*"},
    )


def find_identity_pool() -> str | None:
    names, kw = {f"{P_}_portal", f"{P_}_display"}, {"MaxResults": 60}
    while True:
        r = cog.list_identity_pools(**kw)
        pid = next((p["IdentityPoolId"] for p in r["IdentityPools"] if p["IdentityPoolName"] in names), None)
        if pid or not r.get("NextToken"):
            return pid
        kw["NextToken"] = r["NextToken"]


def ensure_identity_pool(pool: str, client: str) -> str:
    """Só usuários do portal ganham credenciais; o pool antigo da tela (com guest) vira este."""
    name = f"{P_}_portal"
    conf = dict(IdentityPoolName=name, AllowClassicFlow=False, AllowUnauthenticatedIdentities=False,
                CognitoIdentityProviders=[{"ProviderName": f"cognito-idp.{REGION}.amazonaws.com/{pool}",
                                           "ClientId": client, "ServerSideTokenCheck": True}])
    pid = find_identity_pool()
    if not pid:
        pid = cog.create_identity_pool(**conf)["IdentityPoolId"]
        note("identity pool", name, "criado")
    else:
        cur = cog.describe_identity_pool(IdentityPoolId=pid)
        if {k: cur.get(k) for k in conf} != conf:
            cog.update_identity_pool(IdentityPoolId=pid, **conf)
            note("identity pool", name, "atualizado")
        else:
            note("identity pool", name, "mantido")
    web = lambda amr: doc({"Effect": "Allow", "Principal": {"Federated": "cognito-identity.amazonaws.com"},
                           "Action": "sts:AssumeRoleWithWebIdentity",
                           "Condition": {"StringEquals": {"cognito-identity.amazonaws.com:aud": pid},
                                         "ForAnyValue:StringLike": {"cognito-identity.amazonaws.com:amr": amr}}})
    viewer = ensure_role(f"{P}-portal-viewer", web("authenticated"), viewer_doc(),
                         "Oba Pocket: o portal só lê os tópicos da placa")
    roles = {"authenticated": viewer}  # sem unauthenticated: ninguém sem login recebe credenciais
    if cog.get_identity_pool_roles(IdentityPoolId=pid).get("Roles") != roles:
        cog.set_identity_pool_roles(IdentityPoolId=pid, Roles=roles)
    return pid


def lambda_trust() -> dict:
    return doc({"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}}})


def lambda_logs(fn: str) -> list:
    group = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{fn}"
    return [allow("Logs", "logs:CreateLogGroup", [group]),
            allow("LogStreams", ["logs:CreateLogStream", "logs:PutLogEvents"], [f"{group}:log-stream:*"])]


def portal_api_role(fn: str, installer: str) -> str:
    return ensure_role(fn, lambda_trust(), doc(
        *lambda_logs(fn),
        # O IAM não restringe o AttachPolicy pelo alvo Cognito: a policy anexada é fixa no código
        # (VIEWER_POLICY), e o Deny garante que a API nunca mexe em certificados, em região nenhuma
        allow("ViewerPolicy", "iot:AttachPolicy", "*"),
        {"Sid": "NotDevices", "Effect": "Deny", "Action": "iot:AttachPolicy",
         "Resource": [f"arn:aws:iot:*:{ACCOUNT}:cert/*", f"arn:aws:iot:*:{ACCOUNT}:thinggroup/*"]},
        # Os comandos que a API aceita estão na lista dela (portal/api/api.py)
        allow("Commands", "iot:Publish", f"{IOT_ARN}:topic/{P}/{DEV}/cmd"),
        # Registro de Obas: listar, subir (depois da validação) e tirar. Com versionamento
        allow("ListRegistry", "s3:ListBucket", f"arn:aws:s3:::{BUCKET}",
              Condition={"StringLike": {"s3:prefix": ["obas/*"]}}),
        allow("Registry", ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], f"arn:aws:s3:::{BUCKET}/obas/*"),
        allow("Installer", "lambda:InvokeFunction", installer),
    ), "Oba Pocket: API do portal")


def portal_installer_role(fn: str) -> str:
    base = f"{IOT_ARN}:topic/{P}/{DEV}"
    return ensure_role(fn, lambda_trust(), doc(
        *lambda_logs(fn),
        allow("Registry", "s3:GetObject", f"arn:aws:s3:::{BUCKET}/obas/*"),
        allow("Connect", "iot:Connect", f"{IOT_ARN}:client/{P}-portal-*"),
        allow("Send", "iot:Publish", [f"{base}/cmd", f"{base}/ui/install"]),
        allow("Replies", "iot:Subscribe", f"{IOT_ARN}:topicfilter/{P}/{DEV}/reply"),
        allow("ReceiveReplies", "iot:Receive", f"{base}/reply"),
    ), "Oba Pocket: instalador do portal (Obas do registro -> placa)")


def portal_files() -> list:
    return [*sorted(PORTAL_API.glob("*.py")), ROOT / "tools" / "oba.py", ROOT / "tools" / "oba_remote.py",
            ROOT / "schema" / "oba.schema.json"]


@functools.cache
def portal_zip() -> bytes:
    """Um pacote para a API e o instalador (só monta uma vez por rodada)."""
    files = portal_files()
    print("  empacotando a API do portal...")
    pkg = staging("portal-api")
    pip_target(pkg, *PORTAL_REQS)  # o boto3 é o do runtime
    for f in files:
        rel = f.relative_to(PORTAL_API) if f.is_relative_to(PORTAL_API) else f.relative_to(ROOT)
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(f, pkg / rel)
    body = zip_tree(pkg)
    print(f"  pacote do portal: {len(body) // 1024} KB")
    return body


def jwks(pool: str) -> str:
    import urllib.request
    url = f"https://cognito-idp.{REGION}.amazonaws.com/{pool}/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.dumps(json.load(r), separators=(",", ":"))


def portal_sha() -> str:
    return conf_sha([tree_sha(portal_files(), ROOT), PORTAL_REQS])


def ensure_portal_installer(data_ep: str) -> str:
    """Manda um Oba do registro para a placa (a API invoca, assíncrono). Um por vez; pedido
    que ficou na fila passa do QUEUE_S do installer.py e não começa."""
    fn = f"{P}-portal-installer"
    role = portal_installer_role(fn)
    env = {"PREFIX": P, "DEVICE": DEV, "BUCKET": BUCKET, "IOT_ENDPOINT": data_ep}
    conf = dict(Role=role, Handler="installer.handler", Runtime="python3.13", Timeout=INSTALLER_TIMEOUT,
                MemorySize=256, Environment={"Variables": env},
                Description="Oba Pocket: instala um Oba do registro na placa (pedido do portal)")
    return ensure_function(fn, portal_sha(), portal_zip, conf, 1, max_age=120)


def ensure_portal_api(pool: str, client: str, pid: str, data_ep: str, installer: str) -> tuple[str, str]:
    fn = f"{P}-portal-api"
    role = portal_api_role(fn, installer)
    env = {"USER_POOL": pool, "CLIENT_ID": client, "IDENTITY_POOL": pid, "VIEWER_POLICY": f"{P}-portal-viewer",
           "JWKS": jwks(pool), "IOT_ENDPOINT": data_ep, "PREFIX": P, "DEVICE": DEV, "BUCKET": BUCKET,
           "INSTALLER": installer}
    conf = dict(Role=role, Handler="api.handler", Runtime="python3.13", Timeout=PORTAL_TIMEOUT, MemorySize=512,
                Environment={"Variables": env}, Description="Oba Pocket: API do portal (atrás do CloudFront)")
    arn = ensure_function(fn, portal_sha(), portal_zip, conf, PORTAL_CONCURRENCY)
    try:
        url = lam.get_function_url_config(FunctionName=fn)
        if url["AuthType"] != "AWS_IAM" or url.get("Cors"):
            lam.update_function_url_config(FunctionName=fn, AuthType="AWS_IAM", Cors={})
        url = url["FunctionUrl"]
    except lam.exceptions.ResourceNotFoundException:
        url = lam.create_function_url_config(FunctionName=fn, AuthType="AWS_IAM", InvokeMode="BUFFERED")["FunctionUrl"]
    return arn, url.split("/")[2]


def ensure_oac(name: str, kind: str) -> str:
    conf = {"Name": name, "Description": "Oba Pocket: só o CloudFront do portal", "SigningProtocol": "sigv4",
            "SigningBehavior": "always", "OriginAccessControlOriginType": kind}
    kw = {}
    while True:
        r = cf.list_origin_access_controls(**kw)["OriginAccessControlList"]
        oac = next((o for o in r.get("Items", []) if o["Name"] == name), None)
        if oac or not r.get("IsTruncated"):
            break
        kw["Marker"] = r["NextMarker"]
    if not oac:
        oid = cf.create_origin_access_control(OriginAccessControlConfig=conf)["OriginAccessControl"]["Id"]
        return note("OAC", name, "criado") or oid
    cur = cf.get_origin_access_control(Id=oac["Id"])
    if cur["OriginAccessControl"]["OriginAccessControlConfig"] != conf:
        cf.update_origin_access_control(Id=oac["Id"], IfMatch=cur["ETag"], OriginAccessControlConfig=conf)
        return note("OAC", name, "atualizado") or oac["Id"]
    return note("OAC", name, "mantido") or oac["Id"]


def portal_csp(login: str, data_ep: str) -> str:
    return "; ".join([
        "default-src 'self'", "script-src 'self'", "style-src 'self'", "img-src 'self' data: blob:",
        f"connect-src 'self' https://{login} https://cognito-identity.{REGION}.amazonaws.com wss://{data_ep}",
        "worker-src 'self' blob:",  # timers do mqtt.js num worker, para a aba em segundo plano
        "manifest-src 'self'", "frame-ancestors 'none'", "base-uri 'none'", "object-src 'none'", "form-action 'self'",
    ])


def ensure_headers_policy(csp: str) -> str:
    name = f"{P}-portal"
    conf = {"Name": name, "SecurityHeadersConfig": {
        "ContentSecurityPolicy": {"Override": True, "ContentSecurityPolicy": csp},
        "StrictTransportSecurity": {"Override": True, "AccessControlMaxAgeSec": 63072000, "IncludeSubdomains": True,
                                    "Preload": False},
        "ContentTypeOptions": {"Override": True},
        "FrameOptions": {"Override": True, "FrameOption": "DENY"},
        "ReferrerPolicy": {"Override": True, "ReferrerPolicy": "no-referrer"}},
        "CustomHeadersConfig": {"Quantity": 1, "Items": [{"Header": "Permissions-Policy", "Override": True,
            "Value": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"}]}}
    conf["Comment"] = f"Oba Pocket #{conf_sha(conf)}"  # o sha evita comparar o documento inteiro
    items = cf.list_response_headers_policies(Type="custom")["ResponseHeadersPolicyList"].get("Items", [])
    cur = next((i["ResponseHeadersPolicy"] for i in items
                if i["ResponseHeadersPolicy"]["ResponseHeadersPolicyConfig"]["Name"] == name), None)
    if not cur:
        pid = cf.create_response_headers_policy(ResponseHeadersPolicyConfig=conf)["ResponseHeadersPolicy"]["Id"]
        return note("headers", name, "criado") or pid
    if cur["ResponseHeadersPolicyConfig"].get("Comment") == conf["Comment"]:
        return note("headers", name, "mantido") or cur["Id"]
    etag = cf.get_response_headers_policy(Id=cur["Id"])["ETag"]
    cf.update_response_headers_policy(Id=cur["Id"], IfMatch=etag, ResponseHeadersPolicyConfig=conf)
    return note("headers", name, "atualizado") or cur["Id"]


def dist_conf(bucket: str, api_host: str, s3_oac: str, api_oac: str, headers: str) -> dict:
    pc = CFG.get("portal") or {}
    origin = lambda oid, host, oac, extra: {
        "Id": oid, "DomainName": host, "OriginPath": "", "CustomHeaders": {"Quantity": 0},
        "ConnectionAttempts": 3, "ConnectionTimeout": 10, "OriginShield": {"Enabled": False},
        "OriginAccessControlId": oac, **extra}

    def behavior(target, cache, methods, request=None):
        b = {"TargetOriginId": target, "ViewerProtocolPolicy": "redirect-to-https", "Compress": True,
             "AllowedMethods": {"Quantity": len(methods), "Items": methods,
                                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
             "CachePolicyId": cache, "ResponseHeadersPolicyId": headers,
             "TrustedSigners": {"Enabled": False, "Quantity": 0}, "TrustedKeyGroups": {"Enabled": False, "Quantity": 0},
             "SmoothStreaming": False, "FieldLevelEncryptionId": "",
             "LambdaFunctionAssociations": {"Quantity": 0}, "FunctionAssociations": {"Quantity": 0}}
        return {**b, "OriginRequestPolicyId": request} if request else b

    api = behavior("api", CACHING_DISABLED, ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                   ALL_VIEWER_EXCEPT_HOST)
    if pc.get("domain"):
        aliases = {"Quantity": 1, "Items": [pc["domain"]]}
        cert = {"ACMCertificateArn": pc["cert_arn"], "SSLSupportMethod": "sni-only",
                "MinimumProtocolVersion": "TLSv1.2_2021", "CloudFrontDefaultCertificate": False}
    else:
        aliases, cert = {"Quantity": 0}, {"CloudFrontDefaultCertificate": True, "MinimumProtocolVersion": "TLSv1"}
    return {
        "CallerReference": f"{P}-portal", "Comment": f"{P}-portal", "Enabled": True,
        "DefaultRootObject": "index.html", "PriceClass": "PriceClass_All", "HttpVersion": "http2and3",
        "IsIPV6Enabled": True, "Aliases": aliases, "ViewerCertificate": cert, "WebACLId": "",
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "CustomErrorResponses": {"Quantity": 0},  # mascarariam os erros da API
        "Origins": {"Quantity": 2, "Items": [
            origin("spa", f"{bucket}.s3.{REGION}.amazonaws.com", s3_oac, {"S3OriginConfig": {"OriginAccessIdentity": ""}}),
            origin("api", api_host, api_oac, {"CustomOriginConfig": {
                "HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "https-only",
                "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                "OriginReadTimeout": PORTAL_TIMEOUT, "OriginKeepaliveTimeout": 5}}),
        ]},
        "DefaultCacheBehavior": behavior("spa", CACHING_OPTIMIZED, ["GET", "HEAD"]),
        "CacheBehaviors": {"Quantity": 1, "Items": [{"PathPattern": "/api/*", **api}]},
    }


def ensure_distribution(conf: dict) -> tuple[dict, str]:
    sha, cur = conf_sha(conf), find_distribution()
    if not cur:
        d = cf.create_distribution_with_tags(DistributionConfigWithTags={
            "DistributionConfig": conf, "Tags": {"Items": [{"Key": "oba-conf", "Value": sha}]}})["Distribution"]
        note("CloudFront", f"{P}-portal", "criado")
        return d, "criado"
    tags = {t["Key"]: t["Value"] for t in cf.list_tags_for_resource(Resource=cur["ARN"])["Tags"].get("Items", [])}
    if tags.get("oba-conf") == sha:
        note("CloudFront", f"{P}-portal", "mantido")
        return cur, "mantido"
    got = cf.get_distribution_config(Id=cur["Id"])
    merged = {**got["DistributionConfig"], **conf, "CallerReference": got["DistributionConfig"]["CallerReference"]}
    d = cf.update_distribution(Id=cur["Id"], IfMatch=got["ETag"], DistributionConfig=merged)["Distribution"]
    cf.tag_resource(Resource=cur["ARN"], Tags={"Items": [{"Key": "oba-conf", "Value": sha}]})
    note("CloudFront", f"{P}-portal", "atualizado")
    return d, "atualizado"


def portal_bucket_policy(bucket: str, dist_arn: str):
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(doc(
        {"Sid": "CloudFrontOnly", "Effect": "Allow", "Principal": {"Service": "cloudfront.amazonaws.com"},
         "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/*",
         "Condition": {"StringEquals": {"AWS:SourceArn": dist_arn}}},
        {"Sid": "OnlyTls", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
         "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
         "Condition": {"Bool": {"aws:SecureTransport": "false"}}})))


def allow_cloudfront(fn: str, dist_arn: str):
    """Só a distribuição chama a Function URL (as duas permissões são exigidas desde out/2025)."""
    want = {"cloudfront-url": dict(Action="lambda:InvokeFunctionUrl", FunctionUrlAuthType="AWS_IAM"),
            "cloudfront-invoke": dict(Action="lambda:InvokeFunction", InvokedViaFunctionUrl=True)}
    try:
        cur = {s["Sid"]: s for s in json.loads(lam.get_policy(FunctionName=fn)["Policy"])["Statement"]}
    except lam.exceptions.ResourceNotFoundException:
        cur = {}
    for sid, kw in want.items():
        s = cur.get(sid)
        if s and s["Action"] == kw["Action"] and dist_arn in json.dumps(s.get("Condition")):
            continue
        if s:
            lam.remove_permission(FunctionName=fn, StatementId=sid)
        lam.add_permission(FunctionName=fn, StatementId=sid, Principal="cloudfront.amazonaws.com",
                           SourceArn=dist_arn, **kw)


def upload_portal(bucket: str, config_js: bytes) -> int:
    """Sobe portal/web/ e o config.js gerado, pulando o que tem o mesmo md5 (o ETag do SSE-S3)."""
    files = {str(f.relative_to(PORTAL_WEB)): f.read_bytes() for f in sorted(PORTAL_WEB.rglob("*"))
             if f.is_file() and f.name != ".DS_Store"}
    files["config.js"] = config_js
    cur = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        cur.update({o["Key"]: o["ETag"].strip('"') for o in page.get("Contents", [])})
    sent = 0
    for key, body in files.items():
        if cur.get(key) == hashlib.md5(body).hexdigest():
            continue
        s3.put_object(Bucket=bucket, Key=key, Body=body, CacheControl="no-cache",
                      ContentType=MIME.get(Path(key).suffix, "application/octet-stream"))
        sent += 1
    note("site", f"{len(files)} arquivos", f"{sent} enviados" if sent else "mantido")
    return sent


def ensure_portal(data_ep: str) -> tuple[str, str]:
    portal_cfg()
    bucket = ensure_portal_bucket()
    url = portal_url(find_distribution())
    pool = ensure_user_pool(url)
    login = ensure_login_domain(pool)
    client = ensure_client(pool, url)
    ensure_iot_policy(f"{P}-portal-viewer", viewer_doc())
    pid = ensure_identity_pool(pool, client)
    installer = ensure_portal_installer(data_ep)
    api_fn, api_host = ensure_portal_api(pool, client, pid, data_ep, installer)
    s3_oac = ensure_oac(f"{P}-portal-s3", "s3")
    api_oac = ensure_oac(f"{P}-portal-api", "lambda")
    headers = ensure_headers_policy(portal_csp(login, data_ep))
    dist, status = ensure_distribution(dist_conf(bucket, api_host, s3_oac, api_oac, headers))
    portal_bucket_policy(bucket, dist["ARN"])
    allow_cloudfront(api_fn, dist["ARN"])
    if not url:  # 1ª rodada: agora o login sabe para onde voltar
        url = portal_url(dist)
        ensure_user_pool(url)
        ensure_client(pool, url)
    ensure_users(pool)
    js = {"region": REGION, "login": f"https://{login}", "clientId": client, "userPool": pool,
          "identityPool": pid, "url": url, "iotEndpoint": data_ep, "prefix": P, "device": DEV}
    config_js = ("// Gerado pelo setup.py. Não vai para o git.\n"
                 f"window.OBA_PORTAL = {json.dumps(js, indent=2)};\n").encode()
    (BUILD / "portal").mkdir(parents=True, exist_ok=True)
    (BUILD / "portal" / "config.js").write_bytes(config_js)
    if upload_portal(bucket, config_js) and status == "mantido":
        cf.create_invalidation(DistributionId=dist["Id"], InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": str(time.time_ns())})
    if status != "mantido":
        print("  esperando o CloudFront publicar (alguns minutos)...")
        cf.get_waiter("distribution_deployed").wait(Id=dist["Id"], WaiterConfig={"Delay": 20, "MaxAttempts": 60})
    return url, pid


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--new-cert", action="store_true", help="emite um certificado novo e desativa o anterior")
    ap.add_argument("--portal", action="store_true", help="só o portal (login, site e API)")
    ap.add_argument("--resend", metavar="USUÁRIO", help="reenvia o convite do portal (senha temporária vencida)")
    ap.add_argument("--ponte", metavar="NOME",
                    help="credenciais de uma ponte (Claude Code numa máquina) em build/ponte-NOME/; "
                         "com --new-cert, troca o certificado dela")
    args = ap.parse_args()
    init(load_config(Path(args.config)))
    if args.resend:
        return resend_invite(args.resend)
    if args.ponte:
        out = ensure_ponte(args.ponte, args.new_cert)
        print(f"\nPonte {args.ponte}: {out.relative_to(ROOT)}/ (config.json, certificado e chave, só para você)")
        return print(f"Na máquina do Claude Code: python3 ponte/install.py {out.relative_to(ROOT)} (docs/ponte.md)")
    print(f"Oba Pocket: prefixo {P}, placa {DEV}, região {REGION} (Transcribe em {TR_REGION})\n")
    if args.portal:
        url, _ = ensure_portal(iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"])
        return print(f"\nPortal: {url}")

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
    url, _ = ensure_portal(data_ep)

    vocab = wait_vocabulary(vocab)
    write_aws_config(data_ep, cred_ep, alias, vocab)
    print("  gerado     src/aws_config.h")

    changed = [r for r in REPORT if r[2] != "mantido"]
    print(f"\nPronto: {len(REPORT)} recursos, {len(changed)} criados ou alterados.")
    wifi = read_secrets()["wifi"]
    if any("minha-rede" in l or "minha-senha" in l for l in wifi):
        print("Falta o WiFi: preencha WIFI_SSID e WIFI_PASS em src/secrets.h.")
    print(f"Portal: {url}")
    print("Próximos passos: pio run -e core2foraws -t upload, e entre no portal pelo celular.")


if __name__ == "__main__":
    main()
