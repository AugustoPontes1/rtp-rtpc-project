"""
main.py — Ponto de entrada e orquestrador do monitor RTP/RTCP

Responsabilidades deste módulo:
  1. Configuração via variáveis de ambiente (.env)
  2. Estado global compartilhado entre callbacks e o loop de display
  3. Callbacks on_rtp() e on_rtcp() chamados pelo listener a cada pacote
  4. Loop assíncrono que atualiza o display Rich a cada 500ms
  5. Geração do RTCP RR de saída a cada ~5 segundos

Fluxo de dados:
  UDP socket → listener.py → on_rtp() → session.update() + jitter_buffer.put()
                           → on_rtcp() → session.last_sr_* (para cálculo de DLSR)
  asyncio loop → _refresh_loop() → reporter.build_layout() → rich.Live.update()
"""

import asyncio
import os
import random
import time

from dotenv import load_dotenv
from rich.live import Live

from src.jitter_buffer import AdaptiveJitterBuffer
from src.listener import start_listeners
from src.packet.rtcp import RTCP_SR, SenderReport, build_rr
from src.packet.rtp import RTPPacket
from src.reporter import build_layout, console
from src.session import RTPSession

# Carrega variáveis do arquivo .env (se existir) antes de ler os os.getenv()
load_dotenv()

HOST     = os.getenv("RTP_HOST", "0.0.0.0")          # interface de escuta ("0.0.0.0" = todas)
RTP_PORT = int(os.getenv("RTP_PORT", "5004"))         # porta RTP par; RTCP = RTP_PORT + 1
MY_SSRC  = int(os.getenv("MY_SSRC", str(random.randint(0, 0xFFFFFFFF))))
# MY_SSRC identifica ESTE monitor nos RRs que ele geraria.
# Na RFC 3550, todo participante tem um SSRC único, mesmo que só escute.

# --- Estado global ---
# Dicionário SSRC → RTPSession: uma entrada por stream ativo detectado
sessions: dict[int, RTPSession] = {}

# Buffer adaptativo compartilhado: todos os streams vão para o mesmo buffer
# (simplificação; em produção haveria um buffer por SSRC)
jitter_buffer = AdaptiveJitterBuffer(min_delay_ms=20, max_delay_ms=200)

# Último RTCP RR gerado em hex (para exibir no painel)
last_rr_hex: str | None = None


def on_rtp(packet: RTPPacket, addr: tuple[str, int]) -> None:
    """
    Callback chamado pelo listener a cada pacote RTP recebido.

    Executado no event loop do asyncio — não deve bloquear.
    Se o SSRC for novo, cria uma sessão e loga a detecção.
    Em seguida atualiza a sessão e o jitter buffer.
    """
    # Detecta novo stream (novo SSRC = nova fonte de mídia)
    if packet.ssrc not in sessions:
        sessions[packet.ssrc] = RTPSession(ssrc=packet.ssrc)
        console.print(
            f"[green bold]Novo stream detectado[/green bold] "
            f"SSRC=0x{packet.ssrc:08X}  codec={packet.codec_name}  "
            f"addr={addr[0]}:{addr[1]}"
        )

    session = sessions[packet.ssrc]

    # Atualiza a sessão: seq tracking, jitter, contadores de perda
    session.update(packet)

    # Insere no buffer e ajusta o target_delay com o jitter atual.
    # jitter / clock_rate converte de amostras para segundos (ex: 16/8000 = 2ms)
    jitter_buffer.put(packet)
    jitter_buffer.adapt(session.jitter / max(session.clock_rate, 1))


def on_rtcp(pt: int, obj: object, addr: tuple[str, int]) -> None:
    """
    Callback chamado pelo listener a cada pacote RTCP recebido.

    Só nos interessa o SR: guardamos o NTP compact (LSR) e o instante
    de chegada para calcular o DLSR quando gerarmos nosso próximo RR.
    O DLSR diz ao transmissor: "quanto tempo levei para responder ao seu SR".
    """
    if pt == RTCP_SR and isinstance(obj, SenderReport):
        if obj.ssrc in sessions:
            # Armazena os dados do SR para uso no próximo build_report_block()
            sessions[obj.ssrc].last_sr_ntp_compact = obj.ntp_compact
            sessions[obj.ssrc].last_sr_arrival     = time.monotonic()


