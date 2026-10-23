import asyncio
import logging
import os
import random
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import aiohttp
import discord
from discord.ext import commands, tasks

CAMINHO_BANCO = Path(__file__).resolve().parents[1] / "config" / "expedicoes.sqlite3"
UNBELIEVABOAT_API = "https://unbelievaboat.com/api/v1"
MAXIMO_DURACAO_MINUTOS = 10080
MAXIMO_NPCS_POR_EQUIPE = 3
TIERS = ("Pacífico", "Fácil", "Médio", "Difícil", "Inferno")
PODER_PARA_100_POR_TIER = (2, 4, 6, 8, 10)
MOEDA_POR_SERVIDOR = {
    958843879313014794: "dracmas",
    941036256526209183: "denários",
}
logger = logging.getLogger(__name__)


class UnbelievaBoatAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        cobranca_incerta: bool = False,
        status_code: int | None = None,
        sem_acesso: bool = False,
    ) -> None:
        super().__init__(message)
        self.cobranca_incerta = cobranca_incerta
        self.status_code = status_code
        self.sem_acesso = sem_acesso


class SaldoInsuficienteError(RuntimeError):
    def __init__(self, disponivel: int, custo: int) -> None:
        super().__init__()
        self.disponivel = disponivel
        self.custo = custo


async def obter_saldo_unbelievaboat(
    servidor_id: int,
    usuario_id: int,
) -> tuple[int, int]:
    token = os.getenv("UNBELIEVABOAT_API_TOKEN")
    if not token:
        raise UnbelievaBoatAPIError(
            "Configure UNBELIEVABOAT_API_TOKEN no ambiente do bot.",
            sem_acesso=True,
        )

    url = f"{UNBELIEVABOAT_API}/guilds/{servidor_id}/users/{usuario_id}"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as sessao:
            async with sessao.get(
                url,
                headers={"Authorization": token},
            ) as resposta:
                if resposta.status != 200:
                    detalhe = (
                        " Verifique se a aplicação do token está autorizada neste "
                        "servidor e possui permissão de Economy."
                        if resposta.status in (401, 403)
                        else ""
                    )
                    raise UnbelievaBoatAPIError(
                        f"Consulta de saldo retornou HTTP {resposta.status}.{detalhe}",
                        status_code=resposta.status,
                        sem_acesso=resposta.status in (401, 403),
                    )
                dados = await resposta.json()
    except UnbelievaBoatAPIError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as erro:
        raise UnbelievaBoatAPIError(
            "Não foi possível consultar o saldo no UnbelievaBoat."
        ) from erro

    if not isinstance(dados, dict):
        raise UnbelievaBoatAPIError("Resposta de saldo inválida do UnbelievaBoat.")
    cash = dados.get("cash")
    bank = dados.get("bank")
    if (
        not isinstance(cash, int)
        or isinstance(cash, bool)
        or not isinstance(bank, int)
        or isinstance(bank, bool)
        or cash < 0
        or bank < 0
    ):
        raise UnbelievaBoatAPIError("Resposta de saldo inválida do UnbelievaBoat.")
    return cash, bank


async def atualizar_saldo_unbelievaboat(
    servidor_id: int,
    usuario_id: int,
    cash: int,
    bank: int,
    motivo: str,
) -> None:
    token = os.getenv("UNBELIEVABOAT_API_TOKEN")
    if not token:
        raise UnbelievaBoatAPIError(
            "Configure UNBELIEVABOAT_API_TOKEN no ambiente do bot.",
            sem_acesso=True,
        )

    url = f"{UNBELIEVABOAT_API}/guilds/{servidor_id}/users/{usuario_id}"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as sessao:
            async with sessao.patch(
                url,
                headers={"Authorization": token},
                json={"cash": cash, "bank": bank, "reason": motivo},
            ) as resposta:
                if resposta.status != 200:
                    detalhe = (
                        " Verifique se a aplicação do token está autorizada neste "
                        "servidor e possui permissão de Economy."
                        if resposta.status in (401, 403)
                        else ""
                    )
                    raise UnbelievaBoatAPIError(
                        f"Atualização de saldo retornou HTTP {resposta.status}.{detalhe}",
                        cobranca_incerta=resposta.status >= 500,
                        status_code=resposta.status,
                        sem_acesso=resposta.status in (401, 403),
                    )
    except UnbelievaBoatAPIError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as erro:
        raise UnbelievaBoatAPIError(
            "A API não confirmou a atualização do saldo.",
            cobranca_incerta=True,
        ) from erro


@contextmanager
def conectar_banco() -> Iterator[sqlite3.Connection]:
    CAMINHO_BANCO.parent.mkdir(parents=True, exist_ok=True)
    banco = sqlite3.connect(CAMINHO_BANCO, timeout=10)
    banco.execute("PRAGMA foreign_keys = ON")
    try:
        yield banco
        banco.commit()
    except Exception:
        banco.rollback()
        raise
    finally:
        banco.close()


def normalizar(texto: str) -> str:
    decomposicao = unicodedata.normalize("NFKD", texto.casefold())
    return "".join(
        caractere
        for caractere in decomposicao
        if not unicodedata.combining(caractere)
    ).strip()


def obter_tier(tier: str) -> tuple[int, str] | None:
    procurado = normalizar(tier)
    for indice, nome in enumerate(TIERS, start=1):
        if normalizar(nome) == procurado:
            return indice, nome
    return None


def obter_nome_moeda(servidor_id: int) -> str:
    return MOEDA_POR_SERVIDOR.get(servidor_id, "moedas")


