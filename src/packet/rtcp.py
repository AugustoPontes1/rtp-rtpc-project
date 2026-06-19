"""
rtcp.py — Parser e construtor de pacotes RTCP (RFC 3550 §6)

RTCP (RTP Control Protocol) circula na porta adjacente ao RTP (sempre porta ímpar)
e consome no máximo 5% da banda do fluxo. Ele não carrega mídia — carrega métricas
de qualidade de serviço (QoS) trocadas entre transmissor e receptor.

Tipos de pacotes implementados aqui:
  SR  (200) — Sender Report:   enviado por quem está transmitindo mídia
  RR  (201) — Receiver Report: enviado por quem está apenas recebendo
  SDES(202) — Source Description: metadados legíveis (nome, e-mail, CNAME)
  BYE (203) — sinaliza saída de um participante da sessão
"""

import struct
from dataclasses import dataclass, field
from typing import Optional

# Códigos de tipo de pacote RTCP (campo PT no cabeçalho)
RTCP_SR   = 200
RTCP_RR   = 201
RTCP_SDES = 202
RTCP_BYE  = 203

SEQ_MOD = 1 << 16  # módulo do sequence number (16 bits → 0–65535)


@dataclass
class ReportBlock:
    """
    Bloco de relatório de 24 bytes — presente tanto no SR quanto no RR.

    Cada bloco descreve a qualidade de recepção de UMA fonte (identificada
    pelo SSRC). Em conferências multi-participante, o pacote contém um
    bloco por fonte reportada.

    Layout (24 bytes):
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     |                 SSRC_n (fonte sendo reportada)                |  4B
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     | fraction lost |       cumulative number of packets lost       |  4B (1B + 3B)
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     |           extended highest sequence number received           |  4B
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     |                      interarrival jitter                      |  4B
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     |                         last SR (LSR)                         |  4B
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     |                   delay since last SR (DLSR)                  |  4B
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    """

    ssrc: int                 # SSRC da fonte que está sendo reportada
    fraction_lost: int        # Fração de perda no último intervalo: valor/256 = % de perda
                              # Ex: 25 → 25/256 ≈ 9,8% de perda neste intervalo
    cumulative_lost: int      # Total de pacotes perdidos desde o início (24 bits)
    extended_highest_seq: int # Número de sequência mais alto recebido (com ciclos de wrap)
    jitter: int               # Jitter de interchegada em unidades de timestamp (amostras)
    last_sr: int              # Middle 32 bits do NTP timestamp do último SR recebido
                              # Usado pelo transmissor para calcular o RTT (round-trip time)
    delay_since_last_sr: int  # Tempo desde o último SR, em unidades de 1/65536 segundos

    SIZE: int = 24  # tamanho fixo em bytes

    @classmethod
    def parse(cls, data: bytes, offset: int = 0) -> Optional["ReportBlock"]:
        """
        Lê um ReportBlock a partir de `offset` dentro de `data`.

        O campo 'fraction_lost + cumulative_lost' é empacotado em 4 bytes:
          - byte mais significativo (bits 31-24) = fraction_lost (8 bits)
          - bytes restantes (bits 23-0)          = cumulative_lost (24 bits)
        """
        if len(data) < offset + cls.SIZE:
            return None

        # Lê o SSRC da fonte reportada
        src = struct.unpack_from("!I", data, offset)[0]

        # Desempacota a word que mistura fraction_lost e cumulative_lost
        word2 = struct.unpack_from("!I", data, offset + 4)[0]
        fraction_lost   = (word2 >> 24) & 0xFF      # extrai byte mais alto
        cumulative_lost = word2 & 0xFFFFFF           # extrai os 3 bytes restantes

        # Os quatro campos restantes são integers simples de 4 bytes cada
        ext_seq, jitter, lsr, dlsr = struct.unpack_from("!IIII", data, offset + 8)

        return cls(
            ssrc=src,
            fraction_lost=fraction_lost,
            cumulative_lost=cumulative_lost,
            extended_highest_seq=ext_seq,
            jitter=jitter,
            last_sr=lsr,
            delay_since_last_sr=dlsr,
        )

    def pack(self) -> bytes:
        """
        Serializa o bloco de volta para bytes (para montar um RTCP RR de saída).

        Empacota fraction_lost e cumulative_lost juntos em uma única word de 32 bits,
        revertendo o processo do parse().
        """
        # Desloca fraction_lost para os 8 bits mais altos e OR com os 24 bits de lost
        word2 = ((self.fraction_lost & 0xFF) << 24) | (self.cumulative_lost & 0xFFFFFF)
        return struct.pack(
            "!IIIIII",
            self.ssrc,
            word2,
            self.extended_highest_seq,
            self.jitter,
            self.last_sr,
            self.delay_since_last_sr,
        )

    @property
    def loss_percent(self) -> float:
        """Converte fraction_lost (0–255) para porcentagem (0–100%)."""
        return (self.fraction_lost / 256.0) * 100.0


