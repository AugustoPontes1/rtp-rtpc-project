"""
simulator.py — Gerador de tráfego RTP G.711 para testes sem Asterisk

Simula um transmissor G.711 PCMA enviando pacotes para o monitor.
Útil para testar e visualizar o comportamento do monitor sem precisar
do stack VoIP completo (Asterisk + SIP).

Campos que o simulador preenche corretamente:
  - Version = 2, PT = 8 (G.711 PCMA), CC = 0, M = 0
  - Sequence Number: incrementado em 1 por pacote (wrap-around em 65535→0)
  - Timestamp: incrementado em 160 a cada pacote (G.711: 8000Hz × 20ms = 160 amostras)
  - SSRC: aleatório (gerado uma vez no início, fixo durante a sessão)
  - Payload: bytes aleatórios de 160 bytes (simula amostras PCM codificadas)

Uso:
  python simulator.py                        # configuração padrão
  python simulator.py --jitter 50            # adiciona até 50ms de jitter artificial
  python simulator.py --loss 5               # descarta 5% dos pacotes (sem enviar)
  python simulator.py --host 192.168.1.10    # envia para outro host
"""

import argparse
import random
import socket
import struct
import time


def build_rtp(
    ssrc: int,
    seq: int,
    timestamp: int,
    payload_type: int = 8,   # 8 = G.711 PCMA (A-law), padrão europeu/brasileiro
    payload_size: int = 160, # 160 bytes = 20ms de áudio a 8000 Hz (8 bits/amostra)
) -> bytes:
    """
    Monta um pacote RTP mínimo (sem extensões, sem CSRC) em bytes.

    Cabeçalho (12 bytes):
      byte0 = 0x80 → V=2 (binário 10), P=0, X=0, CC=0000
      byte1 = PT   → M=0, PT=8 (ou o que for passado)
      bytes 2-3: sequence number (big-endian)
      bytes 4-7: timestamp (big-endian)
      bytes 8-11: SSRC (big-endian)

    & 0xFFFF e & 0xFFFFFFFF garantem que os valores não ultrapassem seus campos:
      seq é 16 bits (max 65535), timestamp é 32 bits (max 4294967295)
    """
    byte0  = 0x80            # V=2 | P=0 | X=0 | CC=0
    byte1  = payload_type & 0x7F  # M=0 | PT

    header = struct.pack("!BBHII",
        byte0,
        byte1,
        seq & 0xFFFF,           # sequence number (16 bits, cicla de 0 a 65535)
        timestamp & 0xFFFFFFFF, # timestamp RTP (32 bits, cicla de 0 a ~4 bilhões)
        ssrc,
    )

    # Payload: bytes aleatórios simulando amostras G.711 codificadas
    # Em G.711, cada amostra = 1 byte → 160 bytes = 160 amostras = 20ms
    payload = bytes([random.randint(0, 255)] * payload_size)

    return header + payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulador RTP G.711 para teste do monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host",   default="127.0.0.1", help="IP de destino")
    parser.add_argument("--port",   type=int, default=5004, help="Porta RTP de destino")
    parser.add_argument("--jitter", type=float, default=0.0,
                        help="Jitter artificial máximo em ms (adiciona atraso variável)")
    parser.add_argument("--loss",   type=float, default=0.0,
                        help="Porcentagem de perda simulada (0–100)")
    parser.add_argument("--ssrc",   type=lambda x: int(x, 0), default=None,
                        help="SSRC fixo (ex: 0xDEADBEEF); aleatório se omitido")
    args = parser.parse_args()

    # SSRC aleatório se não especificado — simula comportamento real do transmissor
    ssrc = args.ssrc or random.randint(0, 0xFFFFFFFF)

    # Socket UDP simples — RTP não usa TCP nem handshake
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Valores iniciais aleatórios (RFC 3550 recomenda começar com valores aleatórios
    # para dificultar ataques que adivinham sequence numbers)
    seq       = random.randint(0, 65535)
    timestamp = random.randint(0, 0xFFFFFFFF)

    # Parâmetros G.711 / 20ms por pacote
    CLOCK_RATE   = 8000   # 8000 amostras por segundo
    PACKET_MS    = 20     # duração de cada pacote em ms
    TS_INCREMENT = CLOCK_RATE * PACKET_MS // 1000  # = 160 amostras por pacote
    INTERVAL     = PACKET_MS / 1000.0              # = 0.020 segundos entre pacotes

    print(f"Simulador RTP → {args.host}:{args.port}")
    print(f"SSRC=0x{ssrc:08X}  PT=8 (G.711 PCMA)  TS_INC={TS_INCREMENT} amostras/pkt")
    print(f"Jitter={args.jitter}ms  Loss={args.loss}%  Intervalo={PACKET_MS}ms")
    print("Pressione Ctrl+C para parar.\n")

    sent    = 0
    dropped = 0

    try:
        while True:
            start = time.monotonic()

            # Simula perda de pacote: sorteia um número entre 0 e 100
            # Se cair abaixo do threshold de loss, "descarta" (não envia)
            # mas ainda incrementa seq e ts para simular lacuna real
            if random.random() * 100 < args.loss:
                dropped += 1
                seq       = (seq + 1) & 0xFFFF
                timestamp = (timestamp + TS_INCREMENT) & 0xFFFFFFFF
                time.sleep(INTERVAL)
                continue

            # Monta e envia o pacote RTP
            pkt = build_rtp(ssrc=ssrc, seq=seq, timestamp=timestamp)
            sock.sendto(pkt, (args.host, args.port))

            sent     += 1
            seq       = (seq + 1) & 0xFFFF            # wrap-around automático
            timestamp = (timestamp + TS_INCREMENT) & 0xFFFFFFFF

            # Log a cada 50 pacotes (= 1 segundo de áudio G.711/20ms)
            if sent % 50 == 0:
                print(
                    f"Enviados: {sent:5d}  "
                    f"Descartados (loss sim): {dropped:3d}  "
                    f"Seq atual: {seq:5d}  "
                    f"TS atual: {timestamp}"
                )

            # Calcula quanto tempo sobrou do intervalo de 20ms
            elapsed    = time.monotonic() - start
            sleep_time = INTERVAL - elapsed

            # Adiciona jitter artificial: atraso extra aleatório até args.jitter ms
            # Isso faz os pacotes chegarem com intervalos variáveis no receptor,
            # exercitando o cálculo de jitter e o buffer adaptativo do monitor.
            if args.jitter > 0:
                sleep_time += random.uniform(0, args.jitter / 1000.0)

            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print(f"\nEncerrado. Total enviados={sent}  Descartados por loss={dropped}")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