def inicializar_banco() -> None:
    with conectar_banco() as banco:
        banco.executescript(
            """
            CREATE TABLE IF NOT EXISTS expedicao_modelos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                servidor_id INTEGER NOT NULL,
                nome TEXT NOT NULL COLLATE NOCASE,
                duracao_minutos INTEGER NOT NULL,
                vagas INTEGER NOT NULL,
                recompensa_min INTEGER NOT NULL,
                recompensa_max INTEGER NOT NULL,
                UNIQUE (servidor_id, nome)
            );
            CREATE TABLE IF NOT EXISTS expedicoes_ativas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                servidor_id INTEGER NOT NULL,
                canal_id INTEGER NOT NULL,
                mensagem_id INTEGER,
                nome TEXT NOT NULL,
                termina_em REAL NOT NULL,
                vagas INTEGER NOT NULL,
                recompensa_min INTEGER NOT NULL,
                recompensa_max INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'ativa'
            );
            CREATE TABLE IF NOT EXISTS expedicao_participantes (
                expedicao_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                PRIMARY KEY (expedicao_id, usuario_id),
                FOREIGN KEY (expedicao_id) REFERENCES expedicoes_ativas(id)
            );
            CREATE TABLE IF NOT EXISTS expedicao_inventario (
                servidor_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                material TEXT NOT NULL,
                quantidade INTEGER NOT NULL,
                PRIMARY KEY (servidor_id, usuario_id, material)
            );
            CREATE TABLE IF NOT EXISTS expedicao_mercenarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                servidor_id INTEGER NOT NULL,
                nome TEXT NOT NULL COLLATE NOCASE,
                combate INTEGER NOT NULL CHECK (combate BETWEEN 1 AND 10),
                sorte INTEGER NOT NULL CHECK (sorte BETWEEN 1 AND 10),
                exploracao INTEGER NOT NULL CHECK (exploracao BETWEEN 1 AND 10),
                custo INTEGER NOT NULL CHECK (custo >= 0),
                status TEXT NOT NULL DEFAULT 'disponivel'
                    CHECK (status IN ('disponivel', 'em_expedicao', 'morto')),
                UNIQUE (servidor_id, nome)
            );
            CREATE TABLE IF NOT EXISTS expedicao_objetivos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                servidor_id INTEGER NOT NULL,
                numero INTEGER,
                nome TEXT NOT NULL COLLATE NOCASE,
                dificuldade INTEGER NOT NULL CHECK (dificuldade BETWEEN 1 AND 10),
                duracao_minutos INTEGER NOT NULL CHECK (duracao_minutos > 0),
                tamanho_equipe INTEGER NOT NULL CHECK (tamanho_equipe BETWEEN 1 AND 10),
                custo_base INTEGER NOT NULL CHECK (custo_base >= 0),
                recompensa TEXT NOT NULL,
                UNIQUE (servidor_id, nome)
            );
            CREATE TABLE IF NOT EXISTS expedicao_viagens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                servidor_id INTEGER NOT NULL,
                numero INTEGER NOT NULL,
                contratante_id INTEGER NOT NULL,
                canal_id INTEGER NOT NULL,
                objetivo_nome TEXT NOT NULL,
                dificuldade INTEGER NOT NULL,
                recompensa TEXT NOT NULL,
                termina_em REAL NOT NULL,
                custo INTEGER NOT NULL,
                chance_sucesso INTEGER NOT NULL,
                duracao_segundos INTEGER NOT NULL DEFAULT 0,
                manual_pendente INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ativa'
                    CHECK (status IN ('ativa', 'sucesso', 'fracasso'))
            );
            CREATE TABLE IF NOT EXISTS expedicao_sequencias (
                    servidor_id INTEGER PRIMARY KEY,
                    ultimo_numero INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uma_viagem_ativa_por_contratante
                ON expedicao_viagens (servidor_id, contratante_id)
                WHERE status = 'ativa';
            CREATE TABLE IF NOT EXISTS expedicao_viagem_mercenarios (
                viagem_id INTEGER NOT NULL,
                mercenario_id INTEGER NOT NULL,
                nome TEXT NOT NULL,
                combate INTEGER NOT NULL,
                sorte INTEGER NOT NULL,
                exploracao INTEGER NOT NULL,
                morreu INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (viagem_id, mercenario_id),
                FOREIGN KEY (viagem_id) REFERENCES expedicao_viagens(id),
                FOREIGN KEY (mercenario_id) REFERENCES expedicao_mercenarios(id)
            );
            CREATE TABLE IF NOT EXISTS expedicao_mercenario_ranking (
                mercenario_id INTEGER PRIMARY KEY,
                servidor_id INTEGER NOT NULL,
                contratacoes INTEGER NOT NULL DEFAULT 0,
                missoes_concluidas INTEGER NOT NULL DEFAULT 0,
                sucessos INTEGER NOT NULL DEFAULT 0,
                baixas INTEGER NOT NULL DEFAULT 0,
                itens_looteados INTEGER NOT NULL DEFAULT 0,
                pontos INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (mercenario_id)
                    REFERENCES expedicao_mercenarios(id)
            );
            """
        )
        colunas_viagens = {
            linha[1]
            for linha in banco.execute("PRAGMA table_info(expedicao_viagens)")
        }
        if "numero" not in colunas_viagens:
            banco.execute("ALTER TABLE expedicao_viagens ADD COLUMN numero INTEGER")
        servidores_com_viagens = banco.execute(
            """
            SELECT DISTINCT servidor_id FROM expedicao_viagens
            WHERE numero IS NULL ORDER BY servidor_id
            """
        ).fetchall()
        for (servidor_id,) in servidores_com_viagens:
            ultimo_numero = banco.execute(
                """
                SELECT COALESCE(MAX(numero), 0) FROM expedicao_viagens
                WHERE servidor_id = ?
                """,
                (servidor_id,),
            ).fetchone()[0]
            viagens_sem_numero = banco.execute(
                """
                SELECT id FROM expedicao_viagens
                WHERE servidor_id = ? AND numero IS NULL ORDER BY id
                """,
                (servidor_id,),
            ).fetchall()
            for numero, (viagem_id,) in enumerate(
                viagens_sem_numero, start=ultimo_numero + 1
            ):
                banco.execute(
                    "UPDATE expedicao_viagens SET numero = ? WHERE id = ?",
                    (numero, viagem_id),
                )
        banco.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS numero_viagem_por_servidor
            ON expedicao_viagens (servidor_id, numero)
            """
        )
        sequencias_existentes = banco.execute(
            """
            SELECT servidor_id, MAX(numero)
            FROM expedicao_viagens
            GROUP BY servidor_id
            """
        ).fetchall()
        for servidor_id, ultimo_numero in sequencias_existentes:
            banco.execute(
                """
                INSERT INTO expedicao_sequencias (servidor_id, ultimo_numero)
                VALUES (?, ?)
                ON CONFLICT (servidor_id)
                DO UPDATE SET ultimo_numero = MAX(
                    ultimo_numero, excluded.ultimo_numero
                )
                """,
                (servidor_id, ultimo_numero),
            )
        if "duracao_segundos" not in colunas_viagens:
            banco.execute(
                "ALTER TABLE expedicao_viagens "
                "ADD COLUMN duracao_segundos INTEGER NOT NULL DEFAULT 0"
            )
        if "manual_pendente" not in colunas_viagens:
            banco.execute(
                "ALTER TABLE expedicao_viagens "
                "ADD COLUMN manual_pendente INTEGER NOT NULL DEFAULT 0"
            )
        colunas = {
            linha[1]
            for linha in banco.execute("PRAGMA table_info(expedicao_mercenarios)")
        }
        if "tier" not in colunas:
            banco.execute(
                "ALTER TABLE expedicao_mercenarios "
                "ADD COLUMN tier TEXT NOT NULL DEFAULT 'Pacífico'"
            )
        banco.execute(
            "UPDATE expedicao_mercenarios SET tier = 'Pacífico' WHERE tier = 'Pudim'"
        )
        if "removido" not in colunas:
            banco.execute(
                "ALTER TABLE expedicao_mercenarios "
                "ADD COLUMN removido INTEGER NOT NULL DEFAULT 0"
            )
        colunas_objetivos = {
            linha[1]
            for linha in banco.execute("PRAGMA table_info(expedicao_objetivos)")
        }
        if "numero" not in colunas_objetivos:
            banco.execute("ALTER TABLE expedicao_objetivos ADD COLUMN numero INTEGER")
        servidores = banco.execute(
            "SELECT DISTINCT servidor_id FROM expedicao_objetivos WHERE numero IS NULL"
        ).fetchall()
        for (servidor_id,) in servidores:
            objetivos = banco.execute(
                """
                SELECT id FROM expedicao_objetivos
                WHERE servidor_id = ? ORDER BY id
                """,
                (servidor_id,),
            ).fetchall()
            for numero, (objetivo_id,) in enumerate(objetivos, start=1):
                banco.execute(
                    "UPDATE expedicao_objetivos SET numero = ? WHERE id = ?",
                    (numero, objetivo_id),
                )
        banco.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS numero_objetivo_por_servidor
            ON expedicao_objetivos (servidor_id, numero)
            """
        )
def garantir_catalogo(servidor_id: int) -> None:
    with conectar_banco() as banco:
        banco.execute(
            """
            INSERT OR IGNORE INTO expedicao_mercenario_ranking
                (mercenario_id, servidor_id)
            SELECT id, servidor_id FROM expedicao_mercenarios
            WHERE servidor_id = ?
            """,
            (servidor_id,),
        )


def eh_moderador(membro: discord.Member) -> bool:
    permissoes = membro.guild_permissions
    if permissoes.administrator or permissoes.manage_messages:
        return True

    nomes_cargos = []
    for cargo in membro.roles:
        nome = normalizar(cargo.name).replace("_", " ").replace("-", " ")
        nomes_cargos.extend(nome.split())
    return any(nome.startswith(("mod", "adm")) for nome in nomes_cargos)


def formatar_duracao(segundos: int) -> str:
    horas, resto = divmod(segundos, 3600)
    minutos = resto // 60
    if horas:
        return f"{horas}h {minutos}min"
    return f"{minutos}min"