@dataclass
class SenderReport:
    """
    SR (Sender Report) — RFC 3550 §6.4.1

    Enviado pelo transmissor ativo. Além dos ReportBlocks (que reportam
    o que ele recebe das outras fontes), o SR também contém informações
    sobre o que ele está enviando:
      - NTP timestamp: tempo absoluto de parede para sincronização A/V
      - RTP timestamp: valor correspondente ao NTP no clock do codec
      - Contagem de pacotes e bytes enviados

    O par (NTP, RTP timestamp) é o que permite sincronizar fluxos de áudio
    e vídeo de SSRCs diferentes — cada um tem seu próprio clock RTP.
    """

    ssrc: int
    ntp_ts_msw: int       # NTP: segundos desde 1 Jan 1900 (most significant word)
    ntp_ts_lsw: int       # NTP: fração de segundo (least significant word, 1/2³² s)
    rtp_timestamp: int    # Timestamp RTP correspondente ao instante NTP acima
    packet_count: int     # Total de pacotes RTP enviados desde o início da sessão
    octet_count: int      # Total de bytes de payload enviados (sem cabeçalhos)
    report_blocks: list[ReportBlock] = field(default_factory=list)

    @classmethod
    def parse(cls, data: bytes, rc: int) -> Optional["SenderReport"]:
        """
        Parseia um SR.

        `data` começa APÓS o cabeçalho RTCP comum de 4 bytes.
        `rc`   é o Reception report Count do cabeçalho (número de blocos).

        Estrutura do corpo do SR:
          [4B SSRC] [8B NTP ts] [4B RTP ts] [4B pkt count] [4B octet count]
          + rc × ReportBlock(24B)
        """
        if len(data) < 24:
            return None

        # Lê os 6 campos de 4 bytes cada (24 bytes no total)
        ssrc, ntp_msw, ntp_lsw, rtp_ts, pkt_cnt, oct_cnt = struct.unpack_from("!IIIIII", data)

        # Parseia os ReportBlocks que vêm depois dos 24 bytes do sender info
        blocks: list[ReportBlock] = []
        offset = 24
        for _ in range(rc):
            block = ReportBlock.parse(data, offset)
            if block:
                blocks.append(block)
                offset += ReportBlock.SIZE

        return cls(
            ssrc=ssrc,
            ntp_ts_msw=ntp_msw,
            ntp_ts_lsw=ntp_lsw,
            rtp_timestamp=rtp_ts,
            packet_count=pkt_cnt,
            octet_count=oct_cnt,
            report_blocks=blocks,
        )

    @property
    def ntp_compact(self) -> int:
        """
        Middle 32 bits do NTP timestamp (LSR — Last SR).

        O receptor armazena este valor e o inclui no campo 'last_sr' do
        próximo RR. O transmissor subtrai LSR do NTP atual para calcular
        o RTT (round-trip time):
          RTT = NTP_agora - LSR - DLSR
        """
        # Pega os 16 bits menos significativos do MSW e os 16 bits mais
        # significativos do LSW, concatenando-os em 32 bits
        return ((self.ntp_ts_msw & 0xFFFF) << 16) | ((self.ntp_ts_lsw >> 16) & 0xFFFF)


def build_rr(my_ssrc: int, blocks: list[ReportBlock]) -> bytes:
    """
    Constrói um pacote RTCP RR completo, pronto para envio via UDP.

    O RR é o "feedback" que o receptor envia ao transmissor para informar
    a qualidade de recepção. Com esses dados, o transmissor pode ajustar
    o bitrate ou o codec para melhorar a qualidade.

    Estrutura do RR:
      [1B: V=2|P=0|RC] [1B: PT=201] [2B: length] [4B: SSRC do reporter]
      + RC × ReportBlock(24B)

    O campo 'length' é o número de palavras de 32 bits NO PACOTE INTEIRO
    menos 1 (convenção da RFC 3550).
    """
    rc = len(blocks)

    # Calcula o tamanho em words: 1 (SSRC do reporter) + 6 words por bloco (24B / 4)
    length_words = 1 + rc * 6

    # Monta o cabeçalho RTCP:
    # - bits 7-6: versão = 2      → (2 << 6) = 0x80
    # - bit  5:   padding = 0
    # - bits 4-0: RC (report count)
    # O OR com (rc & 0x1F) garante que RC fica nos 5 bits menos significativos
    header    = struct.pack("!BBH", (2 << 6) | (rc & 0x1F), RTCP_RR, length_words)
    ssrc_field = struct.pack("!I", my_ssrc)

    # Concatena todos os ReportBlocks serializados
    blocks_bytes = b"".join(b.pack() for b in blocks)

    return header + ssrc_field + blocks_bytes


def parse_rtcp(data: bytes) -> Optional[tuple[int, object]]:
    """
    Ponto de entrada para parsear qualquer pacote RTCP.

    Retorna (packet_type, objeto_parseado) ou None se o pacote for inválido.

    O cabeçalho RTCP comum (4 bytes) tem o mesmo layout do RTP:
      byte0: V(2) P(1) RC/SC(5)  — RC = reception count, SC = source count
      byte1: PT (packet type)    — 200=SR, 201=RR, 202=SDES, 203=BYE
      bytes 2-3: length          — tamanho em words menos 1
    """
    if len(data) < 4:
        return None

    byte0 = data[0]
    pt    = data[1]
    rc    = byte0 & 0x1F  # bits 4-0: reception report count (SR/RR) ou source count (SDES)

    if pt == RTCP_SR:
        # data[4:] pula o cabeçalho comum de 4 bytes, passando direto para o corpo do SR
        sr = SenderReport.parse(data[4:], rc)
        return (RTCP_SR, sr) if sr else None

    if pt == RTCP_RR:
        # Recebemos um RR de outro participante — útil para ver o que eles acham da nossa transmissão
        return (RTCP_RR, None)

    if pt == RTCP_SDES:
        # Source Description — associa SSRC a metadados como nome e e-mail
        return (RTCP_SDES, None)

    if pt == RTCP_BYE:
        # Participante saindo da sessão
        return (RTCP_BYE, None)

    # Tipo desconhecido — ignora sem travar
    return (pt, None)
