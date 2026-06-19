# rtp-rtcp-monitor

Monitor de streams RTP/RTCP em tempo real, construído em Python puro com asyncio.  
Baseado na **RFC 3550** e nos conceitos de **Transporte e Empacotamento de Mídia**.

---

## O que esse projeto faz

Abre dois sockets UDP (um para RTP, um para RTCP), captura pacotes de áudio em tempo real e exibe um painel no terminal com as métricas de qualidade de cada stream detectado — os mesmos campos que aparecem num RTCP Receiver Report real.

```
┌─────────────────────────────────────────────────────────────────┐
│                    Streams RTP ativos                           │
│  SSRC         Codec       Seq#  Recebidos  Perdidos  Jitter ms  │
│  0x3A4B9C1D   PCMA/G.711  45092    1500        2      3.40 ms   │
├──────────────────────┬──────────────────────────────────────────┤
│   Jitter Buffer      │   RTCP Receiver Report                   │
│   Target delay: 20ms │   81 C9 00 07 DE AD BE EF ...            │
│   No buffer:   0     │                                          │
└──────────────────────┴──────────────────────────────────────────┘
```

---

## Estrutura do projeto

```
rtp-rtcp-monitor/
│
├── main.py              # Ponto de entrada — orquestra tudo
├── simulator.py         # Gera tráfego RTP falso para testes sem Asterisk
│
├── src/
│   ├── packet/
│   │   ├── rtp.py       # Parse do cabeçalho RTP (struct.unpack, RFC 3550 §5.1)
│   │   └── rtcp.py      # Parse de SR, construção de RR (RFC 3550 §6)
│   │
│   ├── session.py       # Estado por SSRC: jitter, perda, sequência
│   ├── jitter_buffer.py # Buffer adaptativo com heap
│   ├── listener.py      # Receptores UDP assíncronos (asyncio)
│   └── reporter.py      # Display Rich em tempo real
│
├── requirements.txt
├── .env.example
├── Dockerfile
└── docker-compose.yml
```

---

## Explicação de cada arquivo

### `src/packet/rtp.py` — O parser do cabeçalho RTP

O RTP é o "envelope" que embrulha o áudio antes de enviar pelo UDP. Sem ele, o UDP entrega os bytes mas não sabe a ordem, o ritmo nem de qual fonte vieram.

O cabeçalho tem **12 bytes fixos** com estes campos:

```
Byte 0: [V=2][P][X][CC   ]   → versão, padding, extensão, nº de CSRCs
Byte 1: [M][PT            ]   → marker, payload type (identifica o codec)
Bytes 2-3: Sequence Number    → incrementado a cada pacote
Bytes 4-7: Timestamp          → conta amostras desde o início
Bytes 8-11: SSRC              → ID único de 32 bits desta fonte
```

O código extrai cada campo com deslocamento de bits (`>>` e `&`):

```python
byte0, byte1, seq, ts, ssrc = struct.unpack_from("!BBHII", data)
version = (byte0 >> 6) & 0x3   # pega os 2 bits mais altos do byte 0
pt      = byte1 & 0x7F         # pega os 7 bits mais baixos do byte 1
```

O `"!"` no formato do struct significa **big-endian** — o padrão de rede definido na RFC 791.

---

### `src/packet/rtcp.py` — SR, RR e a construção do Receiver Report

Contém três coisas:

**1. `ReportBlock` — 24 bytes de métricas de recepção**

É o bloco que descreve como uma fonte está sendo recebida. Aparece tanto no SR quanto no RR. Os campos principais:

| Campo | O que mede |
|---|---|
| `fraction_lost` | Perda no último intervalo (0–255, onde 255 = 100%) |
| `cumulative_lost` | Total de pacotes perdidos desde o início |
| `jitter` | Variação de chegada em amostras do codec |
| `last_sr` | Middle 32 bits do NTP do último SR recebido |
| `delay_since_last_sr` | Tempo desde o último SR (em 1/65536 s) |

O truque do empacotamento: `fraction_lost` (1 byte) e `cumulative_lost` (3 bytes) são armazenados juntos em uma word de 32 bits:

