import asyncio
import logging
import math
import random
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import discord
from discord.ext import commands

from cogs.expedicoes import (
    SaldoInsuficienteError,
    UnbelievaBoatAPIError,
    atualizar_saldo_unbelievaboat,
    eh_moderador,
    obter_nome_moeda,
    obter_saldo_unbelievaboat,
)

CAMINHO_BANCO = Path(__file__).resolve().parents[1] / "config" / "raspadinhas.sqlite3"
LADO_MAXIMO = 10
CASAS_MAXIMAS = LADO_MAXIMO * LADO_MAXIMO
CASAS_POR_PAGINA = 25
TIPOS_PREMIO = {"normal", "médio", "grande", "épico", "vazio"}
logger = logging.getLogger(__name__)


def coordenada_casa(casa: int, casas_por_lado: int) -> str:
    if casas_por_lado < 1 or casas_por_lado > LADO_MAXIMO:
        raise ValueError("O lado da raspadinha deve estar entre 1 e 10.")
    if casa < 1 or casa > casas_por_lado * casas_por_lado:
        raise ValueError("A casa está fora do tamanho desta raspadinha.")
    linha = chr(ord("A") + (casa - 1) // casas_por_lado)
    coluna = (casa - 1) % casas_por_lado + 1
    return f"{linha}{coluna}"


@contextmanager
def conectar_banco() -> Generator[sqlite3.Connection, None, None]:
    CAMINHO_BANCO.parent.mkdir(parents=True, exist_ok=True)
    banco = sqlite3.connect(CAMINHO_BANCO, timeout=10)
    banco.execute("PRAGMA foreign_keys = ON")
    banco.row_factory = sqlite3.Row
    try:
        yield banco
        banco.commit()
    except Exception:
        banco.rollback()
        raise
    finally:
        banco.close()


def inicializar_banco() -> None:
    with conectar_banco() as banco:
        banco.executescript(
            """
            CREATE TABLE IF NOT EXISTS raspadinhas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                servidor_id INTEGER NOT NULL,
                criador_id INTEGER NOT NULL,
                canal_id INTEGER,
                nome TEXT NOT NULL,
                preco INTEGER NOT NULL CHECK (preco > 0),
                casas_total INTEGER NOT NULL DEFAULT 100
                    CHECK (casas_total BETWEEN 1 AND 100),
                status TEXT NOT NULL DEFAULT 'ativa'
                    CHECK (status IN ('ativa', 'pausada', 'arquivada')),
                versao_preco INTEGER NOT NULL DEFAULT 1,
                atualizada_por INTEGER,
                UNIQUE (servidor_id, nome)
            );
            CREATE TABLE IF NOT EXISTS raspadinha_premios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raspadinha_id INTEGER NOT NULL,
                nome TEXT NOT NULL,
                valor INTEGER NOT NULL CHECK (valor >= 0),
                quantidade INTEGER NOT NULL CHECK (quantidade >= 0),
                tipo TEXT NOT NULL CHECK (tipo IN ('normal', 'grande', 'vazio')),
                classificacao TEXT NOT NULL
                    CHECK (classificacao IN ('normal', 'médio', 'grande', 'épico', 'vazio')),
                FOREIGN KEY (raspadinha_id) REFERENCES raspadinhas(id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS raspadinha_aberturas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                servidor_id INTEGER NOT NULL,
                raspadinha_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                casa INTEGER NOT NULL,
                casa_indice INTEGER,
                premio_id INTEGER NOT NULL,
                preco_cobrado INTEGER NOT NULL,
                valor_pago INTEGER NOT NULL,
                interaction_id INTEGER NOT NULL UNIQUE,
                criada_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (raspadinha_id) REFERENCES raspadinhas(id),
                FOREIGN KEY (premio_id) REFERENCES raspadinha_premios(id)
            );
            """
        )
        colunas_cartela = {
            str(coluna["name"])
            for coluna in banco.execute("PRAGMA table_info(raspadinhas)").fetchall()
        }
        if "canal_id" not in colunas_cartela:
            banco.execute("ALTER TABLE raspadinhas ADD COLUMN canal_id INTEGER")
        if "casas_total" not in colunas_cartela:
            banco.execute(
                "ALTER TABLE raspadinhas ADD COLUMN casas_total INTEGER NOT NULL "
                "DEFAULT 100 CHECK (casas_total BETWEEN 1 AND 100)"
            )
        colunas_premio = {
            str(coluna["name"])
            for coluna in banco.execute(
                "PRAGMA table_info(raspadinha_premios)"
            ).fetchall()
        }
        if "quantidade" not in colunas_premio:
            banco.execute(
                "ALTER TABLE raspadinha_premios ADD COLUMN quantidade INTEGER "
                "NOT NULL DEFAULT 0 CHECK (quantidade >= 0)"
            )
            if "peso" in colunas_premio:
                banco.execute(
                    "UPDATE raspadinha_premios SET quantidade = peso"
                )
        if "classificacao" not in colunas_premio:
            banco.execute(
                "ALTER TABLE raspadinha_premios ADD COLUMN classificacao TEXT "
                "NOT NULL DEFAULT 'normal'"
            )
            banco.execute(
                "UPDATE raspadinha_premios SET classificacao = tipo"
            )
        colunas = {
            str(coluna["name"])
            for coluna in banco.execute(
                "PRAGMA table_info(raspadinha_aberturas)"
            ).fetchall()
        }
        if "casa_indice" not in colunas:
            banco.execute(
                "ALTER TABLE raspadinha_aberturas ADD COLUMN casa_indice INTEGER"
            )
        banco.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS raspadinha_casa_unica
            ON raspadinha_aberturas (raspadinha_id, casa_indice)
            WHERE casa_indice IS NOT NULL
            """
        )


class CasasSelect(discord.ui.Select["RaspadinhaView"]):
    def __init__(self, view: "RaspadinhaView") -> None:
        opcoes = view.opcoes_casas()
        super().__init__(
            placeholder="Escolha uma casa ainda não aberta",
            min_values=1,
            max_values=1,
            custom_id=f"raspadinha:casas:{view.raspadinha_id}",
            options=opcoes
            or [
                discord.SelectOption(
                    label="Todas as casas desta página já foram abertas",
                    value="nenhuma",
                )
            ],
            disabled=not view.ativa or not opcoes,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is not None:
            if self.values[0] == "nenhuma":
                await interaction.response.send_message(
                    "Todas as casas desta página já foram abertas.",
                    ephemeral=True,
                )
                return
            await self.view.abrir_casa(interaction, int(self.values[0]))


class PaginaSelect(discord.ui.Select["RaspadinhaView"]):
    def __init__(self, view: "RaspadinhaView") -> None:
        super().__init__(
            placeholder="Escolha um intervalo de casas",
            min_values=1,
            max_values=1,
            custom_id=f"raspadinha:pagina:{view.raspadinha_id}",
            options=view.opcoes_paginas(),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is not None:
            self.view.pagina = int(self.values[0])
            self.view.atualizar_componentes()
            await interaction.response.edit_message(
                embed=self.view.criar_embed(
                    self.view.nome,
                    self.view.preco,
                    obter_nome_moeda(self.view.servidor_id),
                ),
                view=self.view,
            )


class RaspadinhaView(discord.ui.View):
    def __init__(
        self,
        cog: "RaspadinhasCog",
        servidor_id: int,
        raspadinha_id: int,
        versao_preco: int,
        nome: str,
        preco: int,
        casas_total: int,
        casas_abertas: dict[int, dict[str, object]],
    ) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.servidor_id = servidor_id
        self.raspadinha_id = raspadinha_id
        self.versao_preco = versao_preco
        self.nome = nome
        self.preco = preco
        self.casas_total = casas_total
        self.casas_por_lado = math.isqrt(casas_total)
        self.casas_abertas = casas_abertas
        self.pagina = 0
        self.ativa = True
        self.mensagem: discord.Message | None = None
        self.pagina_select = PaginaSelect(self)
        self.casas_select = CasasSelect(self)
        self.add_item(self.pagina_select)
        self.add_item(self.casas_select)

    def opcoes_casas(self) -> list[discord.SelectOption]:
        inicio = self.pagina * CASAS_POR_PAGINA + 1
        fim = min(inicio + CASAS_POR_PAGINA, self.casas_total + 1)
        return [
            discord.SelectOption(
                label=coordenada_casa(casa, self.casas_por_lado),
                value=str(casa),
            )
            for casa in range(inicio, fim)
            if casa not in self.casas_abertas
        ]

    def opcoes_paginas(self) -> list[discord.SelectOption]:
        return [
            discord.SelectOption(
                label=(
                    f"Casas {coordenada_casa(inicio, self.casas_por_lado)}–"
                    f"{coordenada_casa(fim, self.casas_por_lado)}"
                ),
                value=str(pagina),
                default=pagina == self.pagina,
            )
            for pagina, inicio in enumerate(
                range(1, self.casas_total + 1, CASAS_POR_PAGINA)
            )
            for fim in [min(inicio + CASAS_POR_PAGINA - 1, self.casas_total)]
        ]

    def atualizar_componentes(self) -> None:
        self.pagina_select.options = self.opcoes_paginas()
        opcoes = self.opcoes_casas()
        self.casas_select.options = opcoes or [
            discord.SelectOption(
                label="Todas as casas desta página já foram abertas",
                value="nenhuma",
            )
        ]
        self.casas_select.disabled = not self.ativa or not opcoes

    def criar_embed(self, nome: str, preco: int, moeda: str) -> discord.Embed:
        embed = discord.Embed(
            title=f"Raspadinha: {nome}",
            description=(
                f"Tabuleiro **{self.casas_por_lado}×{self.casas_por_lado} "
                f"({coordenada_casa(1, self.casas_por_lado)}–"
                f"{coordenada_casa(self.casas_total, self.casas_por_lado)})** · "
                f"cada casa custa **{preco} {moeda}**.\n"
                "▫ não aberta · ✓ prêmio · ★ prêmio grande · — sem prêmio. "
                "◆ prêmio médio · ✦ prêmio épico. "
                "Escolha um intervalo e depois uma casa no menu."
            ),
            color=discord.Color.gold(),
        )
        cabecalho = "   " + "".join(
            f"{coluna:>4}" for coluna in range(1, self.casas_por_lado + 1)
        )
        linhas = [cabecalho]
        for linha in range(self.casas_por_lado):
            celulas = []
            for coluna in range(self.casas_por_lado):
                casa = linha * self.casas_por_lado + coluna + 1
                resultado = self.casas_abertas.get(casa)
                if resultado is None:
                    celulas.append(f"{'▫':>4}")
                elif resultado["tipo"] == "grande":
                    celulas.append(f"{'★':>4}")
                elif resultado["tipo"] == "médio":
                    celulas.append(f"{'◆':>4}")
                elif resultado["tipo"] == "épico":
                    celulas.append(f"{'✦':>4}")
                elif resultado["tipo"] == "vazio":
                    celulas.append(f"{'—':>4}")
                else:
                    celulas.append(f"{'✓':>4}")
            letra_linha = chr(ord("A") + linha)
            linhas.append(letra_linha + "  " + "".join(celulas))
        embed.add_field(
            name=f"Casas abertas: {len(self.casas_abertas)}/{self.casas_total}",
            value="```text\n" + "\n".join(linhas) + "\n```",
            inline=False,
        )
        recentes = list(self.casas_abertas.items())[-10:]
        if recentes:
            embed.add_field(
                name="Últimas casas reveladas",
                value="\n".join(
                    f"Casa {coordenada_casa(casa, self.casas_por_lado)}: "
                    f"**{resultado['nome']}**"
                    + (
                        f" — {resultado['valor']} {moeda}"
                        if int(resultado["valor"]) > 0
                        else ""
                    )
                    for casa, resultado in recentes
                ),
                inline=False,
            )
        embed.set_footer(
            text="As casas abertas permanecem salvas entre aberturas do painel."
        )
        return embed

    async def abrir_casa(
        self,
        interaction: discord.Interaction,
        casa: int,
    ) -> None:
        if interaction.guild_id != self.servidor_id:
            await interaction.response.send_message(
                "Esta raspadinha só pode ser usada no servidor em que foi criada.",
                ephemeral=True,
            )
            return
        if casa in self.casas_abertas:
            await interaction.response.send_message(
                "Esta casa já foi aberta.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        resultado = await self.cog.abrir_casa(
            servidor_id=self.servidor_id,
            raspadinha_id=self.raspadinha_id,
            versao_preco=self.versao_preco,
            casa=casa,
            usuario_id=interaction.user.id,
            interaction_id=interaction.id,
            canal=interaction.channel,
        )
        erro = resultado.get("erro")
        if erro:
            await interaction.followup.send(str(erro), ephemeral=True)
            if resultado.get("pausada", False):
                self.ativa = False
                self.atualizar_componentes()
                await interaction.edit_original_response(view=self)
            return

        self.casas_abertas[casa] = {
            "nome": resultado["rotulo"],
            "tipo": resultado["tipo"],
            "valor": resultado["valor"],
        }
        if resultado["pausada"]:
            self.ativa = False
        self.atualizar_componentes()

        await interaction.edit_original_response(
            embed=self.criar_embed(
                str(resultado["nome"]),
                int(resultado["preco"]),
                obter_nome_moeda(self.servidor_id),
            ),
            view=self,
        )
        if resultado["aviso"]:
            await interaction.followup.send(str(resultado["aviso"]), ephemeral=True)

    async def on_timeout(self) -> None:
        self.ativa = False
        self.atualizar_componentes()
        if self.mensagem is not None:
            try:
                await self.mensagem.edit(view=self)
            except discord.HTTPException:
                logger.warning(
                    "Não foi possível desativar o painel expirado da raspadinha %s.",
                    self.raspadinha_id,
                    exc_info=True,
                )


class RaspadinhasCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.travas: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        inicializar_banco()

    def obter_trava(self, raspadinha_id: int) -> asyncio.Lock:
        if raspadinha_id not in self.travas:
            self.travas[raspadinha_id] = asyncio.Lock()
        return self.travas[raspadinha_id]

    @staticmethod
    def pode_gerenciar(ctx: commands.Context, criador_id: int) -> bool:
        return ctx.author.id == criador_id or (
            isinstance(ctx.author, discord.Member) and eh_moderador(ctx.author)
        )

    async def cobrar(
        self,
        servidor_id: int,
        usuario_id: int,
        valor: int,
        raspadinha_id: int,
    ) -> None:
        cash, bank = await obter_saldo_unbelievaboat(servidor_id, usuario_id)
        disponivel = cash + bank
        if disponivel < valor:
            raise SaldoInsuficienteError(disponivel, valor)
        debito_cash = min(cash, valor)
        await atualizar_saldo_unbelievaboat(
            servidor_id=servidor_id,
            usuario_id=usuario_id,
            cash=-debito_cash,
            bank=-(valor - debito_cash),
            motivo=f"Abertura de casa da raspadinha #{raspadinha_id}",
        )

    async def pagar_premio(
        self,
        servidor_id: int,
        usuario_id: int,
        valor: int,
        raspadinha_id: int,
    ) -> None:
        if valor:
            await atualizar_saldo_unbelievaboat(
                servidor_id=servidor_id,
                usuario_id=usuario_id,
                cash=valor,
                bank=0,
                motivo=f"Prêmio da raspadinha #{raspadinha_id}",
            )

    async def notificar_dono(
        self,
        canal_id: int | None,
        criador_id: int,
        raspadinha_id: int,
        nome: str,
        tipo_premio: str,
        canal_fallback: discord.abc.Messageable | None,
    ) -> bool:
        canal = self.bot.get_channel(canal_id) if canal_id is not None else None
        try:
            if canal is None and canal_id is not None:
                canal = await self.bot.fetch_channel(canal_id)
        except discord.HTTPException:
            logger.exception(
                "Não foi possível localizar o canal de criação da raspadinha %s.",
                raspadinha_id,
            )
            canal = None

        if canal is None:
            canal = canal_fallback
        if canal is None:
            logger.error(
                "Não há canal disponível para notificar o responsável da raspadinha %s.",
                raspadinha_id,
            )
            return False
        try:
            await canal.send(
                f"<@{criador_id}> o prêmio {tipo_premio} saiu na raspadinha **{nome}**; ela foi pausada. Atualize o preço com "
                f"`!raspadinha atualizar {raspadinha_id} <novo_preço>` para reativá-la.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            return True
        except discord.HTTPException:
            logger.exception(
                "Não foi possível notificar no canal de criação da raspadinha %s.",
                raspadinha_id,
            )
            return False

    async def abrir_casa(
        self,
        servidor_id: int,
        raspadinha_id: int,
        versao_preco: int,
        casa: int,
        usuario_id: int,
        interaction_id: int,
        canal: discord.abc.Messageable | None,
    ) -> dict[str, object]:
        async with self.obter_trava(raspadinha_id):
            with conectar_banco() as banco:
                cartela = banco.execute(
                    """
                    SELECT nome, criador_id, canal_id, preco, casas_total, status,
                        versao_preco
                    FROM raspadinhas WHERE id = ? AND servidor_id = ?
                    """,
                    (raspadinha_id, servidor_id),
                ).fetchone()
                if cartela is None:
                    return {"erro": "Essa raspadinha não existe neste servidor."}
                casas_total = int(cartela["casas_total"])
                if casa < 1 or casa > casas_total:
                    return {
                        "erro": f"A casa escolhida está fora do tabuleiro "
                        f"de {casas_total} casas."
                    }
                if cartela["status"] != "ativa":
                    return {
                        "erro": "Essa raspadinha está pausada ou arquivada.",
                        "pausada": True,
                    }
                if cartela["versao_preco"] != versao_preco:
                    return {
                        "erro": "O preço foi atualizado. Abra um painel novo usando "
                        f"`!raspadinha jogar {raspadinha_id}`."
                    }
                casa_existente = banco.execute(
                    """
                    SELECT 1 FROM raspadinha_aberturas
                    WHERE raspadinha_id = ? AND casa_indice = ?
                    """,
                    (raspadinha_id, casa),
                ).fetchone()
                if casa_existente is not None:
                    return {
                        "erro": f"A casa {coordenada_casa(casa, math.isqrt(casas_total))} "
                        "já foi aberta nesta raspadinha. Abra o painel novamente "
                        "para ver o estado atualizado."
                    }
                total_abertas = banco.execute(
                    """
                    SELECT COUNT(*) FROM raspadinha_aberturas
                    WHERE raspadinha_id = ? AND casa_indice IS NOT NULL
                    """,
                    (raspadinha_id,),
                ).fetchone()[0]
                if total_abertas >= casas_total:
                    return {
                        "erro": f"Todas as {casas_total} casas desta raspadinha já foram abertas."
                    }
                premios = banco.execute(
                    """
                    SELECT p.id, p.nome, p.valor, p.quantidade,
                        p.classificacao AS tipo,
                        p.quantidade - COUNT(a.id) AS restantes
                    FROM raspadinha_premios p
                    LEFT JOIN raspadinha_aberturas a
                        ON a.premio_id = p.id
                    WHERE p.raspadinha_id = ?
                    GROUP BY p.id
                    HAVING restantes > 0
                    """,
                    (raspadinha_id,),
                ).fetchall()
                quantidade_configurada = banco.execute(
                    """
                    SELECT COALESCE(SUM(quantidade), 0)
                    FROM raspadinha_premios WHERE raspadinha_id = ?
                    """,
                    (raspadinha_id,),
                ).fetchone()[0]
                if quantidade_configurada != casas_total:
                    return {
                        "erro": f"As quantidades dos prêmios somam "
                        f"{quantidade_configurada}, mas esta cartela tem "
                        f"{casas_total} casas. Ajuste com "
                        f"`!raspadinha quantidade {raspadinha_id} <id_prêmio> "
                        "<quantidade>`."
                    }
                if not premios:
                    return {
                        "erro": "Não há prêmios restantes configurados para as casas "
                        "desta raspadinha."
                    }

            try:
                await self.cobrar(
                    servidor_id,
                    usuario_id,
                    int(cartela["preco"]),
                    raspadinha_id,
                )
            except SaldoInsuficienteError as erro:
                return {
                    "erro": f"Saldo insuficiente: a casa custa {erro.custo} "
                    f"{obter_nome_moeda(servidor_id)} e você tem "
                    f"{erro.disponivel} (cash + bank)."
                }
            except UnbelievaBoatAPIError as erro:
                logger.error(
                    "Falha ao cobrar a casa da raspadinha %s do usuário %s: %s",
                    raspadinha_id,
                    usuario_id,
                    erro,
                )
                mensagem = (
                    "A cobrança pode ter sido processada. Confira seu saldo antes de "
                    "tentar novamente e avise a moderação se houver débito."
                    if erro.cobranca_incerta
                    else f"Não foi possível cobrar pelo UnbelievaBoat: {erro}"
                )
                return {"erro": mensagem}

            premio = random.choices(
                premios,
                weights=[int(item["restantes"]) for item in premios],
                k=1,
            )[0]
            pausada = premio["tipo"] in {"grande", "épico"}
            try:
                with conectar_banco() as banco:
                    banco.execute("BEGIN IMMEDIATE")
                    status_atual = banco.execute(
                        """
                        SELECT status, versao_preco FROM raspadinhas
                        WHERE id = ? AND servidor_id = ?
                        """,
                        (raspadinha_id, servidor_id),
                    ).fetchone()
                    if (
                        status_atual is None
                        or status_atual["status"] != "ativa"
                        or status_atual["versao_preco"] != versao_preco
                    ):
                        banco.rollback()
                        return {
                            "erro": "A raspadinha mudou enquanto a cobrança era "
                            "processada. Avise a moderação antes de tentar novamente."
                        }
                    banco.execute(
                        """
                        INSERT INTO raspadinha_aberturas (
                            servidor_id, raspadinha_id, usuario_id, casa, casa_indice,
                            premio_id, preco_cobrado, valor_pago, interaction_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            servidor_id,
                            raspadinha_id,
                            usuario_id,
                            casa,
                            casa,
                            premio["id"],
                            cartela["preco"],
                            premio["valor"],
                            interaction_id,
                        ),
                    )
                    if pausada:
                        banco.execute(
                            """
                            UPDATE raspadinhas SET status = 'pausada'
                            WHERE id = ? AND servidor_id = ? AND status = 'ativa'
                            """,
                            (raspadinha_id, servidor_id),
                        )
            except sqlite3.Error:
                logger.exception(
                    "Cobrança efetuada, mas não foi possível registrar a abertura "
                    "da raspadinha %s.",
                    raspadinha_id,
                )
                return {
                    "erro": "A cobrança foi confirmada, mas houve um erro ao registrar "
                    "a abertura. Não tente novamente; avise a moderação."
                }

            aviso_pagamento = ""
            try:
                await self.pagar_premio(
                    servidor_id,
                    usuario_id,
                    int(premio["valor"]),
                    raspadinha_id,
                )
            except UnbelievaBoatAPIError as erro:
                logger.error(
                    "Falha ao pagar prêmio da raspadinha %s ao usuário %s: %s",
                    raspadinha_id,
                    usuario_id,
                    erro,
                )
                aviso_pagamento = (
                    " O prêmio foi registrado, mas o pagamento não foi confirmado; "
                    "avise a moderação antes de jogar novamente."
                )

            aviso_dono = ""
            if pausada:
                notificado = await self.notificar_dono(
                    int(cartela["canal_id"]) if cartela["canal_id"] is not None else None,
                    int(cartela["criador_id"]),
                    raspadinha_id,
                    str(cartela["nome"]),
                    str(premio["tipo"]),
                    canal,
                )
                if not notificado:
                    aviso_dono = (
                        " Não consegui entregar a notificação ao responsável; "
                        "a moderação deve avisá-lo."
                    )
            return {
                "nome": cartela["nome"],
                "preco": cartela["preco"],
                "rotulo": premio["nome"],
                "tipo": premio["tipo"],
                "valor": premio["valor"],
                "pausada": pausada,
                "aviso": aviso_pagamento + aviso_dono,
            }

    @commands.group(name="raspadinha", invoke_without_command=True)
    async def raspadinha(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply("Consulte as raspadinhas dentro de um servidor.")
            return
        with conectar_banco() as banco:
            cartelas = banco.execute(
                """
                SELECT id, nome, preco, casas_total, status, criador_id
                FROM raspadinhas WHERE servidor_id = ? AND status != 'arquivada'
                ORDER BY id
                """,
                (ctx.guild.id,),
            ).fetchall()
        if not cartelas:
            await ctx.reply("Ainda não há raspadinhas cadastradas neste servidor.")
            return
        moeda = obter_nome_moeda(ctx.guild.id)
        linhas = [
            f"**#{item['id']} · {item['nome']}** — "
            f"{math.isqrt(int(item['casas_total']))}×"
            f"{math.isqrt(int(item['casas_total']))} · {item['preco']} {moeda} por casa "
            f"· {'disponível' if item['status'] == 'ativa' else 'aguardando atualização'}"
            for item in cartelas
        ]
        await ctx.reply(
            "\n".join(linhas)
            + "\nUse `!raspadinha jogar <id>` para abrir o tabuleiro."
        )

    @raspadinha.command(name="criar")
    async def criar(
        self,
        ctx: commands.Context,
        preco: int,
        casas_total: int,
        *,
        nome: str,
    ) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.reply("Crie raspadinhas dentro de um servidor.")
            return
        if not eh_moderador(ctx.author):
            await ctx.reply("Somente mods e admins podem criar raspadinhas.")
            return
        lado = math.isqrt(casas_total) if casas_total >= 0 else 0
        if (
            preco <= 0
            or casas_total < 1
            or casas_total > CASAS_MAXIMAS
            or lado * lado != casas_total
            or not nome.strip()
            or len(nome) > 80
        ):
            await ctx.reply(
                "Informe um preço positivo, um nome de até 80 caracteres e uma "
                "quantidade de casas quadrada entre 1 e 100 "
                "(ex.: 25, 36 ou 100)."
            )
            return
        try:
            with conectar_banco() as banco:
                cursor = banco.execute(
                    """
                    INSERT INTO raspadinhas (
                        servidor_id, criador_id, canal_id, nome, preco, casas_total
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ctx.guild.id,
                        ctx.author.id,
                        ctx.channel.id,
                        nome.strip(),
                        preco,
                        casas_total,
                    ),
                )
        except sqlite3.IntegrityError:
            await ctx.reply("Já existe uma raspadinha com esse nome neste servidor.")
            return
        await ctx.reply(
            f"Raspadinha **#{cursor.lastrowid} · {nome.strip()}** "
            f"({lado}×{lado}) criada por "
            f"{preco} {obter_nome_moeda(ctx.guild.id)} por casa. "
            f"Adicione prêmios com `!raspadinha premio {cursor.lastrowid} "
            "<valor> <quantidade> <normal|médio|grande|épico|vazio> <nome>`. "
            "As quantidades dos prêmios, incluindo os vazios, devem somar "
            f"{casas_total}."
        )

    @raspadinha.command(name="premio")
    async def adicionar_premio(
        self,
        ctx: commands.Context,
        raspadinha_id: int,
        valor: int,
        quantidade: int,
        tipo: str,
        *,
        nome: str,
    ) -> None:
        if ctx.guild is None:
            await ctx.reply("Cadastre prêmios dentro de um servidor.")
            return
        tipo = tipo.casefold()
        if valor < 0 or quantidade < 1 or quantidade > CASAS_MAXIMAS:
            await ctx.reply(
                "O valor não pode ser negativo e a quantidade deve ser de 1 a 100."
            )
            return
        if tipo not in TIPOS_PREMIO:
            await ctx.reply(
                "O tipo deve ser `normal`, `médio`, `grande`, `épico` ou `vazio`."
            )
            return
        if (tipo == "vazio" and valor != 0) or (tipo != "vazio" and valor <= 0):
            await ctx.reply(
                "Prêmio vazio deve valer 0; prêmio normal, médio, grande ou épico "
                "deve valer mais que 0."
            )
            return
        if not nome.strip() or len(nome) > 80:
            await ctx.reply("Informe um nome de até 80 caracteres.")
            return
        with conectar_banco() as banco:
            cartela = banco.execute(
                """
                SELECT criador_id, status, casas_total FROM raspadinhas
                WHERE id = ? AND servidor_id = ?
                """,
                (raspadinha_id, ctx.guild.id),
            ).fetchone()
            if cartela is None:
                await ctx.reply("Essa raspadinha não existe neste servidor.")
                return
            if not self.pode_gerenciar(ctx, int(cartela["criador_id"])):
                await ctx.reply("Só o responsável, mods ou admins podem editar prêmios.")
                return
            if cartela["status"] == "arquivada":
                await ctx.reply("Não é possível alterar uma raspadinha arquivada.")
                return
            total_atual = banco.execute(
                """
                SELECT COALESCE(SUM(quantidade), 0)
                FROM raspadinha_premios WHERE raspadinha_id = ?
                """,
                (raspadinha_id,),
            ).fetchone()[0]
            if total_atual + quantidade > int(cartela["casas_total"]):
                await ctx.reply(
                    f"Essa quantidade ultrapassa as {cartela['casas_total']} casas. "
                    f"Já foram configuradas {total_atual} unidades de prêmio."
                )
                return
            colunas = {
                str(coluna["name"])
                for coluna in banco.execute(
                    "PRAGMA table_info(raspadinha_premios)"
                ).fetchall()
            }
            tipo_legado = tipo if tipo in {"normal", "grande", "vazio"} else "normal"
            if "peso" in colunas:
                banco.execute(
                    """
                    INSERT INTO raspadinha_premios (
                        raspadinha_id, nome, valor, peso, quantidade, tipo,
                        classificacao
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        raspadinha_id,
                        nome.strip(),
                        valor,
                        quantidade,
                        quantidade,
                        tipo_legado,
                        tipo,
                    ),
                )
            else:
                banco.execute(
                    """
                    INSERT INTO raspadinha_premios (
                        raspadinha_id, nome, valor, quantidade, tipo, classificacao
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        raspadinha_id,
                        nome.strip(),
                        valor,
                        quantidade,
                        tipo_legado,
                        tipo,
                    ),
                )
        await ctx.reply(
            f"Prêmio **{nome.strip()}** adicionado à raspadinha #{raspadinha_id} "
            f"(valor {valor}, quantidade {quantidade}, tipo {tipo})."
        )

    @raspadinha.command(name="quantidade")
    async def atualizar_quantidade_premio(
        self,
        ctx: commands.Context,
        raspadinha_id: int,
        premio_id: int,
        quantidade: int,
    ) -> None:
        if ctx.guild is None:
            await ctx.reply("Atualize prêmios dentro do servidor da raspadinha.")
            return
        if quantidade < 0 or quantidade > CASAS_MAXIMAS:
            await ctx.reply("A quantidade deve ser um número entre 0 e 100.")
            return
        with conectar_banco() as banco:
            cartela = banco.execute(
                """
                SELECT criador_id, status, casas_total
                FROM raspadinhas WHERE id = ? AND servidor_id = ?
                """,
                (raspadinha_id, ctx.guild.id),
            ).fetchone()
            if cartela is None:
                await ctx.reply("Essa raspadinha não existe neste servidor.")
                return
            if not self.pode_gerenciar(ctx, int(cartela["criador_id"])):
                await ctx.reply("Só o responsável, mods ou admins podem editar prêmios.")
                return
            if cartela["status"] != "ativa":
                await ctx.reply("Só é possível ajustar prêmios de uma raspadinha ativa.")
                return
            premio = banco.execute(
                """
                SELECT nome, quantidade FROM raspadinha_premios
                WHERE id = ? AND raspadinha_id = ?
                """,
                (premio_id, raspadinha_id),
            ).fetchone()
            if premio is None:
                await ctx.reply("Esse prêmio não pertence a essa raspadinha.")
                return
            usados = banco.execute(
                """
                SELECT COUNT(*) FROM raspadinha_aberturas
                WHERE premio_id = ?
                """,
                (premio_id,),
            ).fetchone()[0]
            if quantidade < usados:
                await ctx.reply(
                    f"Esse prêmio já saiu {usados} vez(es); a quantidade não pode "
                    "ficar abaixo do que já foi revelado."
                )
                return
            total_atual = banco.execute(
                """
                SELECT COALESCE(SUM(quantidade), 0)
                FROM raspadinha_premios WHERE raspadinha_id = ?
                """,
                (raspadinha_id,),
            ).fetchone()[0]
            total_novo = total_atual - int(premio["quantidade"]) + quantidade
            if total_novo > int(cartela["casas_total"]):
                await ctx.reply(
                    f"As quantidades somariam {total_novo}, mas a cartela tem "
                    f"{cartela['casas_total']} casas."
                )
                return
            colunas = {
                str(coluna["name"])
                for coluna in banco.execute(
                    "PRAGMA table_info(raspadinha_premios)"
                ).fetchall()
            }
            if "peso" in colunas:
                banco.execute(
                    """
                    UPDATE raspadinha_premios
                    SET quantidade = ?, peso = MAX(1, ?)
                    WHERE id = ?
                    """,
                    (quantidade, quantidade, premio_id),
                )
            else:
                banco.execute(
                    "UPDATE raspadinha_premios SET quantidade = ? WHERE id = ?",
                    (quantidade, premio_id),
                )
        await ctx.reply(
            f"Prêmio **{premio['nome']}** (# {premio_id}) agora terá "
            f"{quantidade} unidade(s) nesta cartela."
        )

    @raspadinha.command(name="premios")
    async def listar_premios(
        self,
        ctx: commands.Context,
        raspadinha_id: int,
    ) -> None:
        if ctx.guild is None:
            await ctx.reply("Consulte prêmios dentro de um servidor.")
            return
        with conectar_banco() as banco:
            premios = banco.execute(
                """
                SELECT p.id, p.nome, p.valor, p.quantidade,
                    p.classificacao AS tipo,
                    (SELECT COUNT(*) FROM raspadinha_aberturas a
                     WHERE a.premio_id = p.id AND a.casa_indice IS NOT NULL) AS abertas
                FROM raspadinha_premios p
                JOIN raspadinhas r ON r.id = p.raspadinha_id
                WHERE p.raspadinha_id = ? AND r.servidor_id = ?
                ORDER BY p.id
                """,
                (raspadinha_id, ctx.guild.id),
            ).fetchall()
        if not premios:
            await ctx.reply("Essa raspadinha não existe ou não tem prêmios cadastrados.")
            return
        moeda = obter_nome_moeda(ctx.guild.id)
        with conectar_banco() as banco:
            casas_total = banco.execute(
                "SELECT casas_total FROM raspadinhas WHERE id = ? AND servidor_id = ?",
                (raspadinha_id, ctx.guild.id),
            ).fetchone()[0]
        quantidade_total = sum(int(item["quantidade"]) for item in premios)
        await ctx.reply(
            f"Quantidade configurada: **{quantidade_total}/{casas_total}** casas.\n"
            + "\n".join(
                f"**#{item['id']} · {item['nome']}** · {item['valor']} {moeda} · "
                f"quantidade {item['quantidade']} ({item['abertas']} já reveladas) "
                f"· {item['tipo']}"
                for item in premios
            )
        )

    @raspadinha.command(name="jogar")
    async def jogar(self, ctx: commands.Context, raspadinha_id: int) -> None:
        if ctx.guild is None:
            await ctx.reply("Jogue dentro do servidor da raspadinha.")
            return
        with conectar_banco() as banco:
            cartela = banco.execute(
                """
                SELECT nome, preco, casas_total, status, versao_preco
                FROM raspadinhas WHERE id = ? AND servidor_id = ?
                """,
                (raspadinha_id, ctx.guild.id),
            ).fetchone()
            tem_premios = banco.execute(
                """
                SELECT COALESCE(SUM(quantidade), 0)
                FROM raspadinha_premios WHERE raspadinha_id = ?
                """,
                (raspadinha_id,),
            ).fetchone()
            aberturas = banco.execute(
                """
                SELECT a.casa_indice, p.nome, p.classificacao AS tipo, a.valor_pago
                FROM raspadinha_aberturas a
                JOIN raspadinha_premios p ON p.id = a.premio_id
                WHERE a.raspadinha_id = ? AND a.casa_indice IS NOT NULL
                ORDER BY a.id
                """,
                (raspadinha_id,),
            ).fetchall()
        if cartela is None:
            await ctx.reply("Essa raspadinha não existe neste servidor.")
            return
        if cartela["status"] != "ativa":
            await ctx.reply("Essa raspadinha está pausada ou arquivada.")
            return
        if int(tem_premios[0]) != int(cartela["casas_total"]):
            await ctx.reply(
                f"As quantidades cadastradas somam {tem_premios[0]}, mas a cartela "
                f"tem {cartela['casas_total']} casas. O responsável deve ajustar as "
                f"quantidades com `!raspadinha premios {raspadinha_id}`."
            )
            return
        view = RaspadinhaView(
            self,
            ctx.guild.id,
            raspadinha_id,
            int(cartela["versao_preco"]),
            str(cartela["nome"]),
            int(cartela["preco"]),
            int(cartela["casas_total"]),
            {
                int(abertura["casa_indice"]): {
                    "nome": str(abertura["nome"]),
                    "tipo": str(abertura["tipo"]),
                    "valor": int(abertura["valor_pago"]),
                }
                for abertura in aberturas
            },
        )
        mensagem = await ctx.send(
            embed=view.criar_embed(
                str(cartela["nome"]),
                int(cartela["preco"]),
                obter_nome_moeda(ctx.guild.id),
            ),
            view=view,
        )
        view.mensagem = mensagem

    @raspadinha.command(name="atualizar")
    async def atualizar_preco(
        self,
        ctx: commands.Context,
        raspadinha_id: int,
        novo_preco: int,
    ) -> None:
        if ctx.guild is None:
            await ctx.reply("Atualize o preço dentro do servidor da raspadinha.")
            return
        with conectar_banco() as banco:
            cartela = banco.execute(
                """
                SELECT nome, criador_id, preco, status
                FROM raspadinhas WHERE id = ? AND servidor_id = ?
                """,
                (raspadinha_id, ctx.guild.id),
            ).fetchone()
            if cartela is None:
                await ctx.reply("Essa raspadinha não existe neste servidor.")
                return
            if not self.pode_gerenciar(ctx, int(cartela["criador_id"])):
                await ctx.reply("Só o responsável, mods ou admins podem atualizar o preço.")
                return
            if cartela["status"] != "pausada":
                await ctx.reply("Essa raspadinha não está aguardando atualização.")
                return
            if novo_preco <= int(cartela["preco"]):
                await ctx.reply(
                    f"O novo preço deve ser maior que o atual "
                    f"({cartela['preco']} {obter_nome_moeda(ctx.guild.id)})."
                )
                return
            banco.execute(
                """
                UPDATE raspadinhas
                SET preco = ?, status = 'ativa', versao_preco = versao_preco + 1,
                    atualizada_por = ?
                WHERE id = ? AND servidor_id = ? AND status = 'pausada'
                """,
                (novo_preco, ctx.author.id, raspadinha_id, ctx.guild.id),
            )
        await ctx.reply(
            f"Raspadinha **{cartela['nome']}** atualizada de {cartela['preco']} para "
            f"{novo_preco} {obter_nome_moeda(ctx.guild.id)} por casa e reativada."
        )

    @raspadinha.command(name="arquivar")
    async def arquivar(self, ctx: commands.Context, raspadinha_id: int) -> None:
        if ctx.guild is None:
            await ctx.reply("Arquive raspadinhas dentro de um servidor.")
            return
        async with self.obter_trava(raspadinha_id):
            with conectar_banco() as banco:
                cartela = banco.execute(
                    """
                    SELECT nome, criador_id FROM raspadinhas
                    WHERE id = ? AND servidor_id = ?
                    """,
                    (raspadinha_id, ctx.guild.id),
                ).fetchone()
                if cartela is None:
                    await ctx.reply("Essa raspadinha não existe neste servidor.")
                    return
                if not self.pode_gerenciar(ctx, int(cartela["criador_id"])):
                    await ctx.reply("Só o responsável, mods ou admins podem arquivar.")
                    return
                banco.execute(
                    """
                    UPDATE raspadinhas SET status = 'arquivada'
                    WHERE id = ? AND servidor_id = ?
                    """,
                    (raspadinha_id, ctx.guild.id),
                )
        await ctx.reply(f"Raspadinha **{cartela['nome']}** arquivada.")

    @raspadinha.command(name="remover")
    async def remover(self, ctx: commands.Context, raspadinha_id: int) -> None:
        if ctx.guild is None:
            await ctx.reply("Remova raspadinhas dentro do servidor em que foram criadas.")
            return
        async with self.obter_trava(raspadinha_id):
            with conectar_banco() as banco:
                banco.execute("BEGIN IMMEDIATE")
                cartela = banco.execute(
                    """
                    SELECT nome, criador_id FROM raspadinhas
                    WHERE id = ? AND servidor_id = ?
                    """,
                    (raspadinha_id, ctx.guild.id),
                ).fetchone()
                if cartela is None:
                    await ctx.reply("Essa raspadinha não existe neste servidor.")
                    return
                if not self.pode_gerenciar(ctx, int(cartela["criador_id"])):
                    await ctx.reply(
                        "Só o responsável, mods ou admins podem remover raspadinhas."
                    )
                    return
                banco.execute(
                    "DELETE FROM raspadinha_aberturas WHERE raspadinha_id = ?",
                    (raspadinha_id,),
                )
                banco.execute(
                    "DELETE FROM raspadinha_premios WHERE raspadinha_id = ?",
                    (raspadinha_id,),
                )
                banco.execute(
                    "DELETE FROM raspadinhas WHERE id = ? AND servidor_id = ?",
                    (raspadinha_id, ctx.guild.id),
                )
        await ctx.reply(
            f"Raspadinha **{cartela['nome']}** removida permanentemente, "
            "incluindo prêmios e histórico de aberturas."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RaspadinhasCog(bot))
