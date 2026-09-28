"""Habilidades prontas do agente genérico (agent.abilities no JSON do Oba).

Cada habilidade define os runs que sabe fazer. Um run que nenhuma habilidade
do Oba conhece cai no generic.
"""

from . import meeting
from .generic import generic

ABILITIES = {"meeting": meeting.RUNS}


def find(abilities, run: str):
    for name in abilities if isinstance(abilities, list) else []:
        fn = ABILITIES.get(name, {}).get(run)
        if fn:
            return fn
    return generic
