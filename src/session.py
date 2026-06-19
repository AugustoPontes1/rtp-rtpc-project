"""
session.py — Estado por SSRC (RFC 3550 §A.1 e §A.8)

Cada fonte de mídia identificada por um SSRC distinto tem sua própria sessão.
Esta classe é o coração do monitor: ela mantém os contadores que permitem
calcular perda de pacotes e jitter em tempo real.
"""

import time
from .packet.rtp import RTPPacket, PAYLOAD_TYPES
from .packet.rtcp import ReportBlock

# Limites para a lógica de detecção de wrap-around e reordenação (RFC 3550 §A.1)
SEQ_MOD      = 1 << 16   # sequence number é de 16 bits → cicla de 0 a 65535
MAX_DROPOUT  = 3000       # lacuna aceitável sem considerar reinício de transmissor
MAX_MISORDER = 100        # pacotes chegando até 100 sequências "atrás" = reordenados


class RTPSession:
    """
    Estado completo de uma sessão RTP para um único SSRC.

    Rastreia:
    - Sequência de pacotes e ciclos de wrap-around do seq number de 16 bits
    - Jitter de interchegada usando a fórmula exponencial da RFC 3550 §A.8
    - Perda de pacotes (fração no intervalo e cumulativa total)
    - Último SR recebido (para preencher os campos LSR/DLSR no RR de resposta)
    """

    def __init__(self, ssrc: int) -> None:
        self.ssrc = ssrc

        # --- rastreamento de sequência ---
        # _max_seq: maior sequence number visto até agora (dentro do ciclo atual)
        # _seq_cycles: número de vezes que o seq number deu a volta (×65536)
        # _base_seq: sequence number do primeiro pacote (ponto de referência)
        # _bad_seq: seq suspeito de reinício do transmissor (ver _update_seq)
        self._max_seq: int = 0
        self._seq_cycles: int = 0
        self._base_seq: int = 0
        self._bad_seq: int = SEQ_MOD + 1  # valor impossível = "nenhum seq suspeito"
        self._received: int = 0
        self._initialized: bool = False

        # Contadores "prior" guardam o estado no início do último intervalo de relatório.
        # A diferença (atual - prior) dá a perda no INTERVALO, que é o fraction_lost do RR.
        self._received_prior: int = 0
        self._expected_prior: int = 0

        # --- cálculo de jitter (RFC 3550 §A.8) ---
        # Ancoramos no primeiro pacote para evitar problemas numéricos:
        # arrival_time é time.monotonic() (segundos desde boot), já o RTP timestamp
        # é um número de 32 bits relativo ao início da transmissão — não podem ser
        # comparados diretamente. Trabalhamos com DELTAS de ambos.
        self._ref_arrival: float | None = None  # arrival_time do primeiro pacote
        self._ref_ts: int = 0                   # timestamp RTP do primeiro pacote
        self._last_transit: float | None = None # transit do pacote anterior
        self._jitter: float = 0.0               # jitter acumulado em amostras (float)

        # --- dados do RTCP SR recebido ---
        # Quando o transmissor nos envia um SR, guardamos o NTP compact (LSR)
        # e o instante de chegada para calcular o DLSR no próximo RR.
        self.last_sr_ntp_compact: int = 0
        self.last_sr_arrival: float = 0.0

        # --- metadados ---
        self.payload_type: int = 0
        self.last_seen: float = time.monotonic()
        self.created_at: float = time.monotonic()

    # ------------------------------------------------------------------ #
    # Propriedades públicas (calculadas a partir do estado interno)        #
    # ------------------------------------------------------------------ #

    @property
    def codec_name(self) -> str:
        name, _ = PAYLOAD_TYPES.get(self.payload_type, (f"PT={self.payload_type}", 8000))
        return name

    @property
    def clock_rate(self) -> int:
        """Taxa de clock do codec em Hz — converte timestamp RTP para tempo real."""
        _, rate = PAYLOAD_TYPES.get(self.payload_type, ("unknown", 8000))
        return rate

    @property
    def received(self) -> int:
        return self._received

    @property
    def extended_max_seq(self) -> int:
        """
        Sequence number estendido = ciclos × 65536 + max_seq_atual.

        Como o seq number tem apenas 16 bits e cicla, precisamos contar
        quantas voltas completas foram dadas para saber o total real.
        Ex: ciclos=1, max_seq=100 → estendido=65636
        """
        return self._seq_cycles + self._max_seq

    @property
    def expected(self) -> int:
        """
        Total de pacotes esperados desde o início da sessão.
        = (seq_mais_alto - seq_inicial + 1), contando wrap-arounds.
        """
        if not self._initialized:
            return 0
        return self.extended_max_seq - self._base_seq + 1

    @property
    def lost(self) -> int:
        """Pacotes perdidos = esperados - realmente recebidos."""
        return max(0, self.expected - self._received)

    @property
    def loss_percent(self) -> float:
        return (self.lost / self.expected * 100.0) if self.expected > 0 else 0.0

    @property
    def fraction_lost(self) -> int:
        """
        Fração de perda no ÚLTIMO INTERVALO de relatório (0–255).

        Escala: 0 = sem perda, 255 ≈ 100% de perda.
        É calculada apenas sobre o intervalo desde o último RR enviado,
        não sobre toda a sessão — isso permite detectar pioras recentes.
        """
        expected_interval = self.expected  - self._expected_prior
        received_interval = self._received - self._received_prior
        lost_interval = expected_interval - received_interval

        if expected_interval <= 0 or lost_interval <= 0:
            return 0
        # Multiplica por 256 (shift de 8 bits) para obter escala de byte inteiro
        return min(255, int((lost_interval << 8) / expected_interval))

    @property
    def jitter(self) -> float:
        """Jitter em unidades de amostras do codec."""
        return self._jitter

    @property
    def jitter_ms(self) -> float:
        """
        Jitter convertido para milissegundos.
        Fórmula: amostras / clock_rate * 1000
        Ex: 16 amostras a 8000 Hz = 2 ms de jitter
        """
        if self.clock_rate == 0:
            return 0.0
        return self._jitter / self.clock_rate * 1000.0

    # ------------------------------------------------------------------ #
    # Atualização com novo pacote RTP recebido                             #
    # ------------------------------------------------------------------ #

    def update(self, packet: RTPPacket) -> None:
        """Processa um novo pacote RTP, atualizando todos os contadores."""
        self.payload_type = packet.payload_type
        self.last_seen = packet.arrival_time

        if not self._initialized:
            # Primeiro pacote: define os valores de referência
            self._init_seq(packet.sequence_number)
            self._initialized = True

        self._update_seq(packet.sequence_number)
        self._update_jitter(packet)
        self._received += 1

    def _init_seq(self, seq: int) -> None:
        """Inicializa os contadores de sequência com o primeiro seq visto."""
        self._base_seq = seq
        self._max_seq  = seq
        self._bad_seq  = SEQ_MOD + 1  # reseta o "seq suspeito"
        self._seq_cycles = 0

    def _update_seq(self, seq: int) -> None:
        """
        Atualiza _max_seq e _seq_cycles com base no novo sequence number.

        O delta sem sinal (udelta) é calculado módulo 65536, o que garante
        que comparações como "seq está à frente de max_seq" funcionem mesmo
        com wrap-around.

        Três casos possíveis:
        1. udelta < MAX_DROPOUT  → pacote em ordem (ou lacuna pequena aceitável)
        2. udelta > SEQ_MOD - MAX_MISORDER → pacote chegou atrasado (reordenado)
        3. Demais → salto muito grande, suspeita de reinício do transmissor
        """
        # Delta sem sinal usando aritmética modular de 16 bits
        udelta = (seq - self._max_seq) % SEQ_MOD

        if udelta < MAX_DROPOUT:
            # Caso 1: pacote normal
            if seq < self._max_seq:
                # O seq number deu a volta (ex: foi de 65534 para 1)
                # Conta mais um ciclo completo de 65536
                self._seq_cycles += SEQ_MOD
            self._max_seq = seq

        elif udelta > SEQ_MOD - MAX_MISORDER:
            # Caso 2: pacote atrasado, chegou fora de ordem
            # Não atualiza max_seq para não prejudicar o cálculo de perda
            pass

        else:
            # Caso 3: salto enorme — transmissor reiniciou sem avisar?
            # Se o próximo pacote também for suspeito (seq == bad_seq+1),
            # confirma reinício e reinicializa os contadores.
            if seq == (self._bad_seq + 1) % SEQ_MOD:
                self._init_seq(seq)
            else:
                # Armazena como suspeito e aguarda confirmação
                self._bad_seq = (seq + 1) % SEQ_MOD

    def _update_jitter(self, packet: RTPPacket) -> None:
        """
        Implementa a fórmula de jitter interchegada da RFC 3550 §A.8:

            transit(i) = arrival_time(i) [em amostras] - rtp_timestamp(i)
            D(i)       = |transit(i) - transit(i-1)|
            J(i)       = J(i-1) + (D(i) - J(i-1)) / 16

        A divisão por 16 é um filtro de média exponencial: dá mais peso ao
        passado e suaviza variações bruscas. O jitter converge lentamente
        para cima quando a rede piora, e também lentamente para baixo quando
        melhora — o que é conservador e evita que o jitter buffer encolha rápido demais.

        Trabalhamos com DELTAS relativos ao primeiro pacote para evitar:
        - Overflow ao multiplicar time.monotonic() por 8000 Hz
        - Erros de comparação entre o clock de parede e o timestamp RTP de 32 bits
        """
        if self._ref_arrival is None:
            # Primeiro pacote: só guarda como referência, sem jitter calculável
            self._ref_arrival = packet.arrival_time
            self._ref_ts = packet.timestamp
            return

        # Delta de chegada em segundos → converte para amostras do codec
        arrival_delta = (packet.arrival_time - self._ref_arrival) * self.clock_rate

        # Delta de timestamp RTP com tratamento de wrap-around de 32 bits:
        # se o timestamp deu a volta, (novo - antigo) mod 2^32 dá o valor correto
        ts_delta = (packet.timestamp - self._ref_ts) % (1 << 32)

        # transit: diferença entre "quando deveria ter chegado" e "quando chegou"
        # Se a rede fosse perfeita e sem jitter, transit seria constante para todos os pacotes.
        # Variações no transit = jitter.
        transit = arrival_delta - ts_delta

        if self._last_transit is None:
            # Segundo pacote: inicializa last_transit mas ainda não calcula jitter
            self._last_transit = transit
            return

        # D(i) = variação de trânsito entre dois pacotes consecutivos
        d = abs(transit - self._last_transit)
        self._last_transit = transit

        # Filtro de média exponencial com fator 1/16 (RFC 3550 §A.8)
        self._jitter += (d - self._jitter) / 16.0

    # ------------------------------------------------------------------ #
    # Geração do ReportBlock para compor RTCP RR de saída                  #
    # ------------------------------------------------------------------ #

    def build_report_block(self, now: float) -> ReportBlock:
        """
        Constrói o ReportBlock que descreve como estamos recebendo esta fonte.

        Este bloco é incluído no RTCP RR que enviaríamos ao transmissor.
        O campo DLSR (Delay Since Last SR) indica quanto tempo passou desde
        que recebemos o último SR dele — o transmissor usa isso para calcular
        o RTT: RTT = NTP_agora - LSR - DLSR.
        """
        # DLSR em unidades de 1/65536 segundos
        dlsr = 0
        if self.last_sr_ntp_compact != 0:
            delay_s = now - self.last_sr_arrival
            dlsr = int(delay_s * 65536) & 0xFFFFFFFF

        block = ReportBlock(
            ssrc=self.ssrc,
            fraction_lost=self.fraction_lost,
            cumulative_lost=min(0x7FFFFF, self.lost),  # clamped a 24 bits (3 bytes)
            extended_highest_seq=self.extended_max_seq,
            jitter=int(self._jitter),                  # truncado para int sem sinal
            last_sr=self.last_sr_ntp_compact,
            delay_since_last_sr=dlsr,
        )

        # Atualiza os contadores "prior" para o próximo intervalo de relatório.
        # A diferença (expected - expected_prior) no próximo build_report_block()
        # dará a perda apenas neste intervalo, não no total.
        self._expected_prior = self.expected
        self._received_prior = self._received

        return block
