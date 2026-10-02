# Segurança

[English](#english) · **Português**

## Como reportar

Achou uma falha? Reporte em particular pelo GitHub: na aba **Security** do repositório,
clique em **Report a vulnerability**. Não abra uma issue pública e não mande por e-mail.

Conte o que dá para fazer, os passos para reproduzir e a versão (o `fw` do `state`, ou o
commit). Não mande chaves, certificados nem ids da sua conta.

O Oba Pocket é um projeto pessoal, mantido no tempo livre: não tem prazo garantido de
resposta nem de correção.

## O que é coberto

O código deste repositório, na versão mais recente: o firmware (`src/`), o harness
(`harness/`), o portal (`portal/`), a ponte (`ponte/`), o `setup.py` e as ferramentas
(`tools/`).

Fica de fora o que não é deste repositório: os serviços da AWS, os servidores MCP do
catálogo, as bibliotecas de terceiros (reporte no projeto delas) e versões antigas ou
forks.

## Modelo de ameaça

Quem pode ler e comandar a placa:

- **A própria placa**, com o certificado dela. Só publica nos tópicos dela
  (`<prefixo>/<placa>/`) e só recebe o `cmd` e o `ext/<fonte>`. As credenciais
  temporárias que ela pega com o certificado só abrem streams do Transcribe.
- **Os usuários do portal**, com login no Cognito. Leem as legendas, o resumo e o
  estado da placa, mandam os comandos da tela dela (ativar, remover, tocar um som,
  desligar o REC) e enviam, instalam e apagam Obas do registro. Não leem os pedidos das
  fontes externas e não ligam o REC.
- **As pontes do Claude Code**, cada uma com o próprio certificado. Cada ponte só
  publica no próprio `ext/<fonte>` e só lê a resposta dela e o `state` da placa.
- **O harness** (roteador e agente), na sua conta. O que alguém fala perto da placa, com
  o REC ligado, vira texto para o agente. O roteador só deixa passar as ações da lista,
  nunca liga o REC e só manda links de domínios permitidos.
- **Quem tem acesso à sua conta da AWS**, como a CLI (`tools/oba.py`), faz o que o IAM
  dele liberar. A [policy mínima](docs/protocol.md#policy-iam-mínima-da-cli) da CLI só
  fala com a placa e com o registro.

Algumas coisas não são falha, mas é bom saber:

- **Acesso físico à placa é acesso à chave dela.** O certificado, a chave e a senha do
  WiFi ficam na flash sem criptografia. Com um cabo USB, dá para ler tudo e gravar Obas
  pela serial. Se perder a placa, troque o certificado e a senha do WiFi
  ([Perdeu a placa?](README.md#perdeu-a-placa)).
- **O portal não tem WAF nem limite por taxa.** A API recusa pedido sem token, mas quem
  tiver o endereço consegue mandar pedidos sem parar. A concorrência reservada da API
  (10, se a cota da conta deixar) segura a Lambda, mas o portal fica lento, e cada
  pedido conta no CloudFront e na Lambda. Se precisar, associe um WebACL do AWS WAF à
  distribuição.
- **O agente `http` é só para desenvolvimento.** O roteador chama sem autenticação, e
  quem tiver a URL usa o seu Bedrock. Fora de `localhost`, `127.0.0.1` e `::1`, a URL
  precisa ser `https`.
- **Os comandos de teste da serial** (REC, fala simulada, eventos falsos e responder aos
  pedidos das fontes externas) só existem no firmware dev (`pio run -e dev -t upload`).
  Não grave o firmware dev numa placa que sai de perto de você.

Os detalhes estão no [README](README.md#por-que-na-nuvem), no
[protocolo](docs/protocol.md#segurança) e na [ponte](docs/ponte.md#segurança).

## English

**Reporting:** report vulnerabilities privately through GitHub: on the repository's
**Security** tab, click **Report a vulnerability**. Please don't open a public issue or
send email. Don't include keys, certificates or account ids.

**Scope:** the code in this repository, latest version. AWS services, the catalog's MCP
servers, third-party libraries and old versions or forks are out of scope.

**Threat model, in short:** the device only publishes on its own topics and only
receives `cmd` and `ext/<source>`. Portal users can read the captions, the summary and
the state, send the device screen's commands and upload, install and delete Obas, but
can't read external source requests or turn REC on. Each Claude Code bridge only talks
on its own `ext/<source>`. Physical access to the device means access to its key: the
flash isn't encrypted ([Lost the board?](README.en.md#lost-the-board)). The portal has
no WAF or rate limit. The `http` agent is for development only and has no
authentication.

Oba Pocket is a personal project, maintained in spare time: there's no guaranteed
response or fix time.
