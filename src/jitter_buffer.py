"""
jitter_buffer.py — Jitter buffer adaptativo (simulação)

O jitter buffer resolve o problema central do áudio sobre IP:
pacotes chegam com atraso variável (jitter), mas precisam ser
reproduzidos com atraso constante.

Funcionamento:
  1. Cada pacote entra com seu arrival_time + target_delay como playout_time
  2. O heap (fila de prioridade) mantém pacotes ordenados por playout_time
  3. get_ready() libera apenas pacotes cujo playout_time já passou

O "adaptativo" vem do adapt(): o target_delay se ajusta dinamicamente
com base no jitter medido pela sessão, buscando o equilíbrio entre:
  - Delay baixo: melhor para conversação, mas descarta pacotes atrasados
  - Delay alto: nenhum descarte, mas introduz latência perceptível
"""

import heapq
import time
from dataclasses import dataclass, field

from .packet.rtp import RTPPacket

# Limites do target_delay para evitar casos extremos
MIN_DELAY_MS = 20.0   # delay mínimo = 1 pacote G.711 de 20ms
MAX_DELAY_MS = 200.0  # delay máximo = 10 pacotes (ITU-T G.114 recomenda < 150ms total)

# Fator de suavização exponencial: valor pequeno = resposta lenta (mais estável)
# Fórmula: new_delay = (1-α) × old_delay + α × ideal_delay
ADAPT_ALPHA = 0.1


@dataclass(order=True)   # order=True permite que o heapq compare BufferedPackets pelo playout_time
class _BufferedPacket:
    """
    Wrapper interno que associa um pacote ao seu instante de reprodução.
    O campo 'playout_time' é usado pelo heap para ordenação (menor = sai primeiro).
    'field(compare=False)' evita que o heapq tente comparar RTPPacket, que não é comparável.
    """
    playout_time: float
    packet: RTPPacket = field(compare=False)


class AdaptiveJitterBuffer:
    """
    Jitter buffer adaptativo baseado em heap (fila de prioridade mínima).

    O uso de heap em vez de deque simples garante que:
    - Pacotes reordenados são reproduzidos na ordem de timestamp correta
    - A operação de "buscar o próximo pronto" é O(log n)
    """

    def __init__(
        self,
        min_delay_ms: float = MIN_DELAY_MS,
        max_delay_ms: float = MAX_DELAY_MS,
    ) -> None:
        self._heap: list[_BufferedPacket] = []  # heap interno (gerenciado por heapq)
        self._min_delay = min_delay_ms / 1000.0  # converte ms → segundos internamente
        self._max_delay = max_delay_ms / 1000.0
        self._target_delay = min_delay_ms / 1000.0  # começa no mínimo, cresce se necessário
        self._delivered = 0   # estatística: total de pacotes liberados para reprodução
        self._discarded = 0   # estatística: total descartados (não usado aqui, para extensão futura)

    def put(self, packet: RTPPacket) -> None:
        """
        Insere um pacote no buffer.

        O playout_time é calculado como:
          arrival_time + target_delay

        Isso significa: "reproduza este pacote daqui a target_delay segundos
        a partir de quando ele chegou". Se o pacote chegou atrasado, seu
        playout_time pode já ter passado — get_ready() o liberará imediatamente.
        """
        playout = packet.arrival_time + self._target_delay
        heapq.heappush(self._heap, _BufferedPacket(playout, packet))

    def get_ready(self, now: float | None = None) -> list[RTPPacket]:
        """
        Retorna todos os pacotes cujo instante de reprodução já chegou.

        heapq.heappop() sempre remove o menor elemento (playout_time mais próximo),
        garantindo que pacotes saiam em ordem cronológica de reprodução,
        independente da ordem de chegada.
        """
        if now is None:
            now = time.monotonic()

        ready: list[RTPPacket] = []
        while self._heap and self._heap[0].playout_time <= now:
            item = heapq.heappop(self._heap)
            ready.append(item.packet)
            self._delivered += 1
        return ready

    def adapt(self, jitter_seconds: float) -> None:
        """
        Ajusta o target_delay com base no jitter atual da sessão.

        Estratégia: o delay ideal é 2× o jitter medido. Isso dá margem para
        absorver variações sem introduzir latência desnecessária.

        A suavização exponencial (ADAPT_ALPHA = 0.1) faz o delay subir rápido
        quando o jitter piora, mas voltar devagar quando melhora — proteção
        conservadora contra descartes.

        Exemplo com ADAPT_ALPHA=0.1:
          jitter=50ms → ideal=100ms → delay cresce 10% em direção a 100ms por ciclo
        """
        # Calcula o delay ideal: 2× jitter, clampado entre min e max
        ideal = min(self._max_delay, max(self._min_delay, jitter_seconds * 2))

        # Média exponencial: suaviza a transição para evitar saltos bruscos no buffer
        self._target_delay = (1 - ADAPT_ALPHA) * self._target_delay + ADAPT_ALPHA * ideal

    @property
    def depth(self) -> int:
        """Número de pacotes atualmente aguardando no buffer."""
        return len(self._heap)

    @property
    def target_delay_ms(self) -> float:
        """Delay de reprodução atual em milissegundos."""
        return self._target_delay * 1000.0

    @property
    def stats(self) -> dict:
        """Snapshot das métricas do buffer para exibição no reporter."""
        return {
            "depth":           self.depth,
            "target_delay_ms": round(self.target_delay_ms, 1),
            "delivered":       self._delivered,
            "discarded":       self._discarded,
        }
