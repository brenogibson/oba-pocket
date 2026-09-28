# Oba Pocket

[English](README.en.md) · **Português**

<p align="center"><img src="docs/img/nimbo.gif" width="320" alt="O Nimbo, uma nuvenzinha, olhando em volta, feliz, tonto, com sono, com vergonha e falando num balão"></p>

Obas são agentes de IA com corpo. O corpo mora num M5Stack Core2: rosto animado,
balão de fala, toque, sensores, LEDs e vibração. O cérebro é o harness, que roda fora
da placa. Os dois conversam por MQTT, pelo [Oba Protocol v1](docs/protocol.md).

Cada Oba é uma pasta com um `oba.json`, que diz como ele é, como reage sozinho e qual
agente é o cérebro dele ([como fazer um Oba](docs/oba.md)). O cartão microSD é a
biblioteca de Obas. Sem cartão, a placa usa o Oba embutido, o Nimbo (`obas/nimbo`).

## O que ele faz

- **Reage na hora, sem nuvem:** toque, chacoalhão, batidinha e barulho viram humores,
  sons e LEDs, pela tabela de reflexos do Oba.
- **Acompanha uma reunião:** com o REC ligado, o áudio vai direto da placa para o
  Amazon Transcribe. O agente lê as legendas e responde com um balão na hora ou com
  uma "carta na manga", uma regra que dispara na placa quando alguém volta ao assunto.
  Quando o REC desliga, o resumo aparece na tela separada.
- **Atende pelo nome:** com o REC ligado, falar o nome do Oba faz ele reagir na hora e
  publica o evento `wake`. O agente só acorda com isso se o Oba tiver `{"on": "wake"}`
  em `agent.triggers`.
- **Troca de Oba pelo ar:** `tools/oba.py install` manda um Oba pelo MQTT, a placa
  confere o sha256 de cada arquivo e pergunta na tela antes de instalar.
- **Dois tipos de corpo:** vetorial (rig), como o Nimbo, ou pixel art em PNG com sons
  WAV, como o Bit (`obas/bit`).

<p align="center"><img src="docs/img/bit.png" width="480" alt="O Bit, um robozinho de pixel art, parado, feliz, tonto e com sono"></p>

## Arquitetura

```
 placa ──áudio (só com REC)──▶ Amazon Transcribe
   │ ▲
   │ └── cmd ◀── roteador (Lambda) ◀──▶ agente (AgentCore)
   │                ▲
   │                └────────────────────────────────────────────────┐
   └── state, evt, transcript, reply ──▶ AWS IoT Core ──▶ IoT Rule ──┘
                                                 └──▶ display/ (tela separada, só leitura)
```

- **Placa** (`src/`): mantém o Oba ativo na PSRAM, desenha a 30 quadros por segundo,
  roda os reflexos e publica eventos de alto nível (`touch.tap`, `imu.shake`, `wake`,
  legendas). Nunca manda o fluxo cru dos sensores.
- **Roteador** (`harness/router/`): aplica os gatilhos do Oba ativo (lote, debounce,
  cooldown), guarda a sessão no DynamoDB, busca o `oba.json` no registro S3 e publica
  as ações do agente.
