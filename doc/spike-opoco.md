# Spike OPOCO — resultados (Fase 0)

> Execução no **Spot Testnet** em 28/09/2026 (`uv run python scripts/spike_opoco.py`), símbolo BTCUSDT, chave HMAC, offset de relógio de ~0,2 s. Log completo em `var/spike/spike-testnet-*.json` (não versionado).
> **Resultado: 19/19 verificações OK.** Critério de saída da Fase 0 atendido no Testnet.

## 1. O que foi verificado

| Cenário | Verificação | Resultado |
|---------|-------------|-----------|
| 0 | Flags `ocoAllowed`, `otoAllowed`, `opoAllowed`, `allowTrailingStop` | ✅ todas ativas; `TRAILING_DELTA` de 10 a 2000 bips (acima e abaixo) |
| A | OPOCO: compra LIMIT FOK + TAKE_PROFIT (ativação por `stopPrice` + `trailingDelta` 100) / STOP_LOSS fixo | ✅ aceito; compra FILLED; TP e SL armados (NEW); `trailingDelta` = 100 |
| A | Quantidade pendente = quantidade recebida (semântica OPO) | ✅ 0.00019 = 0.00019 |
| A | Reenvio do mesmo `listClientOrderId` com a lista aberta | ✅ rejeitado (`-2010 Duplicate order sent.`) |
| B | OPOCO com LIMIT_MAKER acima e STOP_LOSS **só com `trailingDelta`** (300) | ✅ aceito e armado |
| C | OPOCO com compra FOK não executável | ✅ entrada EXPIRED, lista ALL_DONE, pernas não armadas |
| C | Reuso do `listClientOrderId` depois da lista encerrada | ⚠️ **aceito** (ver §2) |
| D | Compra a mercado + OCO avulso (`orderList/oco`) com trailing TP; consulta e cancelamento | ✅ |
| WS | User Data Stream pela WebSocket API (`userDataStream.subscribe.signature`) | ✅ `executionReport`, `listStatus`, `outboundAccountPosition` |

## 2. Achados e consequências

1. **Pernas pendentes sem `origQty` na resposta do envio.** Na resposta de `orderList/opoco`, o TP e o SL vêm `PENDING_NEW` e **sem** `origQty`: a quantidade só é definida quando a entrada executa. O modelo `Order` exigia o campo, e o envio falhava com `ValidationError` **depois de aceito pela Binance**. O reconciliador adotaria a posição depois, mas o ciclo de decisão falharia a cada entrada. **Corrigido:** `origQty` ausente vale 0 nas pernas pendentes. A Binance simulada passou a reproduzir o formato real, e um teste de integração cobre a regressão.
2. **As pernas continuam `PENDING_NEW` por alguns instantes depois da execução da entrada.** O agente já trata isso (`ARMING`, sincroniza de novo). O teste *live* passou a esperar o armamento.
3. **Reuso de `listClientOrderId` depois de encerrada a lista é aceito.** Confirma a D-012: o `seq` dos IDs sempre avança e nunca é reutilizado.
4. **`contingencyType` volta como `"OTO"` numa lista OPOCO.** É só informativo: nenhuma lógica do agente depende desse campo.
5. **O Testnet não cobra comissão** (0 BTC na compra). No Demo Mode e em produção, a comissão em BTC reduz a quantidade recebida. O agente calcula a quantidade líquida pelas execuções (`net_received_base`), e a semântica OPO (pendente = recebido) deve ser reconfirmada no Demo Mode.
6. **O saldo recebido fica travado pelo OCO pendente** (`locked`, não `free`). Medir o recebido pelo saldo livre dá zero. O spike foi corrigido para medir o total.

## 3. Teste *live* da Fase 1

`uv run pytest -m live`: **aprovado** no Testnet. O ciclo completo abrir (OPOCO) → armar → ajustar (troca do OCO) → fechar (cancelar + vender a mercado) funciona. Na primeira execução, o bug do item 2.1 fez o teste falhar **depois** de abrir a posição, e ela ficou aberta no Testnet: uma lista `ta1-live-…` com 0.00017 BTC, encerrada manualmente com `trade-agent close`. O teste agora tem limpeza em `finally`: uma falha no meio nunca deixa posição aberta.

## 4. Pendências

- Repetir o spike e o teste *live* no **Demo Mode** (`TA_BINANCE_ENV=demo`), que cobra comissões, antes do *paper trading* (Fase 7).
