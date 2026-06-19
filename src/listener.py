"""
listener.py — Receptores UDP para RTP e RTCP (asyncio)

Por que asyncio em vez de threads?
  Pacotes RTP chegam a cada 20ms. Com threads, o agendador do OS pode
  introduzir latência variável entre recepção e processamento. O asyncio
  processa callbacks no mesmo loop de eventos que atualiza o display,
  sem troca de contexto de thread — overhead mínimo.

Convenção de portas (RFC 3550 §11):
  RTP  → porta PAR   (ex: 5004)
  RTCP → porta ÍMPAR (ex: 5005 = 5004 + 1)

asyncio.DatagramProtocol:
  Interface assíncrona para sockets UDP. O event loop chama
  datagram_received() cada vez que um datagrama chega no socket.
  Não há polling — o OS notifica via epoll/kqueue.
"""

import asyncio
import time
from typing import Callable

from .packet.rtp import RTPPacket
from .packet.rtcp import parse_rtcp

# Tipos dos callbacks para documentar a assinatura esperada por main.py
OnRTP  = Callable[[RTPPacket, tuple[str, int]], None]  # (pacote, (ip, porta))
OnRTCP = Callable[[int, object, tuple[str, int]], None] # (tipo, objeto, (ip, porta))


class _RTPProtocol(asyncio.DatagramProtocol):
    """
    Protocolo UDP para recepção de pacotes RTP.

    O asyncio instancia um objeto desta classe por socket e chama
    datagram_received() para cada datagrama recebido.
    """

    def __init__(self, callback: OnRTP) -> None:
        self._cb = callback
        # O transport é injetado pelo asyncio após connection_made();
        # guardamos para poder fechar o socket mais tarde se necessário.
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        """Chamado pelo asyncio quando o socket UDP está pronto para receber."""
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """
        Chamado pelo asyncio a cada datagrama recebido.

        Registra time.monotonic() como arrival_time ANTES de qualquer
        processamento — assim o timestamp de chegada é o mais preciso possível,
        não afetado pelo tempo de parse.
        """
        packet = RTPPacket.parse(data, time.monotonic())
        if packet:  # None se versão != 2 ou pacote malformado
            self._cb(packet, addr)

    def error_received(self, exc: Exception) -> None:
        """
        Chamado em erros de socket não-fatais (ex: ICMP port unreachable).
        Erros fatais vão para connection_lost().
        """
        pass

    def connection_lost(self, exc: Exception | None) -> None:
        """Chamado quando o socket é fechado (normal ou por erro fatal)."""
        pass


class _RTCPProtocol(asyncio.DatagramProtocol):
    """
    Protocolo UDP para recepção de pacotes RTCP.

    Estrutura idêntica ao _RTPProtocol, mas chama parse_rtcp()
    em vez de RTPPacket.parse(), e o callback tem assinatura diferente.
    """

    def __init__(self, callback: OnRTCP) -> None:
        self._cb = callback
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """
        Parseia o pacote RTCP e despacha para o callback.

        parse_rtcp() retorna (packet_type, objeto) ou None.
        O desempacotamento com * repassa tipo e objeto como args separados.
        """
        result = parse_rtcp(data)
        if result:
            pt, obj = result
            self._cb(pt, obj, addr)

    def error_received(self, exc: Exception) -> None:
        pass

    def connection_lost(self, exc: Exception | None) -> None:
        pass


async def start_listeners(
    host: str,
    rtp_port: int,
    on_rtp: OnRTP,
    on_rtcp: OnRTCP,
) -> tuple[asyncio.DatagramTransport, asyncio.DatagramTransport]:
    """
    Abre dois sockets UDP e retorna seus transports para que o main.py
    possa fechá-los no shutdown.

    loop.create_datagram_endpoint() é a API de baixo nível do asyncio para UDP.
    Ela recebe uma factory (lambda) que cria o protocolo, não uma instância direta,
    porque o asyncio pode precisar recriar o protocolo em alguns casos.

    Retorna (rtp_transport, rtcp_transport) — ambos AsyncIO DatagramTransport.
    Chamar .close() em qualquer um fecha o socket correspondente.
    """
    loop = asyncio.get_running_loop()

    # Socket RTP: porta par (convenção RFC 3550)
    rtp_transport, _ = await loop.create_datagram_endpoint(
        lambda: _RTPProtocol(on_rtp),
        local_addr=(host, rtp_port),
    )

    # Socket RTCP: porta imediatamente acima da RTP (sempre ímpar)
    rtcp_transport, _ = await loop.create_datagram_endpoint(
        lambda: _RTCPProtocol(on_rtcp),
        local_addr=(host, rtp_port + 1),
    )

    return rtp_transport, rtcp_transport