def _generate_rr() -> str | None:
    """
    Constrói o RTCP RR com o estado atual de todas as sessões.

    Em uma implementação real, este pacote seria enviado via UDP para
    cada transmissor. Aqui apenas exibimos os bytes para fins didáticos.

    Retorna os bytes em hex (ex: "81 c9 00 07 de ad be ef ...") ou None
    se ainda não houver sessões ativas.
    """
    if not sessions:
        return None

    now = time.monotonic()

    # Um ReportBlock por SSRC ativo — máximo de 31 por RFC (5 bits = RC)
    blocks = [s.build_report_block(now) for s in sessions.values()]

    # build_rr() serializa tudo em bytes prontos para envio via socket.sendto()
    rr_bytes = build_rr(MY_SSRC, blocks)

    return " ".join(f"{b:02X}" for b in rr_bytes)


async def _refresh_loop(live: Live) -> None:
    """
    Loop principal do display: atualiza a tela e gera RR a cada ciclo.

    asyncio.sleep(0.5) cede o controle ao event loop, permitindo que
    datagram_received() dos listeners seja chamado nos intervalos.
    Sem o sleep, o loop bloquearia o event loop e nenhum pacote seria processado.

    O RR é gerado apenas quando int(monotonic) % 5 == 0, ou seja, uma vez
    por segundo múltiplo de 5. Simplificação do intervalo adaptativo da RFC.
    """
    global last_rr_hex

    while True:
        # Drena o buffer: libera pacotes cujo playout_time já chegou
        # Em produção, os pacotes liberados seriam enviados para o decodificador de áudio
        jitter_buffer.get_ready()

        # Gera o RTCP RR a cada ~5 segundos (simplificação)
        if int(time.monotonic()) % 5 == 0:
            last_rr_hex = _generate_rr()

        # Atualiza o display com o estado atual de todas as variáveis
        live.update(build_layout(sessions, jitter_buffer.stats, last_rr_hex))

        # Aguarda 500ms antes do próximo ciclo de display
        # (pacotes RTP chegam a cada 20ms — o listener os processa nos intervalos)
        await asyncio.sleep(0.5)


async def main() -> None:
    """
    Entry point assíncrono.

    1. Exibe banner com portas configuradas
    2. Abre os dois sockets UDP (RTP e RTCP) via start_listeners()
    3. Inicia o rich.Live e o loop de display
    4. Fecha os sockets ao sair (Ctrl+C)
    """
    console.print(f"[bold blue]RTP/RTCP Monitor[/bold blue]  —  RFC 3550")
    console.print(
        f"RTP  → [cyan]{HOST}:{RTP_PORT}[/cyan]  |  "
        f"RTCP → [cyan]{HOST}:{RTP_PORT + 1}[/cyan]"
    )
    console.print(f"Meu SSRC: [yellow]0x{MY_SSRC:08X}[/yellow]\n")

    # Registra os callbacks e abre os sockets UDP
    rtp_tr, rtcp_tr = await start_listeners(HOST, RTP_PORT, on_rtp, on_rtcp)

    try:
        # rich.Live mantém a área do terminal reservada e só redesenha o que muda.
        # refresh_per_second=2 garante que mesmo sem pacotes o display não trava.
        initial = build_layout(sessions, jitter_buffer.stats, last_rr_hex)
        with Live(initial, refresh_per_second=2, console=console) as live:
            await _refresh_loop(live)

    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C é esperado — não é um erro
        pass

    finally:
        # Sempre fecha os sockets, mesmo se houver exceção
        rtp_tr.close()
        rtcp_tr.close()
        console.print("\n[dim]Monitor encerrado.[/dim]")


if __name__ == "__main__":
    # asyncio.run() cria o event loop, executa main() até o fim e fecha o loop.
    # É o padrão moderno desde Python 3.7 — substitui loop.run_until_complete().
    asyncio.run(main())