```python
word2 = ((fraction_lost & 0xFF) << 24) | (cumulative_lost & 0xFFFFFF)
```

**2. `SenderReport` — enviado pelo transmissor ativo**

Contém o timestamp NTP absoluto (sincroniza áudio e vídeo), o timestamp RTP correspondente e contadores de pacotes/bytes enviados.

**3. `build_rr()` — constrói o RTCP RR para envio**

Monta o pacote de resposta que este monitor enviaria ao transmissor. O header RTCP segue o mesmo padrão do RTP nos primeiros 4 bytes:

```
Byte 0: V=2 | P=0 | RC (nº de blocos)
Byte 1: PT = 201 (código do RR)
Bytes 2-3: tamanho do pacote em words de 32 bits, menos 1
```

---

### `src/session.py` — Estado por SSRC

É o coração do monitor. Para cada SSRC detectado, uma `RTPSession` mantém:

**Rastreamento de sequência**

O sequence number tem apenas 16 bits e cicla de 0 a 65535. Para contar o total real de pacotes, rastreamos quantas vezes ele deu a volta (`_seq_cycles`):

```
extended_max_seq = _seq_cycles + _max_seq
esperados = extended_max_seq - base_seq + 1
perdidos  = esperados - recebidos
```

Um delta em módulo 65536 (`udelta = (seq - max_seq) % 65536`) torna a comparação correta mesmo com wrap-around.

**Cálculo de jitter (RFC 3550 §A.8)**

O jitter mede a variação no tempo de chegada dos pacotes. Se todos chegassem espaçados exatamente 20ms, o jitter seria zero. Na prática, a rede atrasa pacotes de forma irregular.

A fórmula é um **filtro de média exponencial**:

```
transit(i) = tempo_chegada_em_amostras(i) - timestamp_rtp(i)
D(i)       = |transit(i) - transit(i-1)|
J(i)       = J(i-1) + (D(i) - J(i-1)) / 16
```

A divisão por 16 faz o jitter subir rápido quando a rede piora e cair lentamente quando melhora — comportamento conservador que evita que o jitter buffer encolha cedo demais.

Trabalhamos com deltas relativos ao primeiro pacote (e não valores absolutos) para evitar overflow ao multiplicar `time.monotonic()` pela taxa de clock.

**Fraction lost por intervalo**

Os campos `_received_prior` e `_expected_prior` guardam os contadores no início do último relatório. A diferença dá a perda apenas naquele intervalo — não no total — que é o `fraction_lost` do RR.

---

### `src/jitter_buffer.py` — Buffer adaptativo

Resolve o problema do áudio picotado: pacotes chegam com atraso variável, mas precisam ser reproduzidos com ritmo constante.

**Funcionamento:**

```
1. put(packet)   → calcula playout_time = arrival_time + target_delay
                   insere no heap (fila de prioridade)

2. get_ready()   → remove do heap todos com playout_time ≤ agora
                   esses seriam enviados ao decodificador de áudio

3. adapt(jitter) → ajusta target_delay = max(min, jitter × 2)
                   com suavização exponencial (ADAPT_ALPHA = 0.1)
```

O uso de **heap** (e não lista simples) garante que pacotes reordenados sejam reproduzidos na sequência correta — o heapq sempre entrega o menor `playout_time` primeiro.

O trade-off do `target_delay`:
- **Baixo demais** → pacotes atrasados chegam depois do playout_time → são descartados → cliques e cortes
- **Alto demais** → todos chegam a tempo, mas com latência constante elevada → conversas truncadas

---

### `src/listener.py` — Receptores UDP assíncronos

Implementa dois `asyncio.DatagramProtocol`:

- `_RTPProtocol` — escuta na porta **par** (ex: 5004)
- `_RTCPProtocol` — escuta na porta **ímpar** (ex: 5005), convenção da RFC 3550 §11

O asyncio chama `datagram_received()` automaticamente quando um datagrama chega no socket — sem polling, sem threads. O OS notifica via `epoll` (Linux) ou `kqueue` (Mac).

O `arrival_time` é registrado com `time.monotonic()` **antes** de qualquer processamento, para que o timestamp de chegada seja o mais preciso possível e não seja contaminado pelo tempo de parse.

