import random
import sqlite3
from datetime import datetime, time as datetime_time, timezone
from pathlib import Path

import discord
from discord.ext import commands, tasks

from sistemas.forjas.materiais import MATERIAIS, MISTURAS

TENTATIVAS_DIARIAS = 2
CHANCE_PEDRA_EXTRA = 25
GEMAS = {"diamante", "esmeralda", "safira", "rubi", "ametista"}
CAMINHO_BANCO = Path(__file__).resolve().parent.parent / "config" / "mineracao.sqlite3"

PESOS_MINERIOS = {
    None: 5,
    "carvão": 14,
    "calcário": 14,
    "ferro": 11,
    "cobre": 9,
    "alumínio": 7,
    "bauxita": 6,
    "amianto": 5,
    "bronze": 8,
    "ouro": 8,
    "nióbio": 2,
    "diamante": 2,
    "esmeralda": 2,
    "safira": 2,
    "rubi": 2,
    "ametista": 2,
}
PESOS_MINERIOS["a escolha"] = 100 - sum(PESOS_MINERIOS.values())

MATERIAIS_DIVINOS = {
    "bronze celestial",
    "ouro imperial",
    "prata lunar",
    "ferro estígeo",
}
MINERIOS_SURPRESA = tuple(
    material
    for material in MATERIAIS
    if material not in MATERIAIS_DIVINOS and material != "madeira"
)


def apagar_banco_mineracao() -> None:
    if not CAMINHO_BANCO.exists():
        return

    try:
        CAMINHO_BANCO.unlink()
    except OSError:
        banco = sqlite3.connect(CAMINHO_BANCO, timeout=10)
        try:
            banco.execute("DELETE FROM mineracao_diaria")
            banco.commit()
            banco.execute("VACUUM")
        finally:
            banco.close()


def reservar_tentativa(
    servidor_id: int,
    usuario_id: int,
    hoje: str | None = None,
) -> int | None:
    dia = hoje or datetime.now(timezone.utc).date().isoformat()
    CAMINHO_BANCO.parent.mkdir(parents=True, exist_ok=True)

    banco = sqlite3.connect(CAMINHO_BANCO, timeout=10)
    try:
        banco.execute(
            """
            CREATE TABLE IF NOT EXISTS mineracao_diaria (
                servidor_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                dia TEXT NOT NULL,
                tentativas INTEGER NOT NULL CHECK (tentativas BETWEEN 1 AND 2),
                PRIMARY KEY (servidor_id, usuario_id, dia)
            )
            """
        )
        banco.execute("BEGIN IMMEDIATE")
        banco.execute("DELETE FROM mineracao_diaria WHERE dia < ?", (dia,))
        registro = banco.execute(
            """
            SELECT tentativas FROM mineracao_diaria
            WHERE servidor_id = ? AND usuario_id = ? AND dia = ?
            """,
            (servidor_id, usuario_id, dia),
        ).fetchone()

        if registro and registro[0] >= TENTATIVAS_DIARIAS:
            banco.commit()
            return None

        tentativas = registro[0] + 1 if registro else 1
        banco.execute(
            """
            INSERT INTO mineracao_diaria (servidor_id, usuario_id, dia, tentativas)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (servidor_id, usuario_id, dia)
            DO UPDATE SET tentativas = excluded.tentativas
            """,
            (servidor_id, usuario_id, dia, tentativas),
        )
        banco.commit()
        return tentativas
    finally:
        banco.close()


def rolar_mineracao() -> tuple[str | None, int, bool, int]:
    materiais = list(PESOS_MINERIOS)
    pesos = list(PESOS_MINERIOS.values())
    sorteado = random.choices(materiais, weights=pesos, k=1)[0]
    chance = PESOS_MINERIOS[sorteado]
    if sorteado is None:
        return None, 0, False, chance

    mineral = random.choice(MINERIOS_SURPRESA) if sorteado == "a escolha" else sorteado

    dados = MATERIAIS.get(mineral) or MISTURAS[mineral]
    quantidade = sum(random.randint(1, 5) for _ in range(1 if mineral in GEMAS else 2))
    pedra_extra = random.randint(1, 100) <= CHANCE_PEDRA_EXTRA
    return mineral, quantidade, pedra_extra, chance


def pluralizar_material(nome: str) -> str:
    nome = nome.casefold()
    if nome.endswith("ão"):
        return f"{nome[:-2]}ões"
    return f"{nome}s"


class MineracaoCog(commands.Cog):
    async def cog_load(self) -> None:
        self.limpar_banco_diario.start()

    def cog_unload(self) -> None:
        self.limpar_banco_diario.cancel()

    @tasks.loop(time=datetime_time(hour=0, minute=0, tzinfo=timezone.utc))
    async def limpar_banco_diario(self) -> None:
        apagar_banco_mineracao()

    @limpar_banco_diario.before_loop
    async def aguardar_bot_pronto(self) -> None:
        await self.bot.wait_until_ready()

    @commands.command(name="minerar")
    async def minerar(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply("A mineração só pode ser usada dentro de um servidor.")
            return

        tentativa = reservar_tentativa(ctx.guild.id, ctx.author.id)
        if tentativa is None:
            await ctx.reply(
                f"Você já usou suas {TENTATIVAS_DIARIAS} tentativas de mineração "
                "neste servidor hoje. O limite reinicia à meia-noite."
            )
            return

        mineral, quantidade, pedra_extra, chance = rolar_mineracao()
        embed = discord.Embed(
            title="Resultado da mineração",
            color=discord.Color.from_rgb(139, 90, 43),
        )
        if mineral is None:
            resultado = f"Nada encontrado ({chance}% de chance)"
        else:
            dados = MATERIAIS.get(mineral) or MISTURAS[mineral]
            resultado = (
                f"-# > Parabéns! Você conseguiu **x{quantidade}** "
                f"{pluralizar_material(dados['nome_exibicao'])}."
            )
            if pedra_extra:
                pedras = random.randint(1, 3)
                resultado += f"\n-# > Também encontrou **x{pedras}** pedras."

        embed.description = resultado
        embed.set_footer(
            text=(
                f"Você rodou {tentativa}/{TENTATIVAS_DIARIAS} "
                f"no servidor {ctx.guild.name} no dia de hoje."
            )
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MineracaoCog())