# Oba Protocol v1

Como a placa (o corpo do Oba) conversa com o harness (o cérebro, fora da placa).
O transporte é MQTT. Na v1 ele roda no AWS IoT Core, com mTLS e o certificado da
placa. Nada no protocolo depende da AWS; o que o código de hoje teria que mudar para
rodar sem ela está em [Rodar local](../README.md#rodar-local).

```
 placa ──state (retido)──▶                         ◀──cmd── harness
       ──evt────────────▶  <p>/<dev>/...  broker
       ──transcript─────▶                         ──ui/<canal>──▶ portal
       ──reply──────────▶
```

`<p>` é o prefixo (`prefix` no `config.json`, padrão `obapocket`) e `<dev>` é o nome
da placa (o thing do IoT, que também é o client ID do MQTT).

## Envelope

Toda mensagem é um objeto JSON em UTF-8 com:

| Campo | | |
|---|---|---|
| `v` | sempre | versão do protocolo: `1` |
| `type` | sempre | o que é a mensagem |
| `ts` | sempre | hora de quem mandou, em ms desde 1970 (UTC) |
| `oba` | da placa | id do Oba ativo quando a mensagem saiu |
| `id` | quando precisa | id do comando, da regra ou da frase |

Campos desconhecidos são ignorados. Isso permite acrescentar campos sem mudar a versão.

## Tópicos

| Tópico | Quem publica | Retido | Conteúdo |
|---|---|---|---|
| `<p>/<dev>/state` | placa | sim | como a placa está agora |
| `<p>/<dev>/evt` | placa | | eventos semânticos, em baixa taxa |
| `<p>/<dev>/transcript` | placa | | legendas, só com o REC ligado |
| `<p>/<dev>/reply` | placa | | respostas de `read` e `oba.*`, e recusas |
| `<p>/<dev>/cmd` | harness | | comandos para a placa |
| `<p>/<dev>/ui/<canal>` | harness | depende | para o portal (não vão para a placa) |
| `<p>/<dev>/ext/<fonte>` | fonte externa | | estado e pedidos de aprovação de outro programa ([abaixo](#fontes-externas-ext)) |
| `<p>/<dev>/ext/<fonte>/re` | placa | | as respostas da placa para essa fonte |

A placa assina `cmd` e `ext/+`. A policy dela só deixa publicar nos quatro primeiros
tópicos e em `ext/*/re`, e só na pasta com o próprio nome.

## Placa → harness

### `state` (retido)

Publicado quando a placa conecta, quando algo nele muda (Oba ativo, Obas instalados,
REC, carregando ou não, bateria a cada 5%) e quando chega o comando `state`.

```json
{"v": 1, "type": "state", "ts": 1790000000000, "oba": "bit", "online": true, "fw": "0.5.1",
 "active": {"id": "bit", "name": "Bit", "version": "1.0.0", "sha256": "<hex>"},
 "obas": [{"id": "nimbo", "name": "Nimbo", "version": "1.0.0", "builtin": true},
          {"id": "bit", "name": "Bit", "version": "1.0.0"}],
 "caps": ["display", "touch", "buttons", "imu", "mic.level", "mic.transcribe", "leds", "vibration", "battery",
          "ext", "speaker", "sd"],
 "rec": false, "battery": {"pct": 87, "charging": true},
 "ext": [{"src": "obapocket-ponte-mac", "mood": "busy", "asks": 1}]}
```

`ext` (desde o 0.4.0) lista as [fontes externas](#fontes-externas-ext) que a placa conhece
agora, com o humor de cada uma e quantos pedidos dela estão na fila. Sem fontes, a chave
não vem. O `state` sai de novo quando uma fonte entra ou sai, muda de humor ou muda o
número de pedidos. O rótulo e o conteúdo dos pedidos não entram no `state`.

`active.sha256` (desde o firmware 0.3.0) é o sha256, em hex minúsculo, dos bytes do `oba.json`
como está no cartão. No Oba embutido, é o do JSON minificado. O roteador usa para conferir
o Oba da placa com o registro (abaixo).

A mensagem de última vontade do MQTT, também retida, é
`{"v": 1, "type": "state", "online": false}`. O broker publica ela quando a placa cai.

### `evt`

Fatos de alto nível, nunca o fluxo cru dos sensores. Para não gastar rede nem
acordar o agente à toa, a placa só publica os eventos que o Oba ativo pede em
`agent.triggers[].on`. `wake` e `rule.fired` sempre saem. Cada tipo tem um
intervalo mínimo entre dois envios.

| type | Campos | Intervalo | Quando |
|---|---|---|---|
| `touch.tap` | `x, y` (pixels da tela, 320×240) | 2 s | toque rápido no Oba |
| `button.a`, `button.c` | | 1 s | botões virtuais da esquerda e da direita (o do meio abre a tela de escolha) |
| `sound.loud` | | 5 s | barulho bem acima do ambiente |
| `imu.shake` | | 5 s | chacoalhou a placa |
| `imu.tap` | `dir` (-1 esquerda, 1 direita) | 2 s | teco de lado na placa |
| `wake` | `word, text, partial` | por frase | ouviu o nome do Oba (só com REC) |
| `rule.fired` | `id, on, trigger?, text?` | | uma regra armada disparou |
| `bubble.done` | `id` | | o balão com esse id fechou |

### `transcript`

`{"v": 1, "type": "transcript", "oba", "id", "partial", "text", "ts"}`

O `id` identifica a frase: o Transcribe reenvia a mesma frase, cada vez mais completa,
até fechar com `partial: false`. As parciais saem no máximo 4 vezes por segundo, só
para as legendas da tela. O harness usa só as finais.

### `reply`

`{"v": 1, "type": "reply", "oba", "ts", "id", "re": "<type do comando>", "ok": true, "error"?, "values"?,
"stage"?, "next"?, "active"?}`

Só sai para comandos com `id`:

- `read`, `oba.activate`, `oba.remove`, `oba.install` e `oba.chunk` sempre respondem
  (`stage`, `next` e `active` são da instalação, abaixo);
- os outros só respondem quando a placa recusa (`ok: false`, com o motivo em `error`).
  Um `speak` ou um `arm` aceito não gera resposta: o `bubble.done` e o `rule.fired`
  contam o que aconteceu.

## Harness → placa: `cmd`

A placa não precisa de nada além do que está aqui. `card` (em `speak` e `arm`) é só
para as telas: a placa ignora esse campo.

| type | Campos | Efeito |
|---|---|---|
| `speak` | `id?, bubble, card?` | mostra o balão (entra na fila se já tiver um na tela) |
| `arm` | `id, on?, words?, ttl_s?, do, card?` | arma uma regra (abaixo) |
| `disarm` | `id` | tira a regra |
| `reset` | `session?` | tira todas as regras. `session` é a sessão nova; `null` = acabou |
| `react` | `do` | um humor (`idle`, `happy`, `scared`, `shy`, `dizzy`, `sleepy`), `glance` ou `turn`, como nos reflexos. `busy` e `alert` não: eles vêm das [fontes externas](#fontes-externas-ext) |
| `look` | `x, y, ms?` | olha para (x, y), de -1 a 1, por até 5 s (padrão 1,5 s) |
| `vibrate` | `ms?, level?` | vibra por até 2 s (padrão 200 ms), força de 1 a 255 (padrão 200) |
| `leds` | `fx, color?, trail?, min?, max?, speed?, ms?, hold_ms?` | troca o efeito dos LEDs por `hold_ms` (padrão 5 s, até 60 s). Os campos são os de `leds` nos humores do Oba |
| `play` | `id?, sound, volume?` | toca um som (`sounds` do Oba ativo), volume de 1 a 255 (padrão 128). Só com o REC desligado: com ele ligado, ou som desconhecido, recusa |
| `read` | `id, what` | lê sensores e responde em `reply` (abaixo) |
| `rec` | `on: false` | desliga o REC. Ligar não é aceito: só pelo botão na placa |
| `state` | | a placa republica o `state` |
| `oba.activate` | `id, target` | ativa o Oba `target` (só com o REC desligado) e responde |
| `oba.remove` | `id, target` | apaga o Oba `target` do cartão (nem o ativo nem o embutido) e responde |
| `oba.install` | `id, target, activate?, files` | começa a instalar o Oba `target` pelo ar (abaixo) |
| `oba.chunk` | `id, path, off, data` | um pedaço de um arquivo da instalação `id` |

Comandos que a placa não conhece, ou que ela recusa, são ignorados; se tiverem `id`,
ela responde com `ok: false`.

### `bubble`

```json
{"kind": "text" | "image" | "qr", "text": "Filas pra desacoplar? O Amazon SQS segura a onda!",
 "png": "<base64>", "url": "https://aws.amazon.com/sqs/"}
```

- `text`: a fala, em até 280 caracteres. Fala longa ganha páginas.
- `image`: um PNG de até 80×80 em `png`, com o texto de legenda.
- `qr`: um QR code de `url` (até 300 caracteres). A placa só desenha o QR, não abre o link.

O agente não manda `png`. Ele manda `icon` (o nome de um serviço), e o roteador troca
pelo ícone oficial antes de publicar. O roteador também descarta a `url` que não for
de um domínio permitido e, sem ela, o balão vira `text`.

### Regras armadas (`arm`)

Uma regra é "quando X acontecer, faça Y". Ela fica na placa e dispara na hora, sem
esperar a nuvem, o que fez as "cartas na manga" parecerem instantâneas.

```json
{"v": 1, "type": "arm", "id": "c1a2b3", "on": "speech", "words": ["fila", "mensageria", "sqs"],
 "ttl_s": 900, "do": [{"type": "speak", "bubble": {"kind": "image", "text": "…", "png": "…"}}],
 "card": {"kind": "service", "title": "Amazon SQS", "body": "…"}}
```

- `on`:
  - `speech` (padrão): quando uma das `words` aparece na transcrição, parcial ou final.
    Palavras inteiras, comparadas em minúsculas, sem acento e sem hífen ("e-mail" =
    "email"). Só é aceita com o REC ligado, e as regras de fala somem quando o REC muda;
  - ou um evento da placa: `touch.tap`, `button.a`, `button.c`, `sound.loud`,
    `imu.shake`, `imu.tap`.
- `do`: de 1 a 4 comandos entre `speak`, `react`, `look`, `vibrate`, `leds` e `play`.
- `ttl_s`: validade, até 1 dia (padrão 15 min).
- Cabem 8 regras. Uma nova com o mesmo `id` substitui a antiga; passando de 8, sai a
  mais antiga.
- As regras de fala disparam uma de cada vez: com a tela sem balão e pelo menos 6 s
  depois da anterior. A que casou espera até 20 s pela vez; depois desiste e volta a
  esperar as palavras.
- Ao disparar, a regra sai da placa e a placa publica `rule.fired` com o `id`.

### `read`

`what` é uma lista com qualquer um destes; sem `what`, vêm todos.

| Chave | Valor |
|---|---|
| `battery` | `{pct, mv, charging}` |
| `imu` | `{ax, ay, az}` em g e `{gx, gy, gz}` em °/s |
| `mic` | `{rms, base}`: nível atual e do ambiente |
| `mood` | humor atual, por exemplo `"sleepy"` |
| `rec` | `true`/`false` |
| `time` | `{epoch_ms, uptime_s}` |
| `oba` | `{id, name, version}` |
| `wifi` | `{rssi}` |

```json
{"v": 1, "type": "reply", "re": "read", "id": "r7", "ok": true, "values": {"battery": {"pct": 80, "mv": 4010, "charging": false}}}
```

### Instalar pelo ar (`oba.install` e `oba.chunk`)

O `oba.json` e os arquivos de `files` (sprites, sons) vão em pedaços pelo `cmd`. A placa
grava numa pasta temporária do cartão, confere tamanho e sha256 de cada arquivo, carrega o
Oba de teste e pergunta na tela se instala. `tools/oba.py install` e o instalador do portal
(`portal/api/installer.py`, que reusa o código da CLI) fazem tudo isso.

1. Cabeçalho, com o `oba.json` exatamente uma vez (a CLI põe por último):

   ```json
   {"v": 1, "type": "oba.install", "id": "i7f3a2c", "target": "bit", "activate": false,
    "files": [{"path": "sprites/idle.png", "size": 1234, "sha256": "<hex>"},
              {"path": "oba.json", "size": 4567, "sha256": "<hex>"}]}
   ```

   Aceito: `{"re": "oba.install", "id": "i7f3a2c", "ok": true, "stage": "receiving", "next": {"path": "sprites/idle.png", "off": 0}}`.
   Recusado (`ok: false`, `stage: "done"`, motivo em `error`): REC ligado, sem cartão, `target`
   ou lista inválida, acima dos limites, pouco espaço. Limites: até 64 arquivos contando o
   `oba.json`, cada um até 2 MB (o `oba.json` até 64 KB), 6 MB no total. Uma instalação nova
   cancela a que estiver em andamento.

2. Pedaços, na ordem da lista, com até 8192 bytes em `data` (base64):

   ```json
   {"v": 1, "type": "oba.chunk", "id": "i7f3a2c", "path": "sprites/idle.png", "off": 0, "data": "<base64>"}
   ```

   A placa só aceita o pedaço esperado (o `path` e o `off` do `next`); repetido ou fora de
   ordem é ignorado. Todo pedaço recebido gera um ack
   `{"re": "oba.chunk", "id": "i7f3a2c", "ok": true, "next": {"path": "…", "off": 8192}}`
   (`next: null` quando tudo chegou). Quem manda deixa até 4 pedaços em voo e, sem a placa
   andar por 5 s, reenvia a partir do último `next`. Id sem instalação:
   `ok: false`, `error: "nenhuma instalação com esse id"`.
   Erros que encerram a instalação vêm como `re: "oba.install"`, `ok: false`, `stage: "done"`:
   base64 inválido, pedaço passando do tamanho, erro no cartão, `"sha256 não bate: <path>"`,
   `"o REC ligou"`, `"tempo esgotado"` (30 s sem pedaço).

3. Com o último byte, a placa carrega o Oba de teste (o `id` do JSON tem que ser o `target` e
   os caminhos de `files` os da lista), responde `stage: "confirm"` e pergunta na tela
   ("Instalar Bit?" ou "Atualizar Bit?", versão, o que ele pede, tamanho). 60 s sem toque é
   recusa: `ok: false`, `stage: "done"`, `error: "recusado na placa"`.

4. Aceito, a pasta temporária vira `/obas/<id>/`. Se era o Oba ativo, ou com `activate: true`,
   o novo já entra ativo. A placa publica o `state` e responde
   `{"re": "oba.install", "id": "i7f3a2c", "ok": true, "stage": "done", "active": true}`.

A IoT Rule do roteador não passa os acks de pedaço (`reply` com `re = 'oba.chunk'`): só quem
instala precisa deles.

## Harness → portal: `ui/<canal>`

A placa não assina esses tópicos. O portal assina todos, e também `state`, `transcript`,
`evt`, `cmd` e `reply`: o navegador só lê, e os comandos dele passam pela API do portal.

| Canal | Retido | Conteúdo |
|---|---|---|
| `ui/oba` | sim | o Oba ativo para desenhar: `{id, name, version, palette, outline, eyes, wake_words, sounds}` (rig) ou `{id, name, version, palette, sprites, wake_words, sounds}` (sprites); `sounds` são os nomes que o `play` aceita |
| `ui/think` | | `{state: "start" \| "end", run, note?}`: o agente está pensando |
| `ui/summary` | sim | resumo da reunião: `{session, lines, title, summary, topics, decisions, next_steps, suggestions}` |
| `ui/install` | | andamento de uma instalação pelo portal (publicado pelo instalador, não pelo roteador): `{cid, id, stage, …}` |

No `ui/install`, `cid` é o `id` do `oba.install` e `id` é o Oba. `stage` vai de `start`
a `done` ou `error`:

| `stage` | Campos | |
|---|---|---|
| `start` | | o instalador pegou o pedido |
| `send` | `sent, total` (bytes) | enviando os pedaços, no máximo 2 por segundo |
| `confirm` | `sent, total, wait_s` | tudo chegou; a placa pergunta na tela por até `wait_s` segundos |
| `done` | `active, version` | instalado (`active: true` se virou o Oba ativo) |
| `error` | `error` | o motivo, em português (por exemplo `"recusado na placa"`) |

Nos Obas de sprites, `sprites` é `{"size": [w, h], "origin": [x, y], "scale": s, "fps": f, "sheet": "<base64>"}`:
o `look` do Oba e a folha `idle` (PNG, tira horizontal de quadros de w×h) em base64. A folha
só vai se tiver até 48 KB e o sha256 de `files` bater; senão `sprites` vem sem `sheet`. O portal
anima essa folha num canvas, com `image-rendering: pixelated`.

Os canais retidos de uma sessão são apagados quando começa outra (payload vazio).

No IoT Core, a mensagem retida só chega a quem assina o tópico exato. Quem assina
`<p>/<dev>/#` recebe as próximas mensagens, mas não a retida. Por isso o portal assina cada
tópico pelo nome, em dois SUBSCRIBE: o IoT Core aceita até 8 tópicos em cada um.

## Fontes externas (`ext`)

Outro programa pode mostrar no Oba o que está fazendo e pedir aprovação pela placa. O
primeiro é a [ponte do Claude Code](ponte.md): enquanto o Claude trabalha, o Oba fica
ocupado; quando ele pede permissão para uma ferramenta, a placa mostra o pedido e dá para
aprovar ou negar com um toque.

Cada fonte é um thing do IoT com um certificado próprio (`setup.py --ponte <nome>`). O
nome do thing é o client ID dela e também o `<fonte>` dos tópicos:

```
 fonte ──status, ask, ask.cancel, react…──▶  <p>/<dev>/ext/<fonte>     ──▶ placa
       ◀──reply───────────────────────────  <p>/<dev>/ext/<fonte>/re  ◀── placa
       ◀──state (retido)──────────────────  <p>/<dev>/state           ◀── placa
```

A placa aceita até 4 fontes ao mesmo tempo. Nada disso passa pelo roteador: a IoT Rule
dele só pega `<p>/+/+`.

### A placa não guarda nada retido

A placa assina `ext/+`, e no IoT Core a assinatura com curinga não recebe a mensagem
retida. Por isso nada em `ext` é retido. Para saber quando a placa (re)conectou, a fonte
assina o `state` pelo nome exato, que é retido, e confere o `ext` dele:

- a fonte não aparece em `state.ext`: manda o `status` de novo;
- `state.ext[].asks` é menor que o número de pedidos pendentes: manda os pedidos de novo.
  A placa não duplica um pedido com o mesmo `id`;
- `state.ext[].asks` é maior que zero: manda de novo os `ask.cancel` que ainda não
  venceram. A placa conecta com sessão limpa e pode ter perdido algum, contando um pedido
  velho no lugar de um novo. Ela ignora o cancel de um `id` que não conhece;
- `state.online` é `false` (a última vontade da placa): os pedidos pendentes voltam para
  onde vieram, e os novos nem saem até a placa voltar.

### Fonte → placa

| type | Campos | Efeito |
|---|---|---|
| `status` | `mood, label?, ttl_s?` | o estado da fonte. `mood`: `idle`, `busy` ou `alert` |
| `status` | `online: false` | a fonte saiu (é também a última vontade do MQTT dela) |
| `ask` | `id, tool?, title, body?, danger?, label?, ttl_s?` | pede aprovação (abaixo) |
| `ask.cancel` | `id` | o pedido foi respondido em outro lugar: sai da tela e da fila |
| `react` | `do` | como no `cmd`: `happy`, `scared`, `shy`, `dizzy`, `idle`, `sleepy`, `glance` ou `turn` |
| `vibrate`, `leds`, `play` | como no `cmd` | como no `cmd` |

O resto é ignorado. Todas levam o envelope (`v`, `type`, `ts`). Sem `ts` (como na última
vontade), a placa conta atraso zero. Com o relógio certo, ela descarta a mensagem que
chegar com mais de `ttl_s` (ou 60 s) de atraso e desconta o atraso do `ttl_s` das outras.
Por isso, ao mandar um pedido de novo, a fonte repete o `ts` de quando ele foi criado.

**`status`:** a fonte manda quando o estado muda e, sem mudança, a cada `ttl_s / 3`.
Sem `status` novo em `ttl_s` segundos (padrão 180, de 10 a 3600), a placa esquece a
fonte. `label` tem até 48 caracteres e aparece no alto da tela, por exemplo
`mac · oba-pocket · Bash`. Os limites de texto contam caracteres, não bytes. A placa
desenha ASCII, Latin-1 (os acentos) e a pontuação `– — ‘ ’ “ ” • …`; o resto vira `?`.

**O humor do Oba:** as fontes decidem o humor de repouso da placa. Vale o mais urgente
entre elas: `alert`, depois `busy`, depois `idle`. Os humores passageiros (um toque, um
`react: happy`) voltam para esse humor de repouso quando acabam, e não para o `idle`.
Quando o humor de repouso muda, o Oba acorda. Sem fontes, tudo funciona como antes.

**`ask`:**

```json
{"v": 1, "type": "ask", "ts": 1790000000000, "id": "k3v9x2", "tool": "Bash",
 "title": "Rodar comando", "body": "rm -rf build/", "danger": true,
 "label": "mac · oba-pocket", "ttl_s": 280}
```

| Campo | |
|---|---|
| `id` | de 1 a 32 caracteres, só `A-Z`, `a-z`, `0-9`, `_` e `-` |
| `tool` | o nome da ferramenta, até 40 caracteres |
| `title` | até 60 caracteres |
| `body` | o resumo do que vai acontecer, até 1500 caracteres. `\n` quebra linha |
| `danger` | `true`: aprovar exige segurar o botão por 1,5 s |
| `label` | de onde veio, até 48 caracteres |
| `ttl_s` | depois disso o pedido vence (padrão 120, de 10 a 600) |

A placa enfileira até 4 pedidos, de todas as fontes juntas. Com a fila cheia, responde
`skip` com `error: "fila cheia"`. O pedido aparece quando não tem balão na tela e fica
até alguém responder, até o `ask.cancel` ou até vencer o `ttl_s`. Ao vencer, a placa
responde `skip` com `error: "venceu"`.

Na tela, o Oba vai para o canto e o pedido ocupa o resto. Embaixo ficam dois botões de
toque, **Negar** e **Aprovar**. O botão virtual do meio da placa, **Terminal**, tira o
pedido da placa sem responder (`skip`). Se o texto tiver mais de uma página, o toque no
pedido vira a página, e o **Aprovar** só acende depois da última. A placa vibra quando
um pedido aparece.

### Placa → fonte: `ext/<fonte>/re`

```json
{"v": 1, "type": "reply", "ts": 1790000000000, "oba": "bit", "re": "ask", "id": "k3v9x2",
 "choice": "allow"}
```

`choice` é `allow`, `deny` ou `skip`. `skip` quer dizer "responda em outro lugar"; ele
pode vir com `error`:

| `error` | |
|---|---|
| (nenhum) | alguém tocou em **Terminal** |
| `venceu` | passou o `ttl_s` sem resposta |
| `fila cheia` | já tinha 4 pedidos |
| `fontes demais` | o pedido veio de uma 5ª fonte |
| `falta o title` | o pedido não tinha `title` |

A placa lembra as últimas 16 respostas, inclusive as que vieram de um toque em
**Terminal** e as vencidas: se o mesmo `id` chegar de novo, ela repete a resposta em vez de
perguntar outra vez. As três últimas recusas da tabela ficam fora dessa lista, e o mesmo
`id` pode tentar de novo depois.

Para testar sem fonte, pela serial: `X<fonte> <json>` finge uma mensagem em
`ext/<fonte>`, e `Y`/`N` aprovam ou negam o pedido que está na tela. Esses comandos só
valem no firmware dev (`pio run -e dev -t upload`).

### Policy da fonte

Com `${iot:Connection.Thing.ThingName}` no lugar do nome da fonte:

- `iot:Connect` só como `client/<fonte>`;
- `iot:Publish` só em `<p>/<dev>/ext/<fonte>`;
- `iot:Subscribe` e `iot:Receive` só em `<p>/<dev>/ext/<fonte>/re` e `<p>/<dev>/state`.

A fonte não publica em `cmd` nem lê `evt`, `transcript` ou `reply`. Ela também não
consegue se passar por outra fonte: o tópico sai do certificado. O portal não lê `ext`,
porque os pedidos podem ter comandos e caminhos de arquivo. Ele só vê o `state.ext`.

## O harness

O harness tem duas partes:

- **Roteador:** recebe o que a placa publica, decide quando acordar o agente e publica
  as ações que ele devolver.
- **Agente:** recebe o contexto e devolve ações.

Na AWS, o roteador é uma Lambda chamada por uma IoT Rule, e o agente roda no AgentCore
Runtime. Os dois são genéricos: o comportamento de cada Oba vem do JSON dele (`agent`),
guardado no registro (S3).

### O Oba da placa e o registro

Antes de acordar o agente, o roteador confere o `active` do último `state` com o
`obas/<id>/oba.json` do registro: a `version` tem que ser a mesma (JSON sem `version` vale
`"0"`, como na placa) e o `sha256` tem que ser o dos bytes do objeto no S3 ou o do JSON
minificado (`json.dumps(obj, separators=(",", ":"), ensure_ascii=False)`, o do Oba embutido). Não bateu
com o que está no cache: relê do S3 uma vez. Continuou não batendo: loga "o Oba da placa não
bate com o registro" e não chama o agente. `state` sem `sha256` (firmware antes do 0.3.0) não
é conferido.

O `ui/oba` também sai de novo quando o `sha256` muda. Se o `state` não bateu com o registro
(a placa terminou de instalar antes de a CLI subir o Oba), ele sai de novo no próximo `state`:
por isso a CLI pede um `state` depois de subir.

### Eventos do roteador

Além dos eventos da placa, o roteador cria os seus:

| Evento | Quando |
|---|---|
| `transcript.final` | frase final da transcrição |
| `rec.on`, `rec.off` | o REC mudou no `state` (começa ou termina a sessão) |
| `oba.changed` | o Oba ativo mudou no `state` |
| `reply` | chegou a resposta de um `read` pedido pelo agente (continuação) |

### Gatilhos (`agent.triggers` no JSON do Oba)

```json
"triggers": [
  {"on": "transcript.final", "batch": 2, "cooldown_ms": 8000},
  {"on": "rec.off", "run": "summary"},
  {"on": "touch.tap", "cooldown_ms": 30000, "run": "chat"}
]
```

| Campo | Padrão | |
|---|---|---|
| `on` | | evento da placa ou do roteador |
| `batch` | 1 | quantos eventos juntar antes de acordar o agente |
| `debounce_ms` | 0 | espera esse silêncio (sem outro evento igual) antes de acordar, até 10 s |
| `cooldown_ms` | 0 | intervalo mínimo entre duas rodadas desse gatilho |
| `run` | `think` | o que pedir ao agente (as habilidades definem os nomes) |

Cada gatilho tem uma rodada por vez por placa. Se chegarem eventos durante uma
rodada, eles são tratados logo depois dela, na mesma invocação. O resultado de uma
rodada é descartado se o REC mudou enquanto o agente pensava.

### Sessão

Cada `rec.on` abre uma sessão nova, com id `AAAAMMDD-HHMMSS-xxxxxx`. A sessão guarda:

- os eventos, entre eles as frases finais, que formam o `history`;
- as regras armadas e o que já foi mostrado;
- quem está pensando.

A memória do Oba (`memory`) é separada da sessão: fica por placa e por Oba, e passa
de uma sessão para a outra.

### Contrato roteador → agente

Requisição (JSON):

```json
{
  "v": 1, "run": "think",
  "oba": {"id": "nimbo", "name": "Nimbo", "version": "1.0.0", "persona": "…", "model": null,
          "abilities": ["meeting"], "mcp": ["aws-knowledge"], "sounds": []},
  "device": {"id": "oba-01", "state": {"rec": true, "battery": {"pct": 80}}},
  "session": "20260927-120000-a1b2c3",
  "trigger": {"on": "transcript.final", "batch": 2},
  "events": [{"type": "transcript.final", "text": "…", "ts": 0}],
  "history": [{"type": "transcript.final", "text": "…", "ts": 0}],
  "new_from": 12,
  "armed": [{"id": "c1a2b3", "on": "speech", "words": ["fila"], "title": "Amazon SQS"}],
  "shown": [{"kind": "blog", "title": "…", "url": "…"}],
  "memory": {}
}
```

Resposta:

```json
{"actions": [{"type": "speak", "bubble": {"kind": "image", "text": "…", "icon": "Amazon SQS"}, "card": {}},
             {"type": "arm", "on": "speech", "words": ["dlq"], "do": [{"type": "speak", "bubble": {}}], "card": {}},
             {"type": "disarm", "id": "c1a2b3"},
             {"type": "ui", "channel": "summary", "retain": true, "data": {}}],
 "memory": {}, "note": "falando de filas e picos de tráfego"}
```

Antes de publicar, o roteador confere cada ação:

- só passam os tipos da tabela de comandos, mais `ui`;
- `rec` só com `on: false`; `reset` e `oba.*` nunca vêm do agente;
- `play` com um nome de som válido e o volume entre 1 e 255 (`oba.sounds` da requisição
  traz os nomes dos sons do Oba);
- ícone pelo nome, `url` só de domínio permitido e textos cortados no tamanho máximo;
- `id` para as regras que não têm e no máximo 6 regras armadas: passando disso,
  desarma a mais antiga.

Qualquer agente que cumpra esse contrato pode ser o cérebro de um Oba. O
`agent.endpoint` do JSON escolhe qual, pelo nome, no catálogo do harness
(`agents` no `config.json`). O roteador não chama um endpoint que esteja fora do
catálogo.

Num endpoint `http`, o roteador manda o JSON num POST sem autenticação nenhuma. A URL
precisa ser `https`; `http` só vale em `localhost`, `127.0.0.1` ou `::1`, e o `setup.py`
e o roteador recusam o resto. Use só para desenvolvimento: quem tiver a URL chama o
agente.

## Segurança

- O microfone só liga pelo botão REC na placa. O harness pode desligar, nunca ligar.
- A placa publica só na própria pasta (`<p>/<dev>/`) e só assina `cmd` e `ext/+`, pela
  variável `${iot:Connection.Thing.ThingName}` na policy.
- Aprovar um pedido de uma fonte externa só vale pelo toque na placa. O portal e o
  harness não aprovam nada, e cada fonte só publica na própria pasta de `ext`. Pela
  serial, só o firmware dev aprova (`Y`/`N`).
- Com o REC ligado, o áudio vai direto da placa para o Amazon Transcribe. As
  credenciais são temporárias e vêm do certificado, por um role alias que só abre
  streams do Transcribe.
- O portal exige login (usuário e senha no Cognito). O navegador recebe credenciais
  temporárias que só leem os tópicos daquela placa, e a policy do IoT é anexada pela API,
  não pelo navegador.
- Os comandos do portal passam pela API, que confere o token e só aceita os da tela da
  placa: `oba.activate`, `oba.remove`, `play`, `state` e `rec` com `on: false`. Instalar vai
  pelo instalador do portal, com os mesmos passos da CLI. Os pedaços do `oba.install`
  passam pelo `cmd`, que o navegador também lê. A API também envia Obas para o registro
  (`POST /api/obas`, em zip) e tira de lá (`DELETE /api/obas/<id>`): quem entra no
  portal mexe no registro.
- Comandos com limites na placa: vibrar até 2 s, LEDs até 60 s, olhar até 5 s,
  8 regras, e balão de até 280 caracteres com PNG de até 12 KB.
- Instalar um Oba pelo ar sempre pede o toque em "Instalar" na tela da placa, e a placa
  confere o sha256 de cada arquivo antes.

### Policy IAM mínima da CLI

`tools/oba.py install | remove | activate | play | list | publish` usa o perfil da AWS CLI
e fala com o IoT Core por WebSocket assinado (SigV4), sem certificado. O mínimo, trocando
`<região>`, `<conta>`, `<p>` e `<dev>`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "Descobrir", "Effect": "Allow", "Action": ["iot:DescribeEndpoint", "sts:GetCallerIdentity"],
     "Resource": "*"},
    {"Sid": "Conectar", "Effect": "Allow", "Action": "iot:Connect",
     "Resource": "arn:aws:iot:<região>:<conta>:client/oba-cli-*"},
    {"Sid": "Comandos", "Effect": "Allow", "Action": "iot:Publish",
     "Resource": "arn:aws:iot:<região>:<conta>:topic/<p>/<dev>/cmd"},
    {"Sid": "Respostas", "Effect": "Allow", "Action": "iot:Subscribe",
     "Resource": "arn:aws:iot:<região>:<conta>:topicfilter/<p>/<dev>/reply"},
    {"Sid": "RespostasChegam", "Effect": "Allow", "Action": "iot:Receive",
     "Resource": "arn:aws:iot:<região>:<conta>:topic/<p>/<dev>/reply"},
    {"Sid": "StateRetido", "Effect": "Allow", "Action": "iot:GetRetainedMessage",
     "Resource": "arn:aws:iot:<região>:<conta>:topic/<p>/<dev>/state"},
    {"Sid": "Registro", "Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject"],
     "Resource": "arn:aws:s3:::<p>-<conta>/obas/*"},
    {"Sid": "RegistroLista", "Effect": "Allow", "Action": "s3:ListBucket",
     "Resource": "arn:aws:s3:::<p>-<conta>", "Condition": {"StringLike": {"s3:prefix": "obas/*"}}}
  ]
}
```

`list --registry` também lê os `oba.json` do registro: acrescente `s3:GetObject` em
`arn:aws:s3:::<p>-<conta>/obas/*`. Sem os dois últimos blocos, só `install --no-registry`,
`remove`, `activate`, `play` e `list` funcionam. Para mais de uma placa, repita os tópicos
com cada `<dev>`.
