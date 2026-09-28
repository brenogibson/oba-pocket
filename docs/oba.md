# Como fazer um Oba

Um Oba é uma pasta com um `oba.json`. O JSON diz como ele é, como reage sozinho na
placa e qual agente é o cérebro dele. O formato está em
[`schema/oba.schema.json`](../schema/oba.schema.json), e `tools/oba.py validate` confere
um Oba contra ele.

```
obas/nimbo/
  oba.json
  nimbo.svg        # de onde saiu o contorno (não vai para a placa)

obas/bit/
  oba.json
  draw.py          # gera os quadros e os sons (não vai para a placa)
  sprites/*.png    # folhas de quadros, uma por humor
  sons/*.wav       # sons dos reflexos
```

Na placa, cada Oba fica em `/obas/<id>/` no cartão SD. Sem cartão, a placa usa o Oba
embutido no firmware, o Nimbo. Só vão para a placa o `oba.json` e os arquivos listados
em `files`.

## O mínimo

```json
{
  "oba": 1,
  "id": "nimbo",
  "name": "Nimbo",
  "version": "1.0.0",
  "look": {
    "palette": {"bg": "#5DB8F5", "shadow": "#3A8FD0", "body": "#FFFFFF", "outline": "#B9DDF7",
                "eye": "#1B2A4A", "blush": "#FFA8C5", "star": "#FFD84A", "text": "#EAF6FF",
                "bubble": "#FFFFFF", "ink": "#133056"},
    "outline": [[-57, 54], [-91, 17], [-62, -24], [-17, -65], [16, -46], [68, -13], [91, 21], [59, 54]],
    "eyes": {"left": -22, "right": 22, "y": -8}
  }
}
```

- `id`: de 1 a 31 caracteres, só `a-z`, `0-9`, `_` e `-`. Também é o nome da pasta.
- `name`: aparece na tela de escolha e é o nome do Oba para o agente.
- `version`: a tela de escolha e o `state` mostram. Suba a versão a cada mudança.
- `description`, `author` e `license` são opcionais.

Tudo o que não estiver no JSON usa o padrão do motor.

## Aparência (`look`)

Tem dois tipos de corpo, escolhidos por `look.type`:

- `"rig"` (o padrão, sem `type`): um polígono vetorial preenchido, com olhos e rosto
  desenhados pelo motor por cima. É o Nimbo.
- `"sprites"`: quadros de pixel art em PNG, um conjunto por humor. É o Bit.

Nos dois, as coordenadas são em pixels da tela, a partir do centro do Oba, com o y
para baixo. A tela tem 320×240, e o Oba fica em pé no meio dela (centro em 160, 116).
Os movimentos dos humores (sobe e desce, balanço, achatar, virada, ida para o canto) e
a prévia da tela de escolha valem igual para os dois.

### Rig

| Campo | |
|---|---|
| `palette` | cores em `#RRGGBB`. O fundo da tela (`bg`) também é a cor dos LEDs no idle e no sonolento |
| `outline` | contorno do corpo, de 3 a 256 pontos, no sentido horário |
| `wave` | a parte de baixo balança: abaixo de `from`, cada vez mais até `from + span` |
| `eyes` | x de cada olho, y, raios (`rx`, `ry`) e quanto andam ao olhar para cada lado (`look`) |
| `cheeks` | bochechas, a partir de cada olho |
| `arms` | bracinhos do tímido tapando os olhos: arcos a partir de cada olho |
| `shadow` | sombra no chão |
| `anchors` | onde saem o rabinho do balão (`bubble`), as estrelinhas do tonto (`stars`) e o "z" do sonolento (`zzz`) |

`tools/svg_to_outline.py` transforma o primeiro `<path>` de um SVG em pontos
para o `outline`.

### Sprites

