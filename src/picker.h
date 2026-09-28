// Tela de escolha de Obas: abre pelo botão do meio do Core2, com o REC desligado
#pragma once
#include <M5GFX.h>

// Mostra os Obas instalados, um por vez e já animados. As setas (ou arrastar
// para o lado) trocam, tocar no Oba ativa, segurar remove (com confirmação) e
// o botão do meio fecha sem mudar nada. Bloqueia até fechar; devolve true se
// o Oba ativo mudou.
bool pickerRun(M5Canvas& c);
