"""
reporter.py — Geração do display Rich em tempo real

Funções puras que recebem o estado atual (sessions, buffer_stats, last_rr_hex)
e retornam objetos Rich prontos para renderização. Não há estado interno aqui —
toda a mutabilidade fica em main.py.

Rich.Live atualiza o terminal de forma eficiente: só redesenha as linhas que
mudaram, evitando flicker.
"""

from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .session import RTPSession

# Console global compartilhado — usado também em main.py para logs fora do Live
console = Console()


def _jitter_color(ms: float) -> str:
    """
    Cor do jitter baseada em thresholds práticos para VoIP:
      < 20ms → verde  (imperceptível, abaixo de 1 pacote G.711)
      < 50ms → amarelo (começa a ser percebido, mas buffer absorve)
      ≥ 50ms → vermelho (clipes e cortes frequentes sem buffer adequado)
    """
    if ms < 20:
        return "green"
    if ms < 50:
        return "yellow"
    return "red"


def _loss_color(pct: float) -> str:
    """
    Cor da perda baseada em thresholds do ITU-T G.109 (qualidade subjetiva):
      < 1%  → verde  (Qualidade Boa)
      < 5%  → amarelo (Qualidade Aceitável)
      ≥ 5%  → vermelho (Qualidade Ruim)
    """
    if pct < 1:
        return "green"
    if pct < 5:
        return "yellow"
    return "red"


def build_session_table(sessions: dict[int, RTPSession]) -> Table:
    """
    Tabela principal: uma linha por SSRC ativo.

    Cada linha mostra os campos que um receptor reportaria no RTCP RR,
    mais o jitter calculado localmente — exatamente o que vimos no módulo.
    """
    t = Table(title="Streams RTP ativos", border_style="blue", expand=True)

    # Colunas espelhando os campos do ReportBlock (RFC 3550 §6.4.1)
    t.add_column("SSRC",      style="cyan",  no_wrap=True)  # identificador da fonte
    t.add_column("Codec",     style="green")                # PT mapeado para nome
    t.add_column("Seq#",      justify="right")              # extended_highest_seq
    t.add_column("Recebidos", justify="right")              # packet count
    t.add_column("Perdidos",  justify="right")              # cumulative_lost
    t.add_column("Perda %",   justify="right")              # fraction_lost em %
    t.add_column("Jitter ms", justify="right")              # interarrival jitter em ms
    t.add_column("Clock Hz",  justify="right", style="dim") # taxa de clock do codec

    for ssrc, s in sessions.items():
        lc = _loss_color(s.loss_percent)
        jc = _jitter_color(s.jitter_ms)

        t.add_row(
            f"0x{ssrc:08X}",            # hex com 8 dígitos, estilo Wireshark
            s.codec_name,
            str(s._max_seq),            # seq mais alto recebido no ciclo atual
            str(s.received),
            str(s.lost),
            f"[{lc}]{s.loss_percent:.1f}%[/{lc}]",   # colorido pelo threshold
            f"[{jc}]{s.jitter_ms:.2f}[/{jc}]",       # colorido pelo threshold
            str(s.clock_rate),
        )

    # Linha de placeholder enquanto nenhum stream foi detectado
    if not sessions:
        t.add_row("—", "Aguardando pacotes...", "—", "—", "—", "—", "—", "—")

    return t


def build_buffer_panel(stats: dict) -> Panel:
    """
    Painel lateral mostrando o estado do jitter buffer.

    'depth' = pacotes aguardando no heap (idealmente baixo — indica delay acumulado)
    'target_delay_ms' = delay adaptativo atual (sobe quando jitter piora)
    """
    lines = [
        f"[bold]Target delay:[/bold]     {stats['target_delay_ms']:.1f} ms",
        f"[bold]Pacotes no buffer:[/bold] {stats['depth']}",
        f"[bold]Entregues:[/bold]         {stats['delivered']}",
        f"[bold]Descartados:[/bold]       {stats['discarded']}",
    ]
    return Panel("\n".join(lines), title="Jitter Buffer", border_style="yellow", expand=False)


def build_rtcp_panel(rr_hex: str | None) -> Panel:
    """
    Painel mostrando o RTCP RR que seria enviado ao transmissor.

    Exibe os bytes em hexadecimal — assim dá para confrontar com uma
    captura no Wireshark e entender cada campo do pacote real.
    """
    if rr_hex is None:
        body = "[dim]Nenhum RR gerado ainda (aguardando 5s)...[/dim]"
    else:
        # Quebra em grupos de 8 bytes por linha para facilitar leitura
        words = rr_hex.split(" ")
        lines_hex = " ".join(
            " ".join(words[i:i+8]) for i in range(0, len(words), 8)
        )
        body = f"[bold]RTCP RR (hex):[/bold]\n[cyan]{lines_hex}[/cyan]"

    return Panel(body, title="RTCP Receiver Report", border_style="magenta", expand=False)


def build_layout(
    sessions: dict[int, RTPSession],
    buffer_stats: dict,
    last_rr_hex: str | None,
) -> Columns:
    """
    Monta o layout completo passado para rich.Live.update().

    Estrutura visual:
      ┌─────────────────────────────┬──────────────────────────────────┐
      │    Tabela de Streams RTP    │  Jitter Buffer │  RTCP RR (hex)  │
      └─────────────────────────────┴──────────────────────────────────┘

    Columns() do Rich exibe os renderáveis lado a lado horizontalmente.
    """
    session_table = build_session_table(sessions)
    buffer_panel  = build_buffer_panel(buffer_stats)
    rtcp_panel    = build_rtcp_panel(last_rr_hex)

    # Painel direito agrupa buffer e RTCP um abaixo do outro
    right_side = Columns([buffer_panel, rtcp_panel])

    return Columns([session_table, right_side], expand=True)