```json
"look": {
  "type": "sprites",
  "palette": {"bg": "#1D2547", "shadow": "#121833", "star": "#FFE066", "text": "#EAF0FF",
              "bubble": "#FFFFFF", "ink": "#1D2547"},
  "size": [40, 40],
  "origin": [20, 20],
  "scale": 4,
  "frames": {
    "idle":  {"sheet": "sprites/idle.png", "fps": 4},
    "happy": {"sheet": "sprites/happy.png", "fps": 8}
  },
  "shadow": {"y": 80, "rx": 46, "ry": 6, "dx": 0, "bob": 1},
  "anchors": {"bubble": [56, -36], "stars": {"x": 0, "y": -70, "rx": 64, "ry": 12},
              "zzz": {"x": 48, "y": -48, "dx": 40, "dy": -45}}
}
```

| Campo | |
|---|---|
| `palette` | obrigatórias `bg`, `shadow`, `star`, `text`, `bubble` e `ink`. `rec`, `wait` e `off` são opcionais como no rig. `body`, `outline`, `eye` e `blush` também são opcionais: a placa não usa, mas a tela separada usa (sem elas, `ink` e `star`) |
| `size` | `[w, h]` de um quadro, em pixels do PNG, de 8 a 96 cada |
| `origin` | o ponto do quadro que fica no centro do Oba (de onde saem a sombra e as âncoras). Pode ser fracionário, de 0 a `w` e de 0 a `h` |
| `scale` | inteiro de 1 a 8: cada pixel do PNG vira `scale`×`scale` na tela. `w`·`scale` até 240 e `h`·`scale` até 200 |
| `frames` | as folhas de cada humor (`idle` é obrigatório; humor sem folha usa a do `idle`) |
| `shadow`, `anchors` | como no rig, em pixels da tela (já depois do `scale`) |

Cada folha (`sheet`) é um PNG com os quadros lado a lado numa tira horizontal: altura
`h` e largura N×`w`, com 1 ≤ N ≤ 32. O quadro da vez é
`floor(tempo no humor × fps / 1000) mod N`, com `fps` de 1 a 30 (padrão 6). A mesma
folha pode servir para mais de um humor.

Regras do PNG (o validador e a placa recusam o resto):

- sem entrelaçamento (Adam7);
- transparência só de 0 ou 255: nada de borda suavizada. Desenhe pixel a pixel e
  salve sem antialias;
- a placa decodifica todas as folhas ao ativar o Oba e guarda largura × altura × 3
  bytes de cada uma. Somando as folhas distintas, até 1 MB (dá umas 54 folhas de
  40×40 com 4 quadros).

`wave`, `eyes`, `cheeks`, `arms` e `outline` não existem nos sprites: o rosto está no
desenho. Por isso `face.eyes` e `face.cheeks` dos humores não valem, e olhar para os
lados (`glance`, olhos seguindo o dedo) e piscar não aparecem. Se quiser piscada,
ponha nos quadros, como o `idle` do Bit. O `face.extra` (estrelinhas, "z") é desenhado
pelo motor por cima, nas `anchors`.

O `obas/bit/draw.py` é um exemplo de como gerar as folhas por código (Pillow, sem
nada sorteado). Qualquer editor de pixel art serve, desde que exporte PNG sem
entrelaçamento e sem alpha parcial.

## Humores (`moods`)

Os humores são fixos: `idle`, `happy`, `scared`, `shy`, `dizzy` e `sleepy`. Cada Oba
só diz o que muda no jeito dele de se mexer:

```json
"moods": {
  "idle": {"bob": {"sin": [1.6, 6]}, "sway": {"sin": [0.8, 3]}},
  "happy": {"leds": {"fx": "rainbow", "speed": 80}, "ms": 2500}
}
```