- **Agente** (`harness/agent/`): um agente [Strands](https://strandsagents.com)
  genérico no Amazon Bedrock AgentCore, montado a partir do `agent` do `oba.json`
  (persona, modelo, habilidades, MCPs de uma lista permitida). Qualquer agente que
  siga o [contrato](docs/protocol.md#contrato-roteador--agente) pode ser o cérebro de um Oba.
- **Tela separada** (`display/`): uma página que mostra as legendas, as cartas e o
  resumo. Só lê os tópicos da placa.

Nada no protocolo depende da AWS: o MQTT fica isolado em `src/cloud.cpp` e o harness
segue um contrato simples.

## Precisa de

- Um M5Stack Core2 for AWS (EduKit). Um cartão microSD é opcional: se não estiver em
  FAT32, a placa oferece formatar (segurando para confirmar).
- Uma conta da AWS com um perfil na AWS CLI que possa criar os recursos, e acesso no
  Bedrock ao modelo do `config.json`.
- [PlatformIO Core](https://platformio.org), Python 3.10 ou mais novo, `openssl` e o
  [uv](https://docs.astral.sh/uv/) (ou o pip) para empacotar o harness.

## Subindo

```sh
cp config.example.json config.json            # prefixo, placa, regiões, idioma, modelos
python3 -m venv .venv && . .venv/bin/activate
pip install boto3 jsonschema paho-mqtt pyserial pillow
python3 setup.py                              # AWS: placa, harness e tela, com menor privilégio
# preencha WIFI_SSID e WIFI_PASS em src/secrets.h (o setup avisa se faltar)

pio run -e core2foraws -t upload              # firmware
cd display && python3 -m http.server 8080     # tela separada: http://localhost:8080
```

O `setup.py` usa o perfil padrão da AWS CLI (ou `AWS_PROFILE`) e pode rodar de novo
quantas vezes quiser: só muda o que mudou. Ele cria o thing e o certificado, as
policies, o role alias do Transcribe, o vocabulário, o registro S3, a tabela, a Lambda,
a IoT Rule do roteador, o runtime do AgentCore e o Cognito da tela. Também gera
`src/aws_config.h`, `display/config.js` e `src/secrets.h`. A chave privada da placa
nasce nesta máquina e nunca vai para a AWS.

Na placa:

- **REC** (canto de cima, à esquerda): liga e desliga a transcrição. Só o botão liga
  o microfone; o agente pode desligar, nunca ligar.
- **Botão do meio:** abre a tela de escolha de Obas (com o REC desligado). Tocar
  escolhe, segurar remove.
- **Toque no Oba:** carinho. Chacoalhar, bater e fazer barulho também contam.

## Criar um Oba

```sh
mkdir -p obas/meu-oba && $EDITOR obas/meu-oba/oba.json
python3 tools/oba.py validate obas/meu-oba             # confere o que a placa confere
python3 tools/oba.py install obas/meu-oba --activate   # manda pelo ar e ativa
```

O [guia](docs/oba.md) explica cada campo: aparência (rig ou sprites), humores,
reflexos, sons, palavras de ativação, recursos pedidos e o cérebro (`agent`). O
formato está em [`schema/oba.schema.json`](schema/oba.schema.json). O Nimbo e o Bit
servem de exemplo; o `obas/bit/draw.py` gera os quadros e os sons do Bit.

Obas que você não quer publicar podem ficar em `obas.private/`: o git ignora a pasta,
e o `setup.py` também sobe os Obas de lá para o registro.

## Protocolo

O [Oba Protocol v1](docs/protocol.md) usa seis tópicos em `<prefixo>/<placa>/`:
`state` (retido), `evt`, `transcript` e `reply`, da placa, e `cmd` e `ui/<canal>`, do
harness. O documento traz o envelope, cada evento e comando (`speak`, `arm`, `react`,
`look`, `vibrate`, `leds`, `play`, `read`, `oba.install`…), os gatilhos, a sessão, o
contrato roteador → agente e a policy IAM mínima da CLI.

## Pastas

| Pasta | |
|---|---|
| `src/` | firmware (PlatformIO, Arduino) |
| `harness/router/` | roteador: gatilhos do Oba, sessão, regras armadas |
| `harness/agent/` | agente genérico (Strands) e as habilidades (`abilities/`) |
| `obas/` | Obas públicos: Nimbo e Bit |
| `schema/` | formato do `oba.json` |
| `display/` | tela separada no navegador |
| `docs/` | protocolo e guia de Obas |
| `tools/` | monitor serial, validação e instalação de Obas, fontes e ícones |

## Ferramentas

- `tools/oba.py validate <pasta>`: confere um Oba contra o schema, os sha256 e os limites.
- `tools/oba.py install <pasta> [--activate]`: instala um Oba na placa pelo ar (MQTT) e,
  depois do toque em "Instalar" na tela dela, sobe para o registro S3 (`--no-registry`
  não mexe no registro). Os outros comandos pela nuvem: `remove <id> [--registry]`,
  `activate <id>`, `play <som> [--volume N]`, `list [--registry]` e `publish <pasta>`
  (só o registro). Todos usam a placa do `config.json` (`--device` troca) e o perfil da
  AWS CLI, com a [policy mínima](docs/protocol.md#policy-iam-mínima-da-cli).
- `tools/monitor.py`: log da serial, prints da tela (`s`), REC (`r`), fala simulada
  (`t <frase>`) e Obas pela serial (`u <pasta>`, `a <id>`, `l`, `o`). Acha a placa no
  USB sozinho.
- `tools/svg_to_outline.py`: transforma um SVG no contorno de um Oba rig.
- `tools/make_font.py`: gera a fonte do balão a partir da Nunito.
- `tools/fetch_icons.py`: baixa os ícones da AWS para o roteador (o `setup.py` chama
  quando faltam; eles não vão para o repo).

## Segredos

`src/secrets.h` (WiFi, certificado e chave da placa), `src/aws_config.h`,
`display/config.js` e `config.json` são gerados ou preenchidos por você e ficam fora
do git. Nada do que está no repo depende de uma conta específica.

## Licença

O código está sob a [licença MIT](LICENSE). A fonte Nunito (`tools/fonts/` e a fonte
gerada em `src/bubble_fonts.cpp`) segue a [SIL Open Font License 1.1](tools/fonts/Nunito-OFL.txt).
Os ícones da AWS não vêm no repo: o `tools/fetch_icons.py` baixa o pacote oficial,
que tem os termos dele.
