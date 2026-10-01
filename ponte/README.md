# Ponte do Claude Code

Mostra no Oba o que o Claude Code está fazendo (`idle`, `busy`, `alert`) e leva os
pedidos de permissão para a placa. Você aprova ou nega no Oba, e o terminal continua
valendo. Só a biblioteca padrão do Python 3.9 ou mais novo; funciona também com o
Claude Code no Bedrock.

```bash
python3 setup.py --ponte mac                        # na máquina com a AWS: build/ponte-mac/
python3 ponte/install.py build/ponte-mac/ --check   # na máquina do Claude Code
claude plugin marketplace add "$PWD/ponte"
claude plugin install oba-ponte@oba-pocket --scope user
```

Esta pasta é o marketplace (`oba-pocket`), e `plugin/` é o plugin (`oba-ponte`). O guia
completo está em [`docs/ponte.md`](../docs/ponte.md), e o contrato com a placa em
[`docs/protocol.md`](../docs/protocol.md#fontes-externas-ext).