| Campo | |
|---|---|
| `bob`, `sway`, `squash`, `wave` | sobe e desce, lado a lado, achata e estica, e o balanço de baixo |
| `face` | olhos (`open`, `closing`, `happy`, `scared`, `spiral`, `covered`), bochechas (`true`, `false`, `"touch"`), extra (`none`, `stars`, `zzz`) e `dy`. Nos sprites, só `extra` e `dy` |
| `leds` | efeito da barra de LEDs: `off`, `solid`, `breathe`, `rainbow`, `strobe` ou `chase` |
| `ms` | quanto dura o humor. No `idle`, é o tempo sem interação até dormir (0 = nunca) |
| `quiet_ms` | só sai do humor depois desse tempo sem barulho alto |
| `then` | para onde vai quando acaba |

Cada movimento pode ser:

- um número;
- `{"sin": [vel, amp, base?]}`: `sin(t·vel)·amp + base`;
- `{"bounce": [vel, amp, base?]}`: pulinhos, `-|sin(t·vel)|·amp + base`;
- `{"jitter": [min, max]}`: tremido, um inteiro sorteado a cada quadro.

## Reflexos (`reflexes`)

Reações na hora, sem nuvem. Numa lista em ordem, o primeiro reflexo que casar com o
evento no humor atual ganha. Sem `reflexes`, vale a tabela padrão:

```json
"reflexes": [
  {"on": "sound.loud", "do": "scared", "from": ["idle", "happy", "sleepy"]},
  {"on": "sound.voice", "do": "glance", "from": ["idle"]},
  {"on": "imu.tap", "do": "turn", "except": ["dizzy"]},
  {"on": "imu.shake", "do": "dizzy", "except": ["dizzy"]},
  {"on": "touch.tap", "do": "idle", "from": ["shy"]},
  {"on": "touch.tap", "do": "happy", "except": ["scared", "dizzy", "shy"]},
  {"on": "wake", "do": "happy"},
  {"on": "bubble.open", "do": "happy", "except": ["scared", "dizzy"]}
]
```

- `on`: `touch.tap`, `sound.loud`, `sound.voice`, `imu.shake`, `imu.tap`, `wake` (ouviu o
  nome dele) ou `bubble.open`.
- `do`: um humor, `glance` (olha rápido para um lado), `turn` (vira para o lado do teco)
  ou `none`.
- `from` / `except`: em que humores o reflexo vale. Use um ou outro, nunca os dois.
- `sound`: o nome de um som de `sounds` para tocar junto (abaixo).

## Sons (`sounds`)

```json
"sounds": {"oi": "sons/oi.wav", "yay": "sons/yay.wav", "ai": "sons/ai.wav"},
"reflexes": [
  {"on": "touch.tap", "do": "happy", "sound": "yay"},
  {"on": "sound.loud", "do": "scared", "from": ["idle", "happy", "sleepy"], "sound": "ai"},
  {"on": "wake", "do": "happy", "sound": "oi"}
]
```

- Nome: de 1 a 31 caracteres, só `a-z`, `0-9`, `_` e `-`. Até 16 sons.
- Formato: WAV (RIFF) PCM, 1 canal, 16000 Hz, 16 bits. Qualquer outro formato é
  recusado. Para converter: `ffmpeg -i entrada.mp3 -ac 1 -ar 16000 -c:a pcm_s16le saida.wav`.
- Cada som tem até 10 s, e os dados de todos os sons somados até 512 KB (uns 16 s).
- Tocam pelo `sound` dos reflexos, pelo comando `play` (do agente, das regras armadas
  ou de `tools/oba.py play`) e **só com o REC desligado**: no Core2 o alto-falante e o
  microfone dividem o mesmo pino, então enquanto grava o Oba fica em silêncio.
- Com `sounds`, `requires` precisa ter `"speaker"`.

## Arquivos (`files`)

Todo arquivo que o Oba usa (as folhas e os sons) precisa estar em `files`, com o
sha256 dos bytes em hexadecimal minúsculo:

```json
"files": {
  "sons/oi.wav": "186ab38f441e1db8a40dd86c241571bcafbe99aa0794fc48ad3cb85180aefe96",
  "sprites/idle.png": "1984ab90f011de5deccf9d23c2abc244ce4c2f867db7654ce199999683272ba6"
}
```

