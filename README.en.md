# Oba Pocket

**English** · [Português](README.md)

<p align="center"><img src="docs/img/nimbo.gif" width="320" alt="Nimbo, a little cloud, looking around, happy, dizzy, sleepy, shy and talking in a speech bubble"></p>

Obas are AI agents with a body. The body lives on an M5Stack Core2: an animated face,
a speech bubble, touch, sensors, LEDs and vibration. The brain is the harness, which
runs outside the device. The two talk over MQTT, using the [Oba Protocol v1](docs/protocol.md).

Each Oba is a folder with an `oba.json` that says what it looks like, how it reacts on
its own and which agent is its brain ([how to make an Oba](docs/oba.md)). The microSD
card is the Oba library. Without a card, the device uses the built-in Oba, Nimbo
(`obas/nimbo`).

The code comments, the docs in `docs/` and the device UI are in Brazilian Portuguese.

## What it does

- **Reacts instantly, no cloud needed:** touches, shakes, taps and loud noises turn
  into moods, sounds and LEDs, following the Oba's reflex table.
- **Follows a meeting:** with REC on, audio goes straight from the device to Amazon
  Transcribe. The agent reads the captions and answers with a bubble right away or with
  an "ace up its sleeve": a rule that fires on the device when someone brings the topic
  back up. When REC turns off, the summary shows up on the separate display.
- **Answers to its name:** with REC on, saying the Oba's name makes it react right
  away and publishes a `wake` event. The agent only wakes up on it if the Oba has
  `{"on": "wake"}` in `agent.triggers`.
- **Swaps Obas over the air:** `tools/oba.py install` sends an Oba over MQTT; the device
  checks each file's sha256 and asks on screen before installing.
- **Two kinds of body:** vector (rig), like Nimbo, or PNG pixel art with WAV sounds,
  like Bit (`obas/bit`).

<p align="center"><img src="docs/img/bit.png" width="480" alt="Bit, a pixel art robot, idle, happy, dizzy and sleepy"></p>

## Architecture

```
 device ──audio (REC only)──▶ Amazon Transcribe
   │ ▲
   │ └── cmd ◀── router (Lambda) ◀──▶ agent (AgentCore)
   │                ▲
   │                └────────────────────────────────────────────────┐
   └── state, evt, transcript, reply ──▶ AWS IoT Core ──▶ IoT Rule ──┘
                                                 └──▶ display/ (separate screen, read-only)
```

- **Device** (`src/`): keeps the active Oba in PSRAM, draws at 30 frames per second,
  runs the reflexes and publishes high-level events (`touch.tap`, `imu.shake`, `wake`,
  captions). It never sends the raw sensor stream.
- **Router** (`harness/router/`): applies the active Oba's triggers (batching, debounce,
  cooldown), keeps the session in DynamoDB, fetches the `oba.json` from the S3 registry
  and publishes the agent's actions.