---

### `src/reporter.py` — Display Rich

Funções puras que recebem o estado atual e retornam objetos Rich para renderização. Sem estado interno — toda a mutabilidade fica em `main.py`.

**Tabela de sessões:** uma linha por SSRC, com as colunas espelhando os campos do `ReportBlock` da RFC.

**Cores por threshold:**
- Jitter: verde < 20ms / amarelo < 50ms / vermelho ≥ 50ms
- Perda: verde < 1% / amarelo < 5% / vermelho ≥ 5% (baseado no ITU-T G.109)

**Painel RTCP RR:** exibe os bytes do RR gerado em hexadecimal — dá para comparar diretamente com uma captura no Wireshark.

---

### `main.py` — Orquestrador

Liga todas as peças. O fluxo de dados é:

```
UDP socket
    │
    ▼
listener.py ──→ on_rtp()  ──→ session.update()
                           └→ jitter_buffer.put()
                           └→ jitter_buffer.adapt()
            ──→ on_rtcp() ──→ session.last_sr_* (para campo DLSR do RR)
    │
asyncio loop
    │
    ▼
_refresh_loop() ──→ jitter_buffer.get_ready()   (drena pacotes prontos)
                └→ _generate_rr()               (a cada ~5s)
                └→ reporter.build_layout()
                └→ rich.Live.update()            (atualiza o terminal)
```

O `await asyncio.sleep(0.5)` no loop cede o controle ao event loop a cada ciclo, permitindo que os callbacks `datagram_received()` dos listeners sejam chamados nos intervalos entre atualizações do display.

---

### `simulator.py` — Gerador de tráfego para testes

Envia pacotes RTP G.711 válidos para o monitor sem precisar do Asterisk. Preenche corretamente todos os campos do cabeçalho, incrementa o timestamp em 160 a cada pacote (G.711: 8000 Hz × 20ms) e suporta jitter e perda artificiais configuráveis.

---

## Como rodar

### Pré-requisitos

```bash
pip install -r requirements.txt
```

### Sem o Asterisk (simulador)

Dois terminais:

```bash
# Terminal 1 — inicia o monitor
python main.py

# Terminal 2 — simula um transmissor G.711
python simulator.py

# Com jitter de 40ms e 3% de perda
python simulator.py --jitter 40 --loss 3
```

### Com o VoIP-Project (Asterisk real)

```bash
# 1. Sobe o Asterisk
cd ../VoIP-Project
docker compose up -d

# 2. Inicia o monitor (outra aba)
cd ../rtp-rtcp-monitor
python main.py

# 3. Origina a chamada
cd ../VoIP-Project
python ami.py
```

O monitor captura automaticamente o fluxo RTP gerado pela chamada SIP.

### Via Docker

```bash
docker compose up
```

---

## Variáveis de ambiente

Copie `.env.example` para `.env` e ajuste se necessário:

```env
RTP_HOST=0.0.0.0   # interface de escuta (0.0.0.0 = todas)
RTP_PORT=5004      # porta RTP (RTCP fica em 5005 automaticamente)
```

---

## Conceitos aplicados

| Conceito do módulo | Onde aparece no código |
|---|---|
| Campos do cabeçalho RTP | `src/packet/rtp.py` — `struct.unpack_from` |
| Incremento de timestamp (+160 por pacote) | `simulator.py` — `TS_INCREMENT` |
| Wrap-around do sequence number (16 bits) | `src/session.py` — `_update_seq()` |
| Fórmula de jitter J(i) = J(i-1) + (D - J)/16 | `src/session.py` — `_update_jitter()` |
| SR / RR / campos LSR e DLSR | `src/packet/rtcp.py` — `SenderReport`, `build_rr()` |
| Fraction lost por intervalo | `src/session.py` — propriedade `fraction_lost` |
| Jitter buffer adaptativo | `src/jitter_buffer.py` — `adapt()` |
| Por que UDP e não TCP | Ausência de qualquer retry/ordering — intencionalmente |
| Portas par (RTP) e ímpar (RTCP) | `src/listener.py` — `start_listeners()` |