- Caminho relativo à pasta do Oba: só `A-Z`, `a-z`, `0-9`, `.`, `_`, `/` e `-`, até 64
  caracteres e 4 pastas (`a/b/c/d/x.png`), sem `..` nem `//`, sem começar com `/` ou `.` e
  sem terminar em `/`. O
  cartão não diferencia maiúsculas: o validador recusa dois caminhos que só mudam nisso.
- Todo item de `files` precisa existir na pasta. O `oba.json` não entra.
- É isso que a instalação pelo ar manda, e a placa confere o sha de cada arquivo.
- Mudou um PNG ou um WAV? Atualize o sha. No Bit, o `draw.py` já reescreve `files`;
  para os seus, `shasum -a 256 sprites/*.png sons/*.wav` dá os valores.

## Palavras de ativação (`wake_words`)

O nome do Oba e as grafias que o Transcribe costuma devolver para ele, até 8. Quando
uma delas aparece na transcrição, a placa dispara o reflexo `wake` e publica o evento
`wake`. O `setup.py` põe essas palavras no vocabulário do Transcribe, para ele
reconhecer melhor.

## Recursos (`requires`)

O que o Oba usa da placa: `display`, `touch`, `buttons`, `imu`, `mic.level`,
`mic.transcribe`, `speaker`, `leds`, `vibration`, `battery` e `sd`. Na instalação pelo
ar, a placa mostra essa lista em português ("Pede: tela, toque, movimento,
alto-falante…") e só instala se você aceitar. Um Oba com `sounds` precisa pedir
`speaker`.

## O cérebro (`agent`)

O agente roda fora da placa, no harness (veja [protocol.md](protocol.md)). Sem `agent`,
o Oba só tem os reflexos.

```json
"agent": {
  "endpoint": "default",
  "persona": "Você é o Nimbo, uma nuvenzinha simpática que…",
  "model": "us.anthropic.claude-sonnet-5",
  "abilities": ["meeting"],
  "triggers": [
    {"on": "transcript.final", "batch": 2, "cooldown_ms": 8000},
    {"on": "rec.off", "run": "summary"}
  ],
  "mcp": ["aws-knowledge"]
}
```

| Campo | |
|---|---|
| `endpoint` | o nome de um agente no catálogo do harness (`agents` no `config.json`): um runtime do AgentCore (`{"type": "agentcore", "arn": "…"}`) ou uma URL que recebe o [contrato](protocol.md#contrato-roteador--agente) num POST (`{"type": "http", "url": "…"}`). `default` é o agente genérico |
| `persona` | quem o Oba é, em texto livre. É o começo de todo prompt dele |
| `model` | um modelo da lista permitida do harness. Sem o campo, vale o primeiro da lista |
| `abilities` | habilidades prontas do agente genérico (abaixo) |
| `triggers` | quais eventos acordam o agente e como ([gatilhos](protocol.md#gatilhos-agenttriggers-no-json-do-oba)) |
| `mcp` | ferramentas do catálogo do harness: `aws-knowledge` (documentação da AWS) e `demos` (se configurado) |

Por segurança, o Oba só escolhe por nome. Endpoints, modelos e MCPs precisam estar
nas listas do harness, e o roteador ignora o que não estiver.

### Habilidades

| Nome | `run` | O que faz |
|---|---|---|
| `meeting` | `think` | acompanha a conversa. Solta dicas em balões (texto, ícone de serviço ou QR de um link) e guarda "cartas na manga": regras que disparam quando alguém volta a um assunto |
| | `summary` | no fim da reunião, publica o resumo na tela separada (`ui/summary`) e avisa num balão |

Sem habilidade para o `run`, o agente genérico responde com a persona e escolhe as
ações por conta própria (falar, reagir, olhar, vibrar, LEDs, armar regras e ler
sensores).

## Testar e instalar

```sh
python3 tools/oba.py validate obas/bit      # formato, arquivos, sha256, PNG, WAV e limites
python3 tools/oba.py fmt obas/bit/oba.json  # reescreve com a formatação padrão
```

O `validate` confere tudo o que a placa confere antes de aceitar um Oba: o schema, o
nome da pasta igual ao `id`, cada arquivo de `files` (existe, sha, tamanho), as folhas
(PNG sem entrelaçamento, alpha só 0 ou 255, altura `h`, largura múltipla de `w`, até 32
quadros, até 1 MB decodificadas), os sons (formato, duração, total) e os limites da
instalação. Passou no `validate`, a placa aceita.

### Pelo ar

Com a placa ligada e na nuvem (o `setup.py` já feito), a `tools/oba.py` instala e
gerencia os Obas pelo MQTT, sem tirar o cartão:

```sh
python3 tools/oba.py install obas/bit --activate   # manda, pede confirmação na tela e ativa
python3 tools/oba.py list                          # Obas da placa (--registry: e os do registro S3)
python3 tools/oba.py activate nimbo                # troca o Oba ativo
python3 tools/oba.py play yay                      # toca um som do Oba ativo (--volume 1..255)
python3 tools/oba.py remove bit                    # apaga do cartão (--registry: e do registro S3)
python3 tools/oba.py publish obas/bit              # só sobe para o registro S3
```

O `install` valida a pasta antes (e para se tiver erro), manda o `oba.json` e os
arquivos de `files` em pedaços, e a placa confere tamanho e sha de cada um. Aí a tela
pergunta "Instalar Bit?" (ou "Atualizar Bit?", com a versão antiga e a nova), mostra
o que o Oba pede e o tamanho, e espera o toque em **Instalar** ou **Recusar** (60 s
sem resposta é recusa). Só depois de aceito o Oba vai para `/obas/<id>/` e para o
registro S3; recusado ou com erro, nada muda. Com o REC ligado, o `--activate` só
instala. `--device` escolhe outra placa, e `--no-registry` não mexe no registro.

Os detalhes da mensagem (`oba.install`, `oba.chunk`, `play`) e a policy IAM mínima
para usar esses comandos estão no [protocol.md](protocol.md#instalar-pelo-ar-obainstall-e-obachunk).

### Pelo cartão ou pela serial

```sh
python3 tools/monitor.py                   # e, no monitor: u obas/bit
```

O `u <pasta>` do monitor grava a pasta no cartão pela serial, sem tirar o cartão.
Outro jeito é copiar a pasta (o `oba.json` e os arquivos de `files`) para
`/obas/<id>/` num leitor de cartão. Depois, é só escolher o Oba no botão do meio da
placa (com o REC desligado).

Para o harness achar o Oba, ele precisa estar no registro S3. O `install` e o
`publish` sobem a pasta, e o `setup.py` envia cada pasta de `obas/` (e de
`obas.private/`, se houver) com os arquivos de `files`. O roteador confere se o Oba da
placa (versão e sha256 do `oba.json`) é o mesmo do registro antes de acordar o agente.
Se não bater, o agente não é chamado: suba a versão e publique de novo.

Para mudar o Oba embutido, gere o `src/builtin_oba.h` de novo com
`python3 tools/oba.py embed obas/nimbo/oba.json` e grave o firmware. O embutido
precisa ser rig, sem `sounds` nem `files` (o firmware só leva o JSON).

## Limites

| | |
|---|---|
| `oba.json` | até 64 KB |
| `outline` | até 256 pontos |
| `reflexes` | até 32 |
| `wake_words` | até 8, com até 24 caracteres cada |
| `name` | até 40 caracteres |
| `look.size` | de 8 a 96 pixels de lado; na tela (× `scale`), até 240 × 200 |
| folhas | até 32 quadros cada; todas juntas, decodificadas (largura × altura × 3), até 1 MB |
| `sounds` | até 16; cada um até 10 s; todos juntos até 512 KB de PCM |
| `files` | até 64 arquivos contando o `oba.json`; cada um até 2 MB; tudo junto até 6 MB |
