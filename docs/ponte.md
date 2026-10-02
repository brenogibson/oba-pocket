# A ponte do Claude Code

A ponte mostra no Oba o que o Claude Code está fazendo e leva para a placa os pedidos de
permissão. Você aprova ou nega na tela do Oba, e a resposta volta para o Claude Code.

Ela roda na máquina onde está o Claude Code e fala direto com o AWS IoT Core, como uma
**fonte externa** do Oba ([protocolo](protocol.md#fontes-externas-ext)). Cada máquina é
uma fonte, com o próprio certificado. Um Oba aceita até 4 fontes ao mesmo tempo.

```
Claude Code ──hooks──▶ hook.py ──socket Unix──▶ daemon.py ──MQTT/TLS──▶ AWS IoT Core ──▶ Oba
                        ▲                          │    ◀── ext/<fonte>/re (reply) ◀────┘
                        └──── allow / deny ────────┘    ◀── state (retido)
```

- **`hook.py`**: o Claude Code chama o hook em cada evento. Ele passa o evento para o
  daemon pela `~/.oba-ponte/ponte.sock` e, no pedido de permissão, espera a resposta.
  Sem instalação, ou com qualquer erro, ele sai calado: a ponte nunca atrapalha o
  Claude Code.
- **`daemon.py`**: um processo por máquina, que o primeiro hook sobe. Ele junta as
  sessões num humor, publica em `<p>/<dev>/ext/<fonte>`, recebe as respostas da placa e
  sai sozinho depois de 30 min sem sessões.
- **MQTT**: um cliente MQTT 3.1.1 mínimo, só com a biblioteca padrão do Python (3.9 ou
  mais novo). Não tem nada para instalar com `pip`.

## O que aparece no Oba

As sessões do Claude Code desta máquina viram um humor. Com várias sessões, vale a mais
urgente: `alert`, depois `busy`, depois `idle`.

| No Claude Code | No Oba |
|---|---|
| a sessão começou (`SessionStart`) | `idle` |
| você mandou um prompt, ou ele está usando uma ferramenta | `busy`, com a ferramenta no rótulo |
| pediu permissão (`PermissionRequest`) | `alert` e o pedido na tela |
| outra pergunta no terminal (`Notification`) | `alert` |
| terminou a resposta (`Stop`) | `react: happy`, depois `idle` |
| a resposta deu erro (`StopFailure`) | `react: scared`, depois `idle` |
| vai compactar a conversa (`PreCompact`) | `react: dizzy` |
| esperando você há um tempo (`idle_prompt`) | `idle` |
| a sessão acabou (`SessionEnd`) | a sessão sai; sem sessões, a fonte sai do Oba |
| `busy` ou `alert` por 15 min sem nenhum evento | `idle` |

O rótulo no alto da tela é `<nome> · <pasta> · <ferramenta>`, com até 48 caracteres,
por exemplo `mac · oba-pocket · Bash`. A placa desenha ASCII e Latin-1, então os
acentos aparecem; o que não tem na fonte dela vira `?`.

## Aprovar no Oba

O pedido mostra o título, o que vai acontecer e de onde veio:

| Ferramenta | Título | Corpo |
|---|---|---|
| `Bash` | Rodar comando | `$ <comando>` e a descrição |
| `Edit`, `MultiEdit` | Editar arquivo | o caminho e os trechos `- antigo` / `+ novo` |
| `Write` | Gravar arquivo | o caminho e as primeiras linhas |
| `NotebookEdit` | Editar notebook | o caminho |
| `WebFetch` | Abrir página | a URL |
| `WebSearch` | Buscar na web | a busca |
| `Task`, `Agent` | Chamar agente | a descrição |
| MCP | `MCP <servidor>: <ferramenta>` | os argumentos em JSON |
| as outras | `Usar <ferramenta>` | a entrada em JSON |

Os caminhos dentro do projeto aparecem relativos. O corpo tem até 1500 bytes.

Na placa, **Aprovar** e **Negar** respondem. O botão do meio, **Terminal**, tira o
pedido da placa sem responder: a decisão fica para o terminal. Um pedido perigoso só é
aprovado segurando o botão por 1,5 s. A ponte marca como perigoso, por exemplo,
`rm -r`, `sudo`, `git push --force`, `git reset --hard`, `curl … | sh`,
`chmod -R 777`, `dd of=`, os `aws … delete/terminate/remove`, `kubectl delete`,
`terraform destroy`, `DROP TABLE` e as edições fora da pasta do projeto, em `~/.ssh`,
em `.env` ou em `/etc`. A lista fica em `ponte/plugin/lib/resumo.py` e é fácil de
aumentar.

Também pede a segurada o pedido que não dá para ler inteiro na placa: o corpo que foi
cortado (edições e arquivos grandes, mais de 1500 bytes) ou o comando que teve um
segredo ofuscado. Nesses casos o corpo termina com `(incompleto: confira no terminal)`.
A placa faz o mesmo quando o texto passa de 12 páginas.

**O terminal continua valendo.** Na conversa principal, o Claude Code mostra o diálogo
de permissão no terminal ao mesmo tempo, e vale a primeira resposta. Nos agentes em
segundo plano e no `claude -p`, ele espera o hook antes de mostrar o diálogo (ou de
negar): aí a placa responde primeiro, ou o pedido vence. Se você responder no terminal,
a ponte percebe e tira o pedido da placa (`ask.cancel`). Ela percebe quando:

- a ferramenta roda ou falha (`PostToolUse`, `PostToolUseFailure`);
- chega um prompt novo, o fim da resposta ou o fim da sessão (os pedidos de um agente
  em segundo plano só saem com o fim da sessão: ele continua rodando depois da resposta);
- o transcript da sessão ganha o resultado daquela chamada;
- o pedido vence, em 280 s;
- o Claude Code desiste do hook.

A decisão também fica para o terminal quando você aperta **Terminal**, quando o pedido
vence na placa, com a fila cheia (até 4 pedidos, de todas as fontes) ou com fontes
demais. Com o Oba desligado (o `state` retido diz `online: false`) ou sem conexão com o
IoT Core, o pedido nem sai da máquina: o hook responde na hora, e fica valendo o
terminal. Logo depois de subir (ou de uma queda), a ponte espera até 5 s pela conexão
antes de desistir. Se o Oba desliga com pedidos na tela, eles voltam para o terminal.

`AskUserQuestion` e `ExitPlanMode` não vão para a placa: o Oba fica em `alert` e a
pergunta fica no terminal.

## Instalar

São três passos: criar a fonte na AWS, instalar a ponte na máquina e instalar o plugin
no Claude Code.

**1. Criar a fonte**, na máquina com as credenciais da AWS, na raiz deste repositório:

```bash
python3 setup.py --ponte mac
```

O `setup.py` cria a thing da fonte, o certificado e a policy dela, e grava tudo em
`build/ponte-mac/`:

```
build/ponte-mac/
  config.json          # endpoint, prefix, device, thing e name
  cert.pem             # o certificado desta fonte
  key.pem              # a chave privada (não sai desta pasta)
  AmazonRootCA1.pem
```

O `config.json` tem esta forma (os arquivos são relativos à pasta):

```json
{"endpoint": "xxxx-ats.iot.us-east-1.amazonaws.com", "prefix": "<p>", "device": "<dev>",
 "thing": "<fonte>", "name": "mac",
 "cert": "cert.pem", "key": "key.pem", "ca": "AmazonRootCA1.pem"}
```

`name` é o que aparece no rótulo (até 20 caracteres). `port` é opcional: `8883`, o
padrão, ou `443`, para redes que só deixam passar HTTPS (usa o ALPN `x-amzn-mqtt-ca`).

**2. Instalar a ponte**, na máquina do Claude Code. Copie `build/ponte-mac/` para ela
por um caminho seguro (é uma chave privada) e rode:

```bash
python3 ponte/install.py build/ponte-mac/ --check
```

O `install.py` confere o pacote, abre o certificado e a chave, e copia tudo para
`~/.oba-ponte/` (pasta `0700`, arquivos `0600`). Com `--check`, ele conecta no IoT Core,
publica um `idle` com o rótulo `mac · teste`, espera o Oba mostrar a fonte e publica
`online: false`. Depois dá para apagar a cópia de `build/ponte-mac/` desta máquina.

**3. Instalar o plugin.** O `install.py` mostra os dois comandos, com o caminho certo,
mas não roda nenhum deles:

```bash
claude plugin marketplace add /caminho/do/repo/ponte
claude plugin install oba-ponte@oba-pocket --scope user
```

A pasta `ponte/` é o marketplace, e `ponte/plugin/` é o plugin. Abra uma sessão nova do
Claude Code: o primeiro evento sobe o daemon, e o Oba mostra a fonte.

O `--check` termina com `Tudo certo` e código 0 só quando o Oba mostra a fonte. Com o Oba
desligado, ele diz que a AWS aceitou mas a volta não foi confirmada, e sai com 1.

**Atualizar.** Depois de atualizar o repositório, rode:

```bash
claude plugin marketplace update oba-pocket
claude plugin update oba-ponte@oba-pocket
```

Nas sessões novas, o primeiro hook da versão nova para o daemon velho (assim que ele
não tiver pedido aberto), e o hook seguinte sobe o novo. O daemon velho anota a pasta da
versão nova em `~/.oba-ponte/ponte.lib`: daí em diante, até os hooks das sessões abertas
antes da atualização sobem o daemon novo, e o velho não volta. Para trocar na hora, rode
`python3 ponte/install.py --check`. Reinstalar com o `install.py` apaga essa anotação
(para voltar a uma versão anterior do plugin, por exemplo).

Para ver a ponte funcionando sem o Claude Code, rode o simulador. Ele usa o `hook.py` de
verdade com eventos falsos: uma sessão lê, edita, pede permissão e termina.

```bash
python3 ponte/sim.py              # um pedido comum
python3 ponte/sim.py --danger     # um rm -rf (segure para aprovar)
python3 ponte/sim.py --sessions 3 # três sessões ao mesmo tempo
```

## Várias máquinas

Um Oba, várias fontes: rode `python3 setup.py --ponte <nome>` uma vez por máquina, com
um nome diferente para cada uma (`mac`, `note`, `servidor`). Cada máquina tem o próprio
certificado e o próprio tópico, e o nome aparece no rótulo, então dá para saber de onde
veio cada pedido. O Oba mostra o humor mais urgente entre todas.

Na mesma máquina, todas as sessões do Claude Code dividem uma fonte e um daemon. O
`OBA_PONTE_HOME` troca a pasta, se precisar de duas fontes na mesma máquina. Ele tem que
ser um caminho absoluto, e o Claude Code precisa receber o mesmo valor, por exemplo
`OBA_PONTE_HOME=$HOME/.oba-ponte-2 claude` (com um caminho relativo, a ponte não faz
nada).

## Claude Code no Bedrock

A ponte funciona do mesmo jeito com o Claude Code no Amazon Bedrock
(`CLAUDE_CODE_USE_BEDROCK=1`). Os hooks rodam na máquina, e a ponte só fala com o AWS
IoT Core da sua conta. Não depende de nenhum serviço do claude.ai, nem de login nele.

## Segurança

- **Um certificado por máquina.** Cada fonte tem o próprio certificado, que dá para
  revogar sozinho no IoT Core sem mexer no Oba nem nas outras máquinas.
- **Só o tópico dela.** A policy da fonte só deixa conectar com o nome dela, publicar em
  `<p>/<dev>/ext/<fonte>` e ler `<p>/<dev>/ext/<fonte>/re` e o `state` do Oba. A fonte
  não manda `cmd` para a placa, não lê o que a placa ouve e não se passa por outra
  fonte ([policy da fonte](protocol.md#policy-da-fonte)).
- **O portal não vê os pedidos.** Nada passa pelo roteador nem fica gravado na nuvem:
  o `ext` não é retido, e o portal só vê o resumo em `state.ext`.
- **Segredos ofuscados.** Antes de sair da máquina, o título e o corpo passam por
  `resumo.redact`: chaves AWS (`AKIA…`, `ASIA…`, `aws_secret_access_key`),
  `password=`, `token=`, `api_key=` e parecidos, `Authorization: Bearer`, blocos de
  chave privada, hex longo (32 ou mais), base64 longo (40 ou mais) e ids de conta AWS
  (12 dígitos viram `<conta>`). É uma rede de segurança, não uma garantia: o corpo
  ainda mostra o comando e os caminhos. Depois de `token=`, `password=`, `--password`,
  `Authorization` e parecidos, um valor com `$(…)` ou crase, sem aspas ou entre aspas
  duplas, não vira `<segredo>`, porque é código que vai rodar. Entre aspas simples ele é
  ofuscado, porque aí o shell não expande. Chaves AWS, hex, base64 e chaves privadas são
  ofuscadas mesmo dentro de `$(…)` ou de crase: aí o pedido sai incompleto e pede a
  segurada. Os espaços em série viram um só, para nenhum trecho do comando ficar fora
  da tela.
- **Aprovar só na placa.** A ponte não aprova nada sozinha. Sem resposta da placa, ela
  não imprime nada, e quem decide é o terminal. Pela serial, só o firmware dev aprova
  (`Y`/`N`, com `pio run -e dev -t upload`).
- **Tudo local fica fechado.** `~/.oba-ponte/` é `0700`, e a chave, o socket e o log
  são `0600`. O log só tem tipos de evento, ids e erros: nunca o `tool_input`, o
  transcript, comandos ou caminhos. Do transcript, a ponte lê os blocos `tool_use` (id,
  nome e entrada, para achar a chamada que é o pedido) e `tool_result` (o id). Ela só
  guarda os ids e não copia nada para o log nem para a rede.

## Desinstalar

```bash
python3 ponte/install.py --uninstall
```

Ele para o daemon, apaga `~/.oba-ponte/` e mostra os comandos do Claude Code:

```bash
claude plugin uninstall oba-ponte@oba-pocket
claude plugin marketplace remove oba-pocket
```

Na AWS, revogue e apague o certificado da fonte e a thing dela. Sem o certificado, a
máquina não conecta mais, mesmo que tenha ficado alguma cópia da chave.

Perdeu a máquina, ou a chave vazou? Rode `python3 setup.py --ponte <nome> --new-cert`.
Ele cria um certificado novo em `build/ponte-<nome>/` e desativa os anteriores daquela
ponte, que saem da thing. Para continuar usando a ponte, copie a pasta nova para a
máquina e instale de novo, como no passo 2:
`python3 ponte/install.py build/ponte-<nome>/ --check`.

## Limites

- O pedido que você aprova no terminal sai da placa quando a ferramenta termina (é o
  `PostToolUse` ou o transcript que avisa), e não no momento em que você responde. Um
  comando demorado deixa o pedido na placa até acabar.
- O pedido vence em 280 s. O Claude Code espera o hook até 300 s.
- Uma pergunta do `AskUserQuestion` e o plano do `ExitPlanMode` só aparecem como
  `alert`: a resposta fica no terminal.
- A placa enfileira até 4 pedidos de todas as fontes juntas. Nomes longos de
  ferramentas MCP são cortados em 40 caracteres.
- Sem rede, o daemon tenta de novo sozinho (até 1 min entre tentativas) e manda de novo
  o status e os pedidos pendentes quando volta.

## Desenvolvimento

```
ponte/
  .claude-plugin/marketplace.json
  plugin/
    .claude-plugin/plugin.json
    hooks/hooks.json          # todos os eventos chamam lib/hook.py
    lib/hook.py               # o hook
    lib/daemon.py             # o daemon e a lógica (Ponte)
    lib/mqtt.py               # o cliente MQTT 3.1.1
    lib/resumo.py             # resumo, ofuscação e perigo
    lib/comum.py              # pasta, configuração e socket
  install.py
  sim.py
  tests/                      # unittest, com um broker MQTT falso (broker.py)
```

Os testes não usam rede nem a pasta `~/.oba-ponte`:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ponte/tests -v
claude plugin validate --strict ponte
```