- **Agent** (`harness/agent/`): a generic [Strands](https://strandsagents.com) agent on
  Amazon Bedrock AgentCore, built from the `agent` block of the `oba.json` (persona,
  model, abilities, MCP servers from an allow list). Any agent that follows the
  [contract](docs/protocol.md#contrato-roteador--agente) can be an Oba's brain.
- **Separate display** (`display/`): a web page with the captions, the cards and the
  summary. It can only read the device's topics.

Nothing in the protocol depends on AWS: MQTT is isolated in `src/cloud.cpp` and the
harness follows a simple contract.

## Requirements

- An M5Stack Core2 for AWS (EduKit). A microSD card is optional: if it isn't FAT32,
  the device offers to format it (hold to confirm).
- An AWS account with an AWS CLI profile that can create the resources, and Bedrock
  access to the model in `config.json`.
- [PlatformIO Core](https://platformio.org), Python 3.10 or newer, `openssl`, and
  [uv](https://docs.astral.sh/uv/) (or pip) to package the harness.

## Getting started

```sh
cp config.example.json config.json            # prefix, device, regions, language, models
python3 -m venv .venv && . .venv/bin/activate
pip install boto3 jsonschema paho-mqtt pyserial pillow
python3 setup.py                              # AWS: device, harness and display, least privilege
# fill in WIFI_SSID and WIFI_PASS in src/secrets.h (setup tells you if they're missing)

pio run -e core2foraws -t upload              # firmware
cd display && python3 -m http.server 8080     # separate display: http://localhost:8080
```

`setup.py` uses the default AWS CLI profile (or `AWS_PROFILE`) and is safe to run again
as many times as you like: it only changes what changed. It creates the thing and its
certificate, the policies, the Transcribe role alias, the custom vocabulary, the S3
registry, the table, the Lambda, the router's IoT Rule, the AgentCore runtime and the display's
Cognito pool. It also writes `src/aws_config.h`, `display/config.js` and
`src/secrets.h`. The device's private key is created on your machine and never goes to
AWS.

On the device:

- **REC** (top left corner): turns transcription on and off. Only the button turns the
  microphone on; the agent can turn it off, never on.
- **Middle button:** opens the Oba picker (with REC off). Tap to choose, hold to remove.
- **Touch the Oba:** petting. Shaking, tapping and loud noises count too.

## Making an Oba

```sh
mkdir -p obas/my-oba && $EDITOR obas/my-oba/oba.json
python3 tools/oba.py validate obas/my-oba              # checks what the device checks
python3 tools/oba.py install obas/my-oba --activate    # sends it over the air and activates it
```

The [guide](docs/oba.md) covers every field: look (rig or sprites), moods, reflexes,
sounds, wake words, requested capabilities and the brain (`agent`). The format is in
[`schema/oba.schema.json`](schema/oba.schema.json). Nimbo and Bit are working examples;
`obas/bit/draw.py` generates Bit's frames and sounds.

Obas you don't want to publish can live in `obas.private/`: git ignores that folder,
and `setup.py` uploads the Obas in it to the registry too.

## Protocol

The [Oba Protocol v1](docs/protocol.md) uses six topics under `<prefix>/<device>/`:
`state` (retained), `evt`, `transcript` and `reply` from the device, and `cmd` and
`ui/<channel>` from the harness. The document covers the envelope, every event and
command (`speak`, `arm`, `react`, `look`, `vibrate`, `leds`, `play`, `read`,
`oba.install`…), the triggers, the session, the router → agent contract and the
minimal IAM policy for the CLI.

## Folders

| Folder | |
|---|---|
| `src/` | firmware (PlatformIO, Arduino) |
| `harness/router/` | router: Oba triggers, session, armed rules |
| `harness/agent/` | generic agent (Strands) and its abilities (`abilities/`) |
| `obas/` | public Obas: Nimbo and Bit |
| `schema/` | `oba.json` format |
| `display/` | separate display in the browser |
| `docs/` | protocol and Oba guide |
| `tools/` | serial monitor, Oba validation and install, fonts and icons |

## Tools

- `tools/oba.py validate <folder>`: checks an Oba against the schema, the sha256
  hashes and the limits.
- `tools/oba.py install <folder> [--activate]`: installs an Oba on the device over the
  air (MQTT) and, after you tap "Instalar" on its screen, uploads it to the S3 registry
  (`--no-registry` leaves the registry alone). The other cloud commands:
  `remove <id> [--registry]`, `activate <id>`, `play <sound> [--volume N]`,
  `list [--registry]` and `publish <folder>` (registry only). They all use the device
  from `config.json` (`--device` picks another one) and the AWS CLI profile, with the
  [minimal policy](docs/protocol.md#policy-iam-mínima-da-cli).
- `tools/monitor.py`: serial log, screenshots (`s`), REC (`r`), simulated speech
  (`t <phrase>`) and Obas over serial (`u <folder>`, `a <id>`, `l`, `o`). It finds the
  device on USB by itself.
- `tools/svg_to_outline.py`: turns an SVG into the outline of a rig Oba.
- `tools/make_font.py`: builds the bubble font from Nunito.
- `tools/fetch_icons.py`: downloads the AWS icons for the router (`setup.py` calls it
  when they're missing; they're not in the repo).

## Secrets

`src/secrets.h` (WiFi, the device's certificate and key), `src/aws_config.h`,
`display/config.js` and `config.json` are generated or filled in by you and stay out
of git. Nothing in the repo depends on a specific account.

## License

The code is under the [MIT license](LICENSE). The Nunito font (`tools/fonts/` and the
generated font in `src/bubble_fonts.cpp`) is under the
[SIL Open Font License 1.1](tools/fonts/Nunito-OFL.txt). The AWS icons aren't in the
repo: `tools/fetch_icons.py` downloads the official package, which has its own terms.
