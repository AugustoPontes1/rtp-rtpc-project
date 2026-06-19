"""
rtp.py — Parser do cabeçalho RTP (RFC 3550 §5.1)

O RTP (Real-time Transport Protocol) é um "envelope" que envolve os dados de
mídia (áudio/vídeo) antes de serem enviados via UDP. Ele adiciona três campos
essenciais que o UDP não tem:
  - Sequence Number: detectar perdas e reordenar pacotes
  - Timestamp: reconstruir o ritmo de reprodução (jitter buffer)
  - SSRC: identificar unicamente cada fonte de mídia
"""

import struct
import time
from dataclasses import dataclass
from typing import ClassVar, Optional

# Tabela de Payload Types estáticos definidos na RFC 3551.
# Formato: PT → (nome_do_codec, clock_rate_em_hz)
# O clock_rate é usado para converter o Timestamp RTP em tempo real:
#   tempo_em_segundos = rtp_timestamp / clock_rate
PAYLOAD_TYPES: dict[int, tuple[str, int]] = {
    0:   ("PCMU/G.711",  8000),   # G.711 µ-law (padrão nos EUA/Japão)
    8:   ("PCMA/G.711",  8000),   # G.711 A-law  (padrão na Europa/Brasil)
    9:   ("G.722",       8000),   # Wideband; áudio é 16kHz mas clock RTP é 8000 (RFC 3551 §4.5.2)
    18:  ("G.729",       8000),   # Codec comprimido de 8 kbps
    96:  ("dynamic",     8000),   # Range 96–127: negociado via SDP/SIP
    101: ("DTMF",        8000),   # Tons de teclado (RFC 2833)
}


@dataclass(frozen=True)  # frozen=True → imutável após criação (pacote recebido não deve ser alterado)
class RTPPacket:
    """
    Representa um pacote RTP parseado.

    Layout do cabeçalho (mínimo 12 bytes):

     0                   1                   2                   3
     0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |V=2|P|X|  CC   |M|     PT      |       sequence number         |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                           timestamp                           |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |           synchronization source (SSRC) identifier           |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    """

    version: int          # Sempre 2 (versão atual do RTP, definida na RFC 3550)
    padding: bool         # Se True, bytes extras no final do payload devem ser ignorados
    extension: bool       # Se True, há um cabeçalho de extensão antes do payload
    cc: int               # CSRC count: número de fontes contribuintes (conferências)
    marker: bool          # Marca eventos especiais (ex: primeiro pacote após silêncio)
    payload_type: int     # Identifica o codec (veja PAYLOAD_TYPES acima)
    sequence_number: int  # Incrementado a cada pacote; detecta perda e reordenação
    timestamp: int        # Conta amostras produzidas desde o início da sessão
    ssrc: int             # ID único de 32 bits desta fonte de mídia
    payload: bytes        # Dados de áudio/vídeo codificados (sem cabeçalho)
    arrival_time: float   # time.monotonic() no momento em que o pacote chegou

    # Tamanho fixo do cabeçalho RTP sem extensões e sem lista CSRC
    HEADER_SIZE: ClassVar[int] = 12

    @classmethod
    def parse(cls, data: bytes, arrival_time: float | None = None) -> Optional["RTPPacket"]:
        """
        Converte bytes brutos (vindos do socket UDP) em um RTPPacket.

        Retorna None se:
          - O pacote tem menos de 12 bytes (cabeçalho incompleto)
          - A versão não é 2 (pacote inválido ou de outro protocolo)
        """
        if len(data) < cls.HEADER_SIZE:
            return None

        # struct.unpack_from("!BBHII", data) lê da esquerda para a direita:
        #   B  = 1 byte  → byte0 (contém V, P, X, CC)
        #   B  = 1 byte  → byte1 (contém M, PT)
        #   H  = 2 bytes → sequence number (unsigned short)
        #   I  = 4 bytes → timestamp       (unsigned int)
        #   I  = 4 bytes → SSRC            (unsigned int)
        # "!" = big-endian (padrão de rede, RFC 791)
        byte0, byte1, seq, ts, ssrc = struct.unpack_from("!BBHII", data)

        # Extrai os campos do byte 0 usando deslocamento de bits:
        # bits 7-6: versão  → desloca 6 para a direita e mantém 2 bits
        # bit  5:   padding → desloca 5 e pega o último bit
        # bit  4:   extensão
        # bits 3-0: CC (CSRC count)
        version   = (byte0 >> 6) & 0x3
        if version != 2:
            return None  # Descarta pacotes de versões antigas (RFC 1889, etc.)

        padding   = bool((byte0 >> 5) & 0x1)
        extension = bool((byte0 >> 4) & 0x1)
        cc        = byte0 & 0xF

        # Extrai os campos do byte 1:
        # bit 7: marker
        # bits 6-0: payload type
        marker = bool((byte1 >> 7) & 0x1)
        pt     = byte1 & 0x7F

        # Calcula onde o payload começa, pulando a lista de CSRCs.
        # Cada CSRC ocupa 4 bytes. Em ligações ponto-a-ponto, CC=0.
        offset = cls.HEADER_SIZE + cc * 4

        # Se há extension header, pula ele também.
        # Os primeiros 2 bytes da extensão são o "profile", os próximos 2 são o tamanho
        # em palavras de 32 bits. Então offset avança 4 (header da extensão) + tamanho*4.
        if extension and len(data) >= offset + 4:
            ext_len = struct.unpack_from("!H", data, offset + 2)[0]
            offset += 4 + ext_len * 4

        return cls(
            version=version,
            padding=padding,
            extension=extension,
            cc=cc,
            marker=marker,
            payload_type=pt,
            sequence_number=seq,
            timestamp=ts,
            ssrc=ssrc,
            payload=data[offset:],  # tudo após o cabeçalho é dado de mídia
            arrival_time=arrival_time if arrival_time is not None else time.monotonic(),
        )

    @property
    def codec_name(self) -> str:
        """Nome legível do codec baseado no Payload Type."""
        name, _ = PAYLOAD_TYPES.get(self.payload_type, (f"PT={self.payload_type}", 8000))
        return name

    @property
    def clock_rate(self) -> int:
        """
        Taxa de clock do codec em Hz.

        O Timestamp RTP é medido em unidades desta taxa, não em segundos.
        Para G.711 (8000 Hz) com pacotes de 20ms:
          incremento = 8000 Hz × 0,020 s = 160 amostras por pacote
        """
        _, rate = PAYLOAD_TYPES.get(self.payload_type, ("unknown", 8000))
        return rate