def calcular_chance_sucesso(poder_total: int, tier: int) -> int:
    poder_para_100 = PODER_PARA_100_POR_TIER[tier - 1]
    return max(5, min(100, poder_total * 100 // poder_para_100))


def calcular_custo(equipe: list[tuple]) -> int:
    return sum(int(npc["custo"]) for npc in equipe)


def calcular_duracao(duracao_minutos: int, exploracao_total: int, tier: int) -> int:
    exploracao_necessaria = PODER_PARA_100_POR_TIER[tier - 1]
    razao_exploracao = exploracao_total / exploracao_necessaria
    multiplicador = max(0.5, min(2.0, 2 - razao_exploracao))
    return max(60, int(duracao_minutos * 60 * multiplicador))


def calcular_chance_baixa(
    poder: int,
    exploracao: int,
    tier: int,
    sucesso: bool,
) -> int:
    penalidade_fracasso = 0 if sucesso else 10
    return max(
        1,
        min(65, 4 + tier * 8 - poder * 4 - exploracao * 2 + penalidade_fracasso),
    )


def calcular_bonus_loot(sorte_total: int, tier: int) -> float:
    progresso = calcular_progresso_atributo(sorte_total, tier)
    return min(4.0, progresso * 2)


def calcular_progresso_atributo(total: int, tier: int) -> float:
    necessario = PODER_PARA_100_POR_TIER[tier - 1]
    return max(0.0, total / necessario)


def calcular_pesos_loot(sorte_total: int, tier: int, quantidade_itens: int) -> list[float]:
    progresso = calcular_progresso_atributo(sorte_total, tier)
    expoente = max(-1.0, 3 - progresso * 2)
    return [1 / (indice + 1) ** expoente for indice in range(quantidade_itens)]


def calcular_quantidade_loot(sorte_total: int, tier: int) -> int:
    bonus = calcular_bonus_loot(sorte_total, tier)
    bonus_garantido = int(bonus)
    bonus_fracionario = bonus - bonus_garantido
    if bonus_garantido < 4 and random.random() < bonus_fracionario:
        bonus_garantido += 1
    return 1 + bonus_garantido


def buscar_npcs(servidor_id: int, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    marcadores = ",".join("?" for _ in ids)
    with conectar_banco() as banco:
        banco.row_factory = sqlite3.Row
        return banco.execute(
            f"""
            SELECT id, nome, combate, sorte, exploracao, custo, tier, status
            FROM expedicao_mercenarios
            WHERE servidor_id = ? AND removido = 0 AND id IN ({marcadores})
            ORDER BY nome COLLATE NOCASE
            """,
            (servidor_id, *ids),
        ).fetchall()


class SeletorEquipe(discord.ui.Select):
    def __init__(self, view: "ContratarExpedicaoView", npcs: list[dict]) -> None:
        moeda = obter_nome_moeda(view.servidor_id)
        opcoes = [
            discord.SelectOption(
                label=f"{npc['nome']} · {npc['tier']}",
                value=str(npc["id"]),
                description=(
                    f"Poder {npc['combate']} · Sorte {npc['sorte']} · "
                    f"Exploração {npc['exploracao']} · {npc['custo']} {moeda}"
                ),
            )
            for npc in npcs
        ]
        super().__init__(
            placeholder="Escolha de 1 a 3 exploradores",
            min_values=1,
            max_values=min(MAXIMO_NPCS_POR_EQUIPE, len(opcoes)),
            options=opcoes,
        )
        self.view_owner = view

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view_owner.npc_ids = [int(valor) for valor in self.values]
        self.view_owner.confirmar.disabled = not self.view_owner.npc_ids
        await interaction.response.edit_message(
            embed=self.view_owner.criar_embed(),
            view=self.view_owner,
        )


class ContratarExpedicaoView(discord.ui.View):
    def __init__(
        self,
        cog: "ExpedicoesCog",
        autor_id: int,
        servidor_id: int,
        canal_id: int,
        objetivo: sqlite3.Row,
        npcs: list[dict],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.autor_id = autor_id
        self.servidor_id = servidor_id
        self.canal_id = canal_id
        self.objetivo = objetivo
        self.npcs = npcs
        self.npc_ids: list[int] = []
        self.mensagem: discord.Message | None = None
        self.add_item(SeletorEquipe(self, npcs))
        self.confirmar.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor_id:
            await interaction.response.send_message(
                "Só quem iniciou esta contratação pode escolher a equipe.",
                ephemeral=True,
            )
            return False
        return True

    def criar_embed(self) -> discord.Embed:
        equipe = buscar_npcs(self.servidor_id, self.npc_ids)
        moeda = obter_nome_moeda(self.servidor_id)
        tier = TIERS[int(self.objetivo["dificuldade"]) - 1]
        embed = discord.Embed(
            title=f"Contratar equipe · {self.objetivo['nome']}",
            description=(
                f"**Tier:** {tier}\n"
                f"**Recompensas possíveis:** {self.objetivo['recompensa']}\n"
                f"**Duração base:** {self.objetivo['duracao_minutos']} min\n"
                "Escolha até três exploradores. O preço é calculado pela equipe."
            ),
            color=discord.Color.from_rgb(61, 125, 105),
        )
        if equipe:
            poder = sum(npc["combate"] for npc in equipe)
            sorte = sum(npc["sorte"] for npc in equipe)
            exploracao = sum(npc["exploracao"] for npc in equipe)
            custo = calcular_custo(equipe)
            duracao = calcular_duracao(
                self.objetivo["duracao_minutos"],
                exploracao,
                int(self.objetivo["dificuldade"]),
            )
            chance = calcular_chance_sucesso(poder, int(self.objetivo["dificuldade"]))
            loot_medio = 1 + calcular_bonus_loot(
                sorte,
                int(self.objetivo["dificuldade"]),
            )
            nomes = "\n".join(
                f"**{npc['nome']}** ({npc['tier']}) · Poder {npc['combate']} · "
                f"Sorte {npc['sorte']} · Exploração {npc['exploracao']} · "
                f"{npc['custo']} {moeda}"
                for npc in equipe
            )
            embed.add_field(name="Equipe escolhida", value=nomes, inline=False)
            embed.add_field(name="Custo total", value=f"{custo} {moeda}", inline=True)
            embed.add_field(name="Retorno estimado", value=formatar_duracao(duracao), inline=True)
            embed.add_field(name="Chance de sucesso", value=f"{chance}%", inline=True)
            embed.add_field(
                name="Loot estimado",
                value=f"x{loot_medio:.1f} itens por sucesso (máximo x5)",
                inline=True,
            )
            embed.add_field(
                name="Atributos da equipe",
                value=f"Poder {poder} · Sorte {sorte} · Exploração {exploracao}",
                inline=False,
            )
        embed.set_footer(
            text=(
                "O custo é cobrado pela API; sem acesso, a expedição aguarda cobrança "
                "manual e aprovação da moderação."
            )
        )
        return embed

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.mensagem is not None:
            try:
                await self.mensagem.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Confirmar contratação",
        style=discord.ButtonStyle.success,
        row=1,
    )
    async def confirmar(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not self.npc_ids:
            await interaction.response.send_message(
                "Escolha pelo menos um mercenário neste servidor.",
                ephemeral=True,
            )
            return
        resultado = self.cog.iniciar_viagem(
            servidor_id=self.servidor_id,
            contratante_id=self.autor_id,
            canal_id=self.canal_id,
            objetivo=self.objetivo,
            npc_ids=self.npc_ids,
        )
        if resultado[0] != "iniciada":
            mensagens = {
                "indisponivel": "Um dos exploradores escolhidos não está mais disponível.",
                "viagem_ativa": "Você já tem uma expedição em andamento.",
            }
            await interaction.response.send_message(
                mensagens.get(resultado[0], "Não foi possível iniciar a expedição."),
                ephemeral=True,
            )
            return

        viagem_id, numero_viagem, termina_em, custo, chance = resultado[1:]
        try:
            await self.cog.cobrar_expedicao(
                servidor_id=self.servidor_id,
                contratante_id=self.autor_id,
                viagem_id=numero_viagem,
                custo=custo,
            )
        except SaldoInsuficienteError as erro:
            try:
                self.cog.cancelar_viagem_sem_cobranca(viagem_id)
            except sqlite3.Error:
                logger.exception(
                    "Não foi possível cancelar a expedição %s por saldo insuficiente.",
                    viagem_id,
                )
                await interaction.response.send_message(
                    "O saldo é insuficiente, mas houve uma falha ao cancelar o registro "
                    "da expedição. Avise a moderação antes de tentar novamente.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"Saldo insuficiente no UnbelievaBoat: custo **{erro.custo}**, "
                f"disponível **{erro.disponivel}** (cash + bank). "
                "A expedição não foi iniciada.",
                ephemeral=True,
            )
            return
        except UnbelievaBoatAPIError as erro:
            logger.error(
                "Falha na cobrança da expedição %s para usuário %s: %s",
                viagem_id,
                self.autor_id,
                erro,
            )
            if erro.sem_acesso:
                try:
                    self.cog.marcar_viagem_pendente_manual(viagem_id)
                except sqlite3.Error:
                    logger.exception(
                        "Não foi possível marcar a expedição %s para cobrança manual.",
                        viagem_id,
                    )
                    try:
                        self.cog.cancelar_viagem_sem_cobranca(viagem_id)
                    except sqlite3.Error:
                        logger.exception(
                            "Não foi possível cancelar a expedição %s após falha "
                            "ao registrar a cobrança manual.",
                            viagem_id,
                        )
                        await interaction.response.send_message(
                            "A cobrança não foi validada e houve uma falha ao registrar "
                            "ou cancelar a expedição. Avise a moderação antes de tentar "
                            "novamente.",
                            ephemeral=True,
                        )
                        return
                    await interaction.response.send_message(
                        "A integração monetária está sem acesso e não foi possível "
                        "registrar a solicitação manual. A expedição foi cancelada.",
                        ephemeral=True,
                    )
                    return

                self.stop()
                await interaction.response.edit_message(
                    content=(
                        f"Expedição **#{numero_viagem}** aguardando aprovação da moderação. "
                        f"A cobrança de **{custo} "
                        f"{obter_nome_moeda(self.servidor_id)}** deve ser feita "
                        "manualmente no UnbelievaBoat antes de um mod/ADM usar "
                        f"`!expedicao aprovar {numero_viagem}`. A duração só começa após "
                        "a aprovação."
                    ),
                    embed=None,
                    view=None,
                )
                return
            try:
                self.cog.cancelar_viagem_sem_cobranca(viagem_id)
            except sqlite3.Error:
                logger.exception(
                    "Não foi possível desfazer a expedição %s após erro na cobrança.",
                    viagem_id,
                )
                await interaction.response.send_message(
                    "Ocorreu uma falha ao cobrar e ao cancelar o registro da expedição. "
                    "Avise a moderação antes de tentar novamente.",
                    ephemeral=True,
                )
                return
            if erro.cobranca_incerta:
                mensagem = (
                    "A API do UnbelievaBoat não confirmou a cobrança e a expedição foi "
                    "cancelada. Confira seu saldo antes de tentar novamente."
                )
            elif erro.status_code == 403:
                mensagem = (
                    "O UnbelievaBoat recusou a cobrança por falta de permissão da "
                    "integração neste servidor. A expedição foi cancelada sem iniciar; "
                    "peça à moderação para conferir a autorização da aplicação e a "
                    "permissão Economy."
                )
            else:
                mensagem = (
                    "Não foi possível validar ou cobrar o saldo no UnbelievaBoat; "
                    "a expedição foi cancelada sem iniciar."
                )
            await interaction.response.send_message(mensagem, ephemeral=True)
            return

        self.stop()
        moeda = obter_nome_moeda(self.servidor_id)
        await interaction.response.edit_message(
            content=(
                f"Expedição **#{numero_viagem}** contratada. **{custo} {moeda}** "
                f"foram cobrados do UnbelievaBoat. Chance de sucesso: **{chance}%**. "
                f"Retorno previsto: <t:{int(termina_em)}:R>."
            ),
            embed=None,
            view=None,
        )


class ExpedicoesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        inicializar_banco()
        self.verificar_conclusoes.start()

    def cog_unload(self) -> None:
        self.verificar_conclusoes.cancel()

    async def cobrar_expedicao(
        self,
        servidor_id: int,
        contratante_id: int,
        viagem_id: int,
        custo: int,
    ) -> None:
        if custo < 0:
            raise ValueError("O custo da expedição não pode ser negativo.")
        if custo == 0:
            return

        cash, bank = await obter_saldo_unbelievaboat(
            servidor_id,
            contratante_id,
        )
        disponivel = cash + bank
        if disponivel < custo:
            raise SaldoInsuficienteError(disponivel, custo)

        debito_cash = min(cash, custo)
        debito_bank = custo - debito_cash
        await atualizar_saldo_unbelievaboat(
            servidor_id=servidor_id,
            usuario_id=contratante_id,
            cash=-debito_cash,
            bank=-debito_bank,
            motivo=f"Contratação da expedição #{viagem_id}",
        )

    def marcar_viagem_pendente_manual(self, viagem_id: int) -> None:
        with conectar_banco() as banco:
            cursor = banco.execute(
                """
                UPDATE expedicao_viagens
                SET manual_pendente = 1
                WHERE id = ? AND status = 'ativa' AND manual_pendente = 0
                """,
                (viagem_id,),
            )
            if cursor.rowcount != 1:
                raise sqlite3.Error(
                    f"A expedição {viagem_id} não está disponível para cobrança manual."
                )

    def cancelar_viagem_sem_cobranca(self, viagem_id: int) -> None:
        with conectar_banco() as banco:
            banco.execute("BEGIN IMMEDIATE")
            viagem = banco.execute(
                "SELECT status FROM expedicao_viagens WHERE id = ?",
                (viagem_id,),
            ).fetchone()
            if viagem is None or viagem[0] != "ativa":
                banco.rollback()
                return

            mercenarios = banco.execute(
                """
                SELECT mercenario_id FROM expedicao_viagem_mercenarios
                WHERE viagem_id = ?
                """,
                (viagem_id,),
            ).fetchall()
            for (mercenario_id,) in mercenarios:
                banco.execute(
                    """
                    UPDATE expedicao_mercenarios
                    SET status = 'disponivel'
                    WHERE id = ? AND status = 'em_expedicao'
                    """,
                    (mercenario_id,),
                )
                banco.execute(
                    """
                    UPDATE expedicao_mercenario_ranking
                    SET contratacoes = contratacoes - 1
                    WHERE mercenario_id = ? AND contratacoes > 0
                    """,
                    (mercenario_id,),
                )
                banco.execute(
                    """
                    DELETE FROM expedicao_mercenario_ranking
                    WHERE mercenario_id = ? AND contratacoes = 0
                        AND missoes_concluidas = 0 AND sucessos = 0 AND baixas = 0
                        AND itens_looteados = 0 AND pontos = 0
                    """,
                    (mercenario_id,),
                )
            banco.execute(
                "DELETE FROM expedicao_viagem_mercenarios WHERE viagem_id = ?",
                (viagem_id,),
            )
            banco.execute("DELETE FROM expedicao_viagens WHERE id = ?", (viagem_id,))

    @tasks.loop(seconds=15)
    async def verificar_conclusoes(self) -> None:
        agora = datetime.now(timezone.utc).timestamp()
        with conectar_banco() as banco:
            ids = banco.execute(
                """
                SELECT id FROM expedicao_viagens
                WHERE status = 'ativa' AND manual_pendente = 0 AND termina_em <= ?
                """,
                (agora,),
            ).fetchall()
        for (viagem_id,) in ids:
            await self.concluir_viagem(viagem_id)

    @verificar_conclusoes.before_loop
    async def aguardar_bot_pronto(self) -> None:
        await self.bot.wait_until_ready()

    def iniciar_viagem(
        self,
        servidor_id: int,
        contratante_id: int,
        canal_id: int,
        objetivo: sqlite3.Row,
        npc_ids: list[int],
    ) -> tuple:
        if not 1 <= len(npc_ids) <= MAXIMO_NPCS_POR_EQUIPE:
            return ("indisponivel",)
        tier_objetivo = int(objetivo["dificuldade"])
        agora = datetime.now(timezone.utc).timestamp()

        with conectar_banco() as banco:
            banco.row_factory = sqlite3.Row
            banco.execute("BEGIN IMMEDIATE")
            viagem_ativa = banco.execute(
                """
                SELECT 1 FROM expedicao_viagens
                WHERE servidor_id = ? AND contratante_id = ? AND status = 'ativa'
                """,
                (servidor_id, contratante_id),
            ).fetchone()
            if viagem_ativa:
                banco.rollback()
                return ("viagem_ativa",)

            marcadores = ",".join("?" for _ in npc_ids)
            equipe = banco.execute(
                f"""
                SELECT id, nome, combate, sorte, exploracao, custo, tier
                FROM expedicao_mercenarios
                WHERE servidor_id = ? AND removido = 0 AND status = 'disponivel'
                    AND id IN ({marcadores})
                """,
                (servidor_id, *npc_ids),
            ).fetchall()
            if len(equipe) != len(set(npc_ids)):
                banco.rollback()
                return ("indisponivel",)

            if any(obter_tier(npc["tier"])[0] > tier_objetivo for npc in equipe):
                banco.rollback()
                return ("indisponivel",)

            numero_viagem = banco.execute(
                """
                SELECT COALESCE(
                    (SELECT ultimo_numero FROM expedicao_sequencias
                     WHERE servidor_id = ?),
                    0
                ) + 1
                """,
                (servidor_id,),
            ).fetchone()[0]
            banco.execute(
                """
                INSERT INTO expedicao_sequencias (servidor_id, ultimo_numero)
                VALUES (?, ?)
                ON CONFLICT (servidor_id)
                DO UPDATE SET ultimo_numero = excluded.ultimo_numero
                """,
                (servidor_id, numero_viagem),
            )
            poder = sum(npc["combate"] for npc in equipe)
            exploracao = sum(npc["exploracao"] for npc in equipe)
            custo = calcular_custo(equipe)
            chance = calcular_chance_sucesso(poder, tier_objetivo)
            duracao_segundos = calcular_duracao(
                int(objetivo["duracao_minutos"]),
                exploracao,
                tier_objetivo,
            )
            termina_em = agora + duracao_segundos
            cursor = banco.execute(
                """
                INSERT INTO expedicao_viagens
                    (servidor_id, numero, contratante_id, canal_id, objetivo_nome,
                    dificuldade, recompensa, termina_em, custo, chance_sucesso,
                    duracao_segundos)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    servidor_id,
                    numero_viagem,
                    contratante_id,
                    canal_id,
                    objetivo["nome"],
                    tier_objetivo,
                    objetivo["recompensa"],
                    termina_em,
                    custo,
                    chance,
                    duracao_segundos,
                ),
            )
            viagem_id = cursor.lastrowid
            for npc in equipe:
                banco.execute(
                    """
                    INSERT INTO expedicao_viagem_mercenarios
                        (viagem_id, mercenario_id, nome, combate, sorte, exploracao)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        viagem_id,
                        npc["id"],
                        npc["nome"],
                        npc["combate"],
                        npc["sorte"],
                        npc["exploracao"],
                    ),
                )
                banco.execute(
                    "UPDATE expedicao_mercenarios SET status = 'em_expedicao' WHERE id = ?",
                    (npc["id"],),
                )
                banco.execute(
                    """
                    INSERT INTO expedicao_mercenario_ranking
                        (mercenario_id, servidor_id, contratacoes)
                    VALUES (?, ?, 1)
                    ON CONFLICT (mercenario_id)
                    DO UPDATE SET contratacoes = contratacoes + 1
                    """,
                    (npc["id"], servidor_id),
                )

        return "iniciada", viagem_id, numero_viagem, termina_em, custo, chance

    async def concluir_viagem(self, viagem_id: int) -> None:
        with conectar_banco() as banco:
            banco.row_factory = sqlite3.Row
            banco.execute("BEGIN IMMEDIATE")
            viagem = banco.execute(
                """
                SELECT * FROM expedicao_viagens
                WHERE id = ? AND status = 'ativa' AND manual_pendente = 0
                """,
                (viagem_id,),
            ).fetchone()
            if viagem is None:
                banco.rollback()
                return
            equipe = banco.execute(
                "SELECT * FROM expedicao_viagem_mercenarios WHERE viagem_id = ?",
                (viagem_id,),
            ).fetchall()
            sucesso = random.randint(1, 100) <= viagem["chance_sucesso"]
            tier = int(viagem["dificuldade"])
            baixas = []
            baixas_ids = set()
            sobreviventes = []
            for npc in equipe:
                chance_morte = calcular_chance_baixa(
                    npc["combate"],
                    npc["exploracao"],
                    tier,
                    sucesso,
                )
                morreu = random.randint(1, 100) <= chance_morte
                banco.execute(
                    """
                    UPDATE expedicao_viagem_mercenarios SET morreu = ?
                    WHERE viagem_id = ? AND mercenario_id = ?
                    """,
                    (int(morreu), viagem_id, npc["mercenario_id"]),
                )
                banco.execute(
                    "UPDATE expedicao_mercenarios SET status = ? WHERE id = ?",
                    ("morto" if morreu else "disponivel", npc["mercenario_id"]),
                )
                if morreu:
                    baixas_ids.add(npc["mercenario_id"])
                (baixas if morreu else sobreviventes).append(npc["nome"])

            sorte_total = sum(npc["sorte"] for npc in equipe)
            achados: dict[str, int] = {}
            if sucesso:
                recompensas = [
                    item.strip()
                    for item in viagem["recompensa"].split(",")
                    if item.strip()
                ]
                quantidade = calcular_quantidade_loot(sorte_total, tier)
                pesos_loot = calcular_pesos_loot(
                    sorte_total,
                    tier,
                    len(recompensas),
                )
                for item in random.choices(recompensas, weights=pesos_loot, k=quantidade):
                    achados[item] = achados.get(item, 0) + 1

            itens_looteados = sum(achados.values())
            pontos_loot = sum(
                quantidade * (recompensas.index(item) + 1)
                for item, quantidade in achados.items()
            ) if sucesso else 0
            for npc in equipe:
                banco.execute(
                    """
                    INSERT INTO expedicao_mercenario_ranking
                        (mercenario_id, servidor_id)
                    VALUES (?, ?)
                    ON CONFLICT (mercenario_id) DO NOTHING
                    """,
                    (npc["mercenario_id"], viagem["servidor_id"]),
                )
                banco.execute(
                    """
                    UPDATE expedicao_mercenario_ranking
                    SET missoes_concluidas = missoes_concluidas + 1,
                        sucessos = sucessos + ?,
                        baixas = baixas + ?,
                        itens_looteados = itens_looteados + ?,
                        pontos = pontos + ?
                    WHERE mercenario_id = ?
                    """,
                    (
                        int(sucesso),
                        int(npc["mercenario_id"] in baixas_ids),
                        itens_looteados,
                        pontos_loot,
                        npc["mercenario_id"],
                    ),
                )

            status = "sucesso" if sucesso else "fracasso"
            banco.execute(
                "UPDATE expedicao_viagens SET status = ? WHERE id = ?",
                (status, viagem_id),
            )

        canal = self.bot.get_channel(viagem["canal_id"])
        if canal is None:
            try:
                canal = await self.bot.fetch_channel(viagem["canal_id"])
            except discord.HTTPException:
                return

        resultado = "Objetivo concluído" if sucesso else "A equipe não conseguiu concluir o objetivo"
        embed = discord.Embed(
            title=f"Relatório da expedição #{viagem['numero']}: {viagem['objetivo_nome']}",
            description=f"<@{viagem['contratante_id']}> · **{resultado}**",
            color=discord.Color.green() if sucesso else discord.Color.red(),
        )
        if achados:
            embed.add_field(
                name="Loot",
                value="\n".join(f"**{nome}:** x{quantidade}" for nome, quantidade in achados.items()),
                inline=False,
            )
        else:
            embed.add_field(name="Loot", value="Nenhum item encontrado.", inline=False)
        embed.add_field(
            name="Equipe sobrevivente",
            value=", ".join(sobreviventes) if sobreviventes else "Ninguém retornou.",
            inline=True,
        )
        embed.add_field(
            name="Baixas permanentes",
            value=", ".join(baixas) if baixas else "Nenhuma",
            inline=True,
        )
        await canal.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def exigir_moderacao(self, ctx: commands.Context) -> bool:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.reply("Este comando só pode ser usado em um servidor.")
            return False
        if not eh_moderador(ctx.author):
            await ctx.reply("Somente mods ou ADMs podem configurar expedições.")
            return False
        return True

    @commands.group(
        name="expedicao",
        aliases=["expedições", "expedicoes"],
        invoke_without_command=True,
    )
    async def expedicao(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="Comandos de expedição",
            description="Consulte exploradores, objetivos e suas viagens.",
            color=discord.Color.from_rgb(61, 125, 105),
        )
        embed.add_field(
            name="Jogadores",
            value=(
                "`!expedicao npc` · ver exploradores disponíveis\n"
                "`!expedicao objetivos` · listar objetivos cadastrados\n"
                "`!expedicao contratar <número>` · escolher um objetivo e montar a equipe\n"
                "`!expedicao status` · consultar sua expedição ativa\n"
                "`!expedicao ranking` · ver o ranking dos exploradores"
            ),
            inline=False,
        )
        embed.add_field(
            name="Mods/ADMs",
            value=(
                "`!expedicao criar <tier> <minutos> \"<recompensas>\" <nome>` · "
                "cadastrar objetivo\n"
                "`!expedicao remover <número>` · remover objetivo\n"
                "`!expedicao npc adicionar <tier> <poder> <sorte> <exploração> "
                "<custo> <nome>` · "
                "cadastrar NPC\n"
                "`!expedicao npc editar <custo> <nome>` · editar o custo de um NPC\n"
                "`!expedicao npc editar_status <tier> <poder> <sorte> "
                "<exploração> <nome>` · editar os atributos de um NPC\n"
                "`!expedicao npc remover <nome>` · remover NPC\n"
                "`!expedicao pendentes` · listar cobranças manuais aguardando moderação\n"
                "`!expedicao aprovar <id>` · iniciar após cobrar manualmente\n"
                "`!expedicao rejeitar <id>` · cancelar uma solicitação pendente"
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                "Tiers: Pacífico, Fácil, Médio, Difícil ou Inferno. "
                "Duração: 1 a 10080 minutos. Separe as recompensas por vírgulas."
            )
        )
        await ctx.reply(embed=embed)

    @expedicao.group(name="npc", invoke_without_command=True)
    async def listar_npcs(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply("Consulte o catálogo dentro de um servidor.")
            return
        garantir_catalogo(ctx.guild.id)
        with conectar_banco() as banco:
            banco.row_factory = sqlite3.Row
            npcs = banco.execute(
                """
                SELECT nome, tier, combate, sorte, exploracao, custo, status
                FROM expedicao_mercenarios
                WHERE servidor_id = ? AND removido = 0
                ORDER BY CASE tier
                    WHEN 'Pacífico' THEN 1 WHEN 'Fácil' THEN 2 WHEN 'Médio' THEN 3
                    WHEN 'Difícil' THEN 4 ELSE 5 END, nome COLLATE NOCASE
                """,
                (ctx.guild.id,),
            ).fetchall()
        embed = discord.Embed(
            title="Exploradores disponíveis",
            description=(
                "Poder, sorte e exploração podem variar de 1 a 6. "
                "Exploradores mortos não retornam."
            ),
            color=discord.Color.from_rgb(61, 125, 105),
        )
        moeda = obter_nome_moeda(ctx.guild.id)
        for tier in TIERS:
            linhas = [
                f"**{npc['nome']}** · Poder {npc['combate']} · Sorte {npc['sorte']} · "
                f"Exploração {npc['exploracao']} · {npc['custo']} {moeda} · "
                f"{npc['status']}"
                for npc in npcs
                if npc["tier"] == tier
            ]
            embed.add_field(
                name=tier,
                value="\n".join(linhas) or "Nenhum NPC cadastrado neste tier.",
                inline=False,
            )
        await ctx.send(embed=embed)

    @listar_npcs.command(name="adicionar", aliases=["cadastrar"])
    async def adicionar_npc(
        self,
        ctx: commands.Context,
        tier: str,
        poder: int,
        sorte: int,
        exploracao: int,
        custo: int,
        *,
        nome: str,
    ) -> None:
        if not await self.exigir_moderacao(ctx):
            return
        resultado_tier = obter_tier(tier)
        if resultado_tier is None:
            await ctx.reply(
                "Tier inválido. Use: Pacífico, Fácil, Médio, Difícil ou Inferno."
            )
            return
        if any(not 1 <= atributo <= 6 for atributo in (poder, sorte, exploracao)):
            await ctx.reply("Poder, sorte e exploração precisam ficar entre 1 e 6.")
            return
        if custo < 0:
            await ctx.reply("O custo do NPC não pode ser negativo.")
            return
        nome = nome.strip().strip('"')
        if not nome or len(nome) > 80:
            await ctx.reply("O nome do NPC precisa ter entre 1 e 80 caracteres.")
            return
        _, nome_tier = resultado_tier
        duplicado = False
        try:
            with conectar_banco() as banco:
                existente = banco.execute(
                    """
                    SELECT id, removido FROM expedicao_mercenarios
                    WHERE servidor_id = ? AND nome = ?
                    """,
                    (ctx.guild.id, nome),
                ).fetchone()
                if existente is None:
                    banco.execute(
                        """
                        INSERT INTO expedicao_mercenarios
                            (servidor_id, nome, combate, sorte, exploracao, custo, tier)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ctx.guild.id,
                            nome,
                            poder,
                            sorte,
                            exploracao,
                            custo,
                            nome_tier,
                        ),
                    )
                    resposta = "cadastrado"
                elif existente[1]:
                    banco.execute(
                        """
                        UPDATE expedicao_mercenarios
                        SET combate = ?, sorte = ?, exploracao = ?, custo = ?,
                            tier = ?, status = 'disponivel', removido = 0
                        WHERE id = ?
                        """,
                        (
                            poder,
                            sorte,
                            exploracao,
                            custo,
                            nome_tier,
                            existente[0],
                        ),
                    )
                    resposta = "reativado"
                else:
                    duplicado = True
        except sqlite3.IntegrityError:
            await ctx.reply("Já existe um NPC com esse nome neste servidor.")
            return
        if duplicado:
            await ctx.reply("Já existe um NPC com esse nome neste servidor.")
            return
        await ctx.reply(
            f"NPC **{nome}** {resposta} no tier **{nome_tier}**, com o custo de "
            f"**{custo} {obter_nome_moeda(ctx.guild.id)}**."
        )

    @listar_npcs.command(name="editar", aliases=["editar_valor", "editar-valor"])
    async def editar_custo_npc(
        self,
        ctx: commands.Context,
        custo: int,
        *,
        nome: str,
    ) -> None:
        if not await self.exigir_moderacao(ctx):
            return
        if custo < 0:
            await ctx.reply("O custo do NPC não pode ser negativo.")
            return
        nome = nome.strip().strip('"')
        with conectar_banco() as banco:
            npc = banco.execute(
                """
                SELECT id, status, removido FROM expedicao_mercenarios
                WHERE servidor_id = ? AND nome = ?
                """,
                (ctx.guild.id, nome),
            ).fetchone()
            if npc is None or npc[2]:
                await ctx.reply("Não encontrei esse NPC neste servidor.")
                return
            if npc[1] == "em_expedicao":
                await ctx.reply(
                    "Esse NPC está em uma expedição e não pode ser editado agora."
                )
                return
            banco.execute(
                "UPDATE expedicao_mercenarios SET custo = ? WHERE id = ?",
                (custo, npc[0]),
            )
        await ctx.reply(
            f"Custo do NPC **{nome}** atualizado para "
            f"**{custo} {obter_nome_moeda(ctx.guild.id)}**."
        )

    @listar_npcs.command(
        name="editar_status",
        aliases=["editar-status", "editar_atributos", "editar-atributos"],
    )
    async def editar_status_npc(
        self,
        ctx: commands.Context,
        tier: str,
        poder: int,
        sorte: int,
        exploracao: int,
        *,
        nome: str,
    ) -> None:
        if not await self.exigir_moderacao(ctx):
            return
        resultado_tier = obter_tier(tier)
        if resultado_tier is None:
            await ctx.reply(
                "Tier inválido. Use: Pacífico, Fácil, Médio, Difícil ou Inferno."
            )
            return
        if any(not 1 <= atributo <= 6 for atributo in (poder, sorte, exploracao)):
            await ctx.reply("Poder, sorte e exploração precisam ficar entre 1 e 6.")
            return
        nome = nome.strip().strip('"')
        if not nome or len(nome) > 80:
            await ctx.reply("O nome do NPC precisa ter entre 1 e 80 caracteres.")
            return

        _, nome_tier = resultado_tier
        with conectar_banco() as banco:
            npc = banco.execute(
                """
                SELECT id, status, removido FROM expedicao_mercenarios
                WHERE servidor_id = ? AND nome = ?
                """,
                (ctx.guild.id, nome),
            ).fetchone()
            if npc is None or npc[2]:
                await ctx.reply("Não encontrei esse NPC neste servidor.")
                return
            if npc[1] == "em_expedicao":
                await ctx.reply(
                    "Esse NPC está em uma expedição e não pode ser editado agora."
                )
                return
            banco.execute(
                """
                UPDATE expedicao_mercenarios
                SET tier = ?, combate = ?, sorte = ?, exploracao = ?
                WHERE id = ?
                """,
                (nome_tier, poder, sorte, exploracao, npc[0]),
            )
        await ctx.reply(
            f"Status do NPC **{nome}** atualizado: tier **{nome_tier}**, "
            f"poder **{poder}**, sorte **{sorte}** e exploração **{exploracao}**."
        )

    @listar_npcs.command(name="remover")
    async def remover_npc(self, ctx: commands.Context, *, nome: str) -> None:
        if not await self.exigir_moderacao(ctx):
            return
        nome = nome.strip().strip('"')
        with conectar_banco() as banco:
            npc = banco.execute(
                """
                SELECT id, status, removido FROM expedicao_mercenarios
                WHERE servidor_id = ? AND nome = ?
                """,
                (ctx.guild.id, nome),
            ).fetchone()
            if npc is None or npc[2]:
                await ctx.reply("Não encontrei esse NPC neste servidor.")
                return
            if npc[1] == "em_expedicao":
                await ctx.reply("Esse NPC está em uma expedição e não pode ser removido agora.")
                return
            banco.execute(
                "UPDATE expedicao_mercenarios SET removido = 1 WHERE id = ?",
                (npc[0],),
            )
        await ctx.reply(f"NPC **{nome}** removido do catálogo deste servidor.")

    @expedicao.command(name="criar")
    async def cadastrar_objetivo(
        self,
        ctx: commands.Context,
        tier: str,
        duracao_minutos: int,
        recompensas: str,
        *,
        nome: str,
    ) -> None:
        if not await self.exigir_moderacao(ctx):
            return
        resultado_tier = obter_tier(tier)
        if resultado_tier is None:
            await ctx.reply(
                "Tier inválido. Use: Pacífico, Fácil, Médio, Difícil ou Inferno."
            )
            return
        tier_indice, nome_tier = resultado_tier
        recompensas_limpas = [item.strip() for item in recompensas.split(",") if item.strip()]
        if not 1 <= duracao_minutos <= MAXIMO_DURACAO_MINUTOS:
            await ctx.reply("A duração precisa ficar entre 1 minuto e 7 dias.")
            return
        if not recompensas_limpas or len(", ".join(recompensas_limpas)) > 500:
            await ctx.reply("Informe pelo menos uma recompensa (lista separada por vírgulas).")
            return
        nome = nome.strip().strip('"')
        if not nome or len(nome) > 80:
            await ctx.reply("O nome do objetivo precisa ter entre 1 e 80 caracteres.")
            return
        try:
            with conectar_banco() as banco:
                banco.execute("BEGIN IMMEDIATE")
                numero = banco.execute(
                    """
                    SELECT COALESCE(MAX(numero), 0) + 1
                    FROM expedicao_objetivos WHERE servidor_id = ?
                    """,
                    (ctx.guild.id,),
                ).fetchone()[0]
                banco.execute(
                    """
                    INSERT INTO expedicao_objetivos
                        (servidor_id, numero, nome, dificuldade, duracao_minutos,
                         tamanho_equipe, custo_base, recompensa)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        ctx.guild.id,
                        numero,
                        nome,
                        tier_indice,
                        duracao_minutos,
                        MAXIMO_NPCS_POR_EQUIPE,
                        ", ".join(recompensas_limpas),
                    ),
                )
        except sqlite3.IntegrityError:
            await ctx.reply("Já existe um objetivo com esse nome neste servidor.")
            return
        await ctx.reply(f"Objetivo **{nome}** cadastrado no tier **{nome_tier}**.")

    @expedicao.command(name="objetivos")
    async def listar_objetivos(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply("Consulte os objetivos dentro de um servidor.")
            return
        with conectar_banco() as banco:
            objetivos = banco.execute(
                """
                SELECT numero, nome, dificuldade, duracao_minutos, recompensa
                FROM expedicao_objetivos WHERE servidor_id = ? ORDER BY numero
                """,
                (ctx.guild.id,),
            ).fetchall()
        if not objetivos:
            await ctx.reply("Ainda não há objetivos cadastrados pela moderação.")
            return
        linhas = [
            f"**#{objetivo[0]} · {objetivo[1]}** ({TIERS[objetivo[2] - 1]}, "
            f"{objetivo[3]} min)\nLoot: {objetivo[4]}"
            for objetivo in objetivos
        ]
        await ctx.send(embed=discord.Embed(
            title="Objetivos de expedição",
            description="\n\n".join(linhas)[:4000],
            color=discord.Color.from_rgb(61, 125, 105),
        ))

    @expedicao.command(name="remover")
    async def remover_objetivo(self, ctx: commands.Context, numero: int) -> None:
        if not await self.exigir_moderacao(ctx):
            return
        with conectar_banco() as banco:
            cursor = banco.execute(
                "DELETE FROM expedicao_objetivos WHERE numero = ? AND servidor_id = ?",
                (numero, ctx.guild.id),
            )
        if cursor.rowcount == 0:
            await ctx.reply("Não encontrei esse objetivo neste servidor.")
            return
        await ctx.reply(f"Objetivo `#{numero}` removido com sucesso.")

    @expedicao.command(name="contratar")
    async def contratar(self, ctx: commands.Context, numero: int) -> None:
        if ctx.guild is None:
            await ctx.reply("As expedições só podem ser contratadas dentro de um servidor.")
            return
        garantir_catalogo(ctx.guild.id)
        with conectar_banco() as banco:
            banco.row_factory = sqlite3.Row
            objetivo = banco.execute(
                """
                SELECT * FROM expedicao_objetivos
                WHERE numero = ? AND servidor_id = ?
                """,
                (numero, ctx.guild.id),
            ).fetchone()
            ativa = banco.execute(
                """
                SELECT 1 FROM expedicao_viagens
                WHERE servidor_id = ? AND contratante_id = ? AND status = 'ativa'
                """,
                (ctx.guild.id, ctx.author.id),
            ).fetchone()
            npcs = banco.execute(
                """
                SELECT id, nome, tier, combate, sorte, exploracao, custo
                FROM expedicao_mercenarios
                WHERE servidor_id = ? AND removido = 0 AND status = 'disponivel'
                ORDER BY CASE tier
                    WHEN 'Pacífico' THEN 1 WHEN 'Fácil' THEN 2 WHEN 'Médio' THEN 3
                    WHEN 'Difícil' THEN 4 ELSE 5 END, nome COLLATE NOCASE
                """,
                (ctx.guild.id,),
            ).fetchall()
        if ativa:
            await ctx.reply("Você já tem uma expedição em andamento.")
            return
        if objetivo is None:
            await ctx.reply("Não encontrei esse objetivo. Use `!expedicao objetivos`.")
            return
        tier_objetivo = int(objetivo["dificuldade"])
        npcs_elegiveis = [
            npc for npc in npcs if obter_tier(npc["tier"])[0] <= tier_objetivo
        ]
        if not npcs_elegiveis:
            await ctx.reply("Não há exploradores disponíveis para esse tier.")
            return
        view = ContratarExpedicaoView(
            self,
            ctx.author.id,
            ctx.guild.id,
            ctx.channel.id,
            objetivo,
            npcs_elegiveis,
        )
        view.mensagem = await ctx.send(embed=view.criar_embed(), view=view)

    @expedicao.command(name="ranking")
    async def ranking(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply("Consulte o ranking dentro de um servidor.")
            return
        garantir_catalogo(ctx.guild.id)
        with conectar_banco() as banco:
            ranking = banco.execute(
                """
                SELECT n.nome, n.tier,
                       CASE WHEN n.removido = 1 THEN 'removido' ELSE n.status END,
                       r.contratacoes, r.missoes_concluidas, r.sucessos,
                       r.baixas, r.itens_looteados, r.pontos
                FROM expedicao_mercenario_ranking AS r
                JOIN expedicao_mercenarios AS n ON n.id = r.mercenario_id
                WHERE r.servidor_id = ?
                ORDER BY r.pontos DESC, r.contratacoes DESC,
                         r.sucessos DESC, n.nome COLLATE NOCASE
                """,
                (ctx.guild.id,),
            ).fetchall()
        linhas = [
            f"**#{posicao} {nome}** ({tier}, {status}) · **{pontos} pts** · "
            f"{contratacoes} contratações · {sucessos}/{missoes} sucessos · "
            f"{itens} itens · {baixas} baixas"
            for posicao, (
                nome,
                tier,
                status,
                contratacoes,
                missoes,
                sucessos,
                baixas,
                itens,
                pontos,
            ) in enumerate(ranking, start=1)
        ]
        embed = discord.Embed(
            title=f"Ranking de exploradores · {ctx.guild.name}",
            description=(
                "\n".join(linhas)[:4000]
                if linhas
                else "Ainda não há exploradores cadastrados."
            ),
            color=discord.Color.from_rgb(61, 125, 105),
        )
        embed.set_footer(
            text="Popularidade = contratações. Pontos = quantidade × raridade do loot; cada membro recebe crédito pelo saque da equipe."
        )
        await ctx.send(embed=embed)

    @expedicao.command(name="pendentes")
    async def listar_cobrancas_pendentes(self, ctx: commands.Context) -> None:
        if not await self.exigir_moderacao(ctx):
            return
        with conectar_banco() as banco:
            pendentes = banco.execute(
                """
                SELECT numero, contratante_id, objetivo_nome, custo
                FROM expedicao_viagens
                WHERE servidor_id = ? AND status = 'ativa' AND manual_pendente = 1
                ORDER BY id
                """,
                (ctx.guild.id,),
            ).fetchall()
        if not pendentes:
            await ctx.reply("Não há cobranças manuais aguardando aprovação.")
            return
        moeda = obter_nome_moeda(ctx.guild.id)
        linhas = [
            f"**#{viagem_id} · {objetivo}** — <@{contratante_id}> · "
            f"cobrar **{custo} {moeda}** e então usar "
            f"`!expedicao aprovar {viagem_id}`."
            for viagem_id, contratante_id, objetivo, custo in pendentes
        ]
        await ctx.send(
            embed=discord.Embed(
                title="Cobranças manuais pendentes",
                description="\n".join(linhas)[:4000],
                color=discord.Color.orange(),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @expedicao.command(name="aprovar")
    async def aprovar_cobranca_manual(
        self,
        ctx: commands.Context,
        viagem_id: int,
    ) -> None:
        if not await self.exigir_moderacao(ctx):
            return
        with conectar_banco() as banco:
            banco.execute("BEGIN IMMEDIATE")
            viagem = banco.execute(
                """
                SELECT duracao_segundos FROM expedicao_viagens
                WHERE numero = ? AND servidor_id = ? AND status = 'ativa'
                    AND manual_pendente = 1
                """,
                (viagem_id, ctx.guild.id),
            ).fetchone()
            if viagem is None:
                banco.rollback()
                await ctx.reply(
                    "Não encontrei uma expedição manual pendente com esse ID."
                )
                return
            termina_em = (
                datetime.now(timezone.utc).timestamp() + int(viagem[0])
            )
            cursor = banco.execute(
                """
                UPDATE expedicao_viagens
                SET manual_pendente = 0, termina_em = ?
                WHERE numero = ? AND servidor_id = ? AND status = 'ativa'
                    AND manual_pendente = 1
                """,
                (termina_em, viagem_id, ctx.guild.id),
            )
            if cursor.rowcount != 1:
                banco.rollback()
                await ctx.reply(
                    "A expedição deixou de estar pendente; confira a lista novamente."
                )
                return
        logger.info(
            "Expedição %s aprovada manualmente pelo moderador %s no servidor %s.",
            viagem_id,
            ctx.author.id,
            ctx.guild.id,
        )
        await ctx.reply(
            f"Expedição **#{viagem_id}** aprovada. A duração começou agora; "
            "confirme que a cobrança manual foi realizada."
        )

    @expedicao.command(name="rejeitar")
    async def rejeitar_cobranca_manual(
        self,
        ctx: commands.Context,
        viagem_id: int,
    ) -> None:
        if not await self.exigir_moderacao(ctx):
            return
        with conectar_banco() as banco:
            pendente = banco.execute(
                """
                SELECT id FROM expedicao_viagens
                WHERE numero = ? AND servidor_id = ? AND status = 'ativa'
                    AND manual_pendente = 1
                """,
                (viagem_id, ctx.guild.id),
            ).fetchone()
        if pendente is None:
            await ctx.reply(
                "Não encontrei uma expedição manual pendente com esse ID."
            )
            return
        self.cancelar_viagem_sem_cobranca(pendente[0])
        logger.info(
            "Expedição %s rejeitada pelo moderador %s no servidor %s.",
            viagem_id,
            ctx.author.id,
            ctx.guild.id,
        )
        await ctx.reply(
            f"Expedição **#{viagem_id}** rejeitada e exploradores liberados. "
            "Se a cobrança já foi feita, devolva o valor manualmente."
        )

    @expedicao.command(name="status")
    async def status(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply("Consulte o status dentro de um servidor.")
            return
        with conectar_banco() as banco:
            viagem = banco.execute(
                """
                SELECT numero, objetivo_nome, termina_em, custo, chance_sucesso,
                    manual_pendente
                FROM expedicao_viagens
                WHERE servidor_id = ? AND contratante_id = ? AND status = 'ativa'
                """,
                (ctx.guild.id, ctx.author.id),
            ).fetchone()
        if viagem is None:
            await ctx.reply("Você não tem expedições em andamento.")
            return
        if viagem[5]:
            await ctx.reply(
                f"Expedição **#{viagem[0]} · {viagem[1]}** aguardando a moderação. "
                f"Custo: {viagem[3]} {obter_nome_moeda(ctx.guild.id)}. "
                "A duração começa após a cobrança manual e aprovação."
            )
            return
        await ctx.reply(
            f"Expedição **#{viagem[0]} · {viagem[1]}**: retorno <t:{int(viagem[2])}:R>. "
            f"Custo estimado: {viagem[3]} {obter_nome_moeda(ctx.guild.id)}; "
            f"chance de sucesso: {viagem[4]}%."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ExpedicoesCog(bot))
